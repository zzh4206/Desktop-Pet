"""桌宠资源占用监控（CPU / 内存 / 线程）。

用途
----
在桌宠运行期间按固定间隔采样其 CPU 占用、常驻内存（RSS）与线程数，实时打印、
落盘 CSV，并在结束时给出统计摘要。默认**附加（attach）**到已在运行的桌宠进程；
也可用 ``--launch`` 由本脚本代为启动桌宠后监控。

口径对齐 ``mac任务清单.md`` 的浸泡测试：内存 ≤ 200MB、idle CPU < 1%。

用法示例
--------
    # 1) 桌宠已在运行 —— 附加监控（Ctrl+C 结束并出摘要）
    .venv/bin/python tools/monitor_pet.py

    # 2) 由脚本启动桌宠，监控 5 分钟，1 秒一次
    .venv/bin/python tools/monitor_pet.py --launch --duration 300 --interval 1

    # 3) 指定进程 PID 或自定义启动命令
    .venv/bin/python tools/monitor_pet.py --pid 12345
    .venv/bin/python tools/monitor_pet.py --launch --cmd ".venv/bin/python app.py --verbose"

依赖
----
psutil（已在 .venv 中）。运行本脚本请使用项目虚拟环境解释器。
"""

from __future__ import annotations

import argparse
import csv
import shlex
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import psutil

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_APP = REPO_ROOT / "app.py"

# 浸泡测试口径阈值
MEM_LIMIT_MB = 200.0
IDLE_CPU_PCT = 1.0


# --------------------------------------------------------------------------- #
# 进程发现
# --------------------------------------------------------------------------- #
def find_pet_process() -> psutil.Process:
    """按命令行匹配 ``python app.py``，排除自身。"""
    self_pid = psutil.Process().pid
    candidates = []
    for proc in psutil.process_iter(["pid", "name", "cmdline", "create_time"]):
        try:
            info = proc.info
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if info["pid"] == self_pid:
            continue
        name = (info["name"] or "").lower()
        cmdline = info["cmdline"] or []
        if "python" not in name:
            continue
        # 命令行里存在以 app.py 结尾的参数即视为桌宠入口
        if not any(Path(str(c)).name == "app.py" for c in cmdline):
            continue
        candidates.append((info["create_time"] or 0, info["pid"]))
    if not candidates:
        raise RuntimeError(
            "未找到正在运行的桌宠进程（python app.py）。"
            "请先启动桌宠，或改用 --launch 由本脚本启动。"
        )
    # 多个候选取最新启动者
    candidates.sort(reverse=True)
    return psutil.Process(candidates[0][1])


# --------------------------------------------------------------------------- #
# 采样器
# --------------------------------------------------------------------------- #
class Sampler:
    def __init__(self, pid: int, include_tree: bool) -> None:
        self.pid = pid
        self.include_tree = include_tree
        # pid -> Process。psutil 的 cpu_percent 基线缓存在 Process 实例上，
        # 因此必须跨采样复用同一实例，否则每次都返回 0.0。
        self._cache: dict[int, psutil.Process] = {}

    def _get(self, pid: int) -> psutil.Process | None:
        proc = self._cache.get(pid)
        if proc is None:
            try:
                proc = psutil.Process(pid)
            except psutil.NoSuchProcess:
                return None
            try:
                proc.cpu_percent(interval=None)  # 建立基线（本次返回 0）
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
            self._cache[pid] = proc
        return proc

    def _pids(self) -> list[int]:
        root = self._get(self.pid)
        if root is None:
            return []
        pids = [self.pid]
        if self.include_tree:
            try:
                pids += [c.pid for c in root.children(recursive=True)]
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        return pids

    def prime(self) -> None:
        """建立 CPU 基线（首点恒为 0，循环里会自然丢弃该瞬态）。"""
        self._pids()

    def sample(self) -> dict | None:
        total_cpu = 0.0
        total_rss = 0.0
        total_threads = 0
        seen = 0
        for pid in self._pids():
            proc = self._get(pid)
            if proc is None:
                continue
            data = self._read(proc)
            if data is None:
                continue
            seen += 1
            total_cpu += data["cpu_pct"]
            total_rss += data["rss_mb"]
            total_threads += data["threads"]
        if seen == 0:
            return None
        return {
            "cpu_pct": total_cpu,
            "rss_mb": total_rss,
            "threads": total_threads,
        }

    @staticmethod
    def _read(proc: psutil.Process) -> dict | None:
        try:
            with proc.oneshot():
                cpu = proc.cpu_percent(interval=None)
                mem = proc.memory_info()
                threads = proc.num_threads()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return None
        return {
            "cpu_pct": cpu,
            "rss_mb": mem.rss / (1024 * 1024),
            "threads": threads,
        }


# --------------------------------------------------------------------------- #
# 输出
# --------------------------------------------------------------------------- #
def fmt_summary(samples: list[dict], pid: int, tree: bool) -> str:
    def stats(key: str):
        vals = [s[key] for s in samples]
        vals_sorted = sorted(vals)
        n = len(vals_sorted)
        avg = sum(vals) / n
        p95 = vals_sorted[min(n - 1, int(n * 0.95))]
        return avg, min(vals), max(vals), p95

    cpu_avg, cpu_min, cpu_max, cpu_p95 = stats("cpu_pct")
    rss_avg, rss_min, rss_max, rss_p95 = stats("rss_mb")
    thr_avg, _, thr_max, _ = stats("threads")

    duration = samples[-1]["elapsed_s"] - samples[0]["elapsed_s"]
    rss_growth = samples[-1]["rss_mb"] - samples[0]["rss_mb"]

    lines = [
        "",
        "=" * 62,
        "  桌宠资源占用摘要",
        "=" * 62,
        f"  进程 PID            : {pid}{'（含子进程树）' if tree else ''}",
        f"  采样点数 / 时长     : {len(samples)} 点 / {duration:.1f}s",
        f"  采样间隔            : {samples[1]['elapsed_s'] - samples[0]['elapsed_s']:.2f}s"
        if len(samples) > 1
        else "",
        "",
        "  CPU（原始值，多核 / 含子进程下可 >100%）:",
        f"    平均 {cpu_avg:6.2f}%  最小 {cpu_min:6.2f}%  最大 {cpu_max:6.2f}%  P95 {cpu_p95:6.2f}%",
        "",
        "  常驻内存 RSS:",
        f"    平均 {rss_avg:7.1f}MB  最小 {rss_min:7.1f}MB  峰值 {rss_max:7.1f}MB  P95 {rss_p95:7.1f}MB",
        f"    首末变化 {rss_growth:+7.1f}MB",
        "",
        f"  线程数：平均 {thr_avg:.1f}  最大 {thr_max}",
        "",
    ]
    # 浸泡测试口径提示
    flags = []
    if rss_max > MEM_LIMIT_MB:
        flags.append(f"峰值内存 {rss_max:.0f}MB 超过浸泡测试上限 {MEM_LIMIT_MB:.0f}MB")
    if cpu_avg > IDLE_CPU_PCT:
        flags.append(f"平均 CPU {cpu_avg:.2f}% 高于 idle 口径 {IDLE_CPU_PCT}%")
    if flags:
        lines.append("  阈值提示:")
        for f in flags:
            lines.append(f"    ⚠ {f}")
    else:
        lines.append("  阈值：均未超过浸泡测试口径（内存 ≤200MB、idle CPU <1%）。")
    lines.append("=" * 62)
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="监控桌宠运行时的 CPU / 内存占用")
    ap.add_argument("--launch", action="store_true",
                    help="由本脚本启动桌宠后监控（否则附加到已运行实例）")
    ap.add_argument("--pid", type=int, default=None,
                    help="直接监控指定 PID（覆盖自动发现 / --launch 不适用）")
    ap.add_argument("--cmd", default=None,
                    help="--launch 的启动命令；默认 '.venv/bin/python app.py'")
    ap.add_argument("--interval", type=float, default=1.0,
                    help="采样间隔（秒），默认 1.0")
    ap.add_argument("--duration", type=float, default=0.0,
                    help="监控时长（秒）；0 表示直到 Ctrl+C 或桌宠退出")
    ap.add_argument("--out", default=None,
                    help="CSV 输出路径；默认 tools/monitor_pet_<时间戳>.csv")
    ap.add_argument("--tree", action="store_true",
                    help="汇总主进程及其子进程的 CPU / 内存")
    ap.add_argument("--quiet", action="store_true",
                    help="不逐行打印，仅在结束输出摘要")
    ap.add_argument("--print-every", type=int, default=1,
                    help="每 N 次采样打印一行（默认 1）")
    args = ap.parse_args()

    # 后台/管道下 print 默认块缓冲，改行缓冲保证进度实时可见
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)

    # ---- 启动 / 附加 ----
    launched = None
    if args.pid is not None:
        pid = args.pid
    elif args.launch:
        cmd = shlex.split(args.cmd) if args.cmd else [sys.executable, str(DEFAULT_APP)]
        print(f"[monitor] 启动桌宠: {' '.join(cmd)}")
        launched = subprocess.Popen(cmd)
        pid = launched.pid
    else:
        proc = find_pet_process()
        pid = proc.pid
        print(f"[monitor] 附加到桌宠进程 PID={pid}")

    sampler = Sampler(pid, include_tree=args.tree)

    out_path = args.out or str(
        REPO_ROOT / "tools" / f"monitor_pet_{datetime.now():%Y%m%d_%H%M%S}.csv"
    )

    # 等待目标就绪 + 预热 CPU 基线
    sampler.prime()
    time.sleep(max(0.5, args.interval))

    samples: list[dict] = []
    start = time.monotonic()
    next_sample_at = start

    def stop_handler(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, stop_handler)
    signal.signal(signal.SIGTERM, stop_handler)

    header = ["ts", "elapsed_s", "cpu_pct", "cpu_norm_pct", "rss_mb", "threads"]
    cpu_cores = psutil.cpu_count() or 1
    print(f"[monitor] 采样间隔 {args.interval}s，结果写入 {out_path}")
    if not args.quiet:
        print("  " + "  ".join(header))

    try:
        with open(out_path, "w", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(header)
            idx = 0
            while True:
                if args.duration and (time.monotonic() - start) >= args.duration:
                    break
                data = sampler.sample()
                if data is None:
                    print("\n[monitor] 桌宠进程已退出，停止监控。")
                    break

                elapsed = time.monotonic() - start
                row = [
                    datetime.now().strftime("%H:%M:%S"),
                    f"{elapsed:.2f}",
                    f"{data['cpu_pct']:.2f}",
                    f"{data['cpu_pct'] / cpu_cores:.2f}",
                    f"{data['rss_mb']:.2f}",
                    data["threads"],
                ]
                writer.writerow(row)
                fh.flush()

                record = {
                    "elapsed_s": elapsed,
                    "cpu_pct": data["cpu_pct"],
                    "rss_mb": data["rss_mb"],
                    "threads": data["threads"],
                }
                samples.append(record)

                if not args.quiet and idx % args.print_every == 0:
                    print(
                        f"  {row[0]}  {row[1]:>8}s  {row[2]:>6}%  "
                        f"{row[3]:>5}%  {row[4]:>7}MB  {row[5]:>4}thr"
                    )
                idx += 1

                # 中断式睡眠，Ctrl+C 立即生效
                next_sample_at += args.interval
                while time.monotonic() < next_sample_at:
                    time.sleep(0.05)
    except KeyboardInterrupt:
        print("\n[monitor] 收到中断信号，停止采样。")
    finally:
        if launched is not None and launched.poll() is None:
            print("[monitor] 停止并关闭由本脚本启动的桌宠…")
            launched.terminate()
            try:
                launched.wait(timeout=5)
            except subprocess.TimeoutExpired:
                launched.kill()

    if len(samples) < 2:
        print("[monitor] 采样点不足，无法给出统计。")
        return 1

    print(fmt_summary(samples, pid, args.tree))
    print(f"[monitor] 明细已写入 {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
