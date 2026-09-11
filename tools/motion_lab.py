#!/usr/bin/env python3
"""motion_lab —— 新动效引擎独立开发台（v0.15.x 与 app 解耦后的验证入口）。

背景
----
新引擎（``pet.rig.motion`` + 风/光影通道 + rig/paperdoll 呈现）已从 ``app.py``
独立开来：app 暂锁定 ``frames`` 原引擎接管画面（app.py 内 ``⚠️ 回退·待合并``
TODO 标记处）。本脚本在**脱离 app/FSM/配置**的前提下单独跑、单独看、单独回归，
成熟后再把引擎合回 app。

两种模式
--------
* ``--dump``（无 GUI，零 Qt 依赖）：按脚本化输入序列推进引擎，打印关键观测
  （呼吸浮动幅度 / 超低频漂移 / 眨眼次数 / 走路律动 / 落地 squash 回弹 /
  速度倾斜收敛），可选导出逐帧 CSV。
* ``--view``（有 GUI）：``RigWindow`` 直驱目检，序列 idle 呼吸 → walk →
  落地 squash；``--wind`` / ``--sun`` 接入风 / 光影通道。

用法
----
    python tools/motion_lab.py --dump --seconds 10 [--csv /tmp/motion.csv]
    python tools/motion_lab.py --view [--seconds 12] [--wind] [--sun]

从仓库根目录运行；脚本会自动定位 ``assets/rig``。
"""

from __future__ import annotations

import argparse
import csv
import math
import os
import sys

_THIS = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_THIS)
sys.path.insert(0, _ROOT)

from pet.rig.motion import MotionEngine, MotionInputs  # noqa: E402
from pet.rig.spec import load_rig_spec  # noqa: E402

DT_MS = 33.0
RIG_REL = os.path.join("assets", "rig", "final")
STAGE = "final"

# 风/光影通道演示用坐标（默认北京；改你自己的城市）
DEMO_CFG = {
    "wind": {"enabled": True, "latitude": 39.9, "longitude": 116.4,
             "poll_minutes": 20, "fallback_gain": 1.0},
    "sun": {"enabled": True, "latitude": 39.9, "longitude": 116.4,
            "timezone_offset": None, "shadow_alpha": 0.4},
}


# --------------------------------------------------------------------------- #
# 公用
# --------------------------------------------------------------------------- #
def _find_rig_dir() -> str | None:
    for base in (os.getcwd(), _ROOT):
        d = os.path.join(base, RIG_REL)
        if os.path.isdir(d):
            return d
    return None


def _load_spec():
    d = _find_rig_dir()
    if d is None:
        print("[motion_lab] 未找到 rig 资产目录，引擎以无部件（纯 body）运行。")
        return None
    return load_rig_spec(d, STAGE)


# --------------------------------------------------------------------------- #
# --dump：无 GUI 数值验证
# --------------------------------------------------------------------------- #
def run_dump(seconds: float, csv_path: str | None) -> int:
    spec = _load_spec()
    n_parts = len(spec.parts) if spec else 0
    eng = MotionEngine(spec)
    steps = max(1, int(seconds * 1000.0 / DT_MS))
    print(f"[dump] spec parts={n_parts}  dt={DT_MS:.0f}ms  steps={steps}")

    # 脚本化输入：0–3s idle → 3–6s walk(倾斜5°,1.3Hz) → 6s 停 → 6.2s 触发落地
    def inputs(t_s: float) -> MotionInputs:
        if t_s < 3.0:
            return MotionInputs(walking=False, tilt_deg=0.0)
        if t_s < 6.0:
            return MotionInputs(walking=True, tilt_deg=5.0, walk_hz=1.3)
        return MotionInputs(walking=False, tilt_deg=0.0)

    # 统计容器
    idle_float = [math.inf, -math.inf]   # body_y min/max（纯 idle 0–3s）
    idle_drift = [math.inf, -math.inf]   # body_angle min/max（纯 idle 0–3s）
    walk_bob = [math.inf, -math.inf]     # body_y min/max（walk 3–6s）
    squash_min = 1.0
    squash_peak = 1.0
    squash_done = False
    blink_count = 0
    prev_blink = False
    gait_k_end = 0.0
    tilt_end = 0.0
    rows: list[dict] = []

    for i in range(steps):
        t_ms = i * DT_MS
        t_s = t_ms / 1000.0
        if not squash_done and t_s >= 6.2:
            eng.trigger_squash()
            squash_done = True
        f = eng.step(inputs(t_s), DT_MS)

        if t_s < 3.0:
            idle_float[0] = min(idle_float[0], f.body_y)
            idle_float[1] = max(idle_float[1], f.body_y)
            idle_drift[0] = min(idle_drift[0], f.body_angle)
            idle_drift[1] = max(idle_drift[1], f.body_angle)
        if 3.0 <= t_s < 6.0:
            walk_bob[0] = min(walk_bob[0], f.body_y)
            walk_bob[1] = max(walk_bob[1], f.body_y)
        if 5.9 <= t_s < 6.0:
            tilt_end = f.body_angle
            gait_k_end = eng.gait_k
        if squash_done:
            squash_min = min(squash_min, f.body_scale_y)
            squash_peak = max(squash_peak, f.body_scale_y)
        if f.blink_on and not prev_blink:
            blink_count += 1
        prev_blink = f.blink_on

        rows.append({
            "t_ms": round(t_ms, 1),
            "body_angle": round(f.body_angle, 4),
            "scale_x": round(f.body_scale_x, 4),
            "scale_y": round(f.body_scale_y, 4),
            "body_y": round(f.body_y, 4),
            "blink_on": int(f.blink_on),
            "gait_k": round(eng.gait_k, 4),
            "gait_phase": round(eng.gait_phase, 4),
        })

    # ---- 观测报告 ----
    print("\n== 观测（脚本化 10s 序列）==")
    print(f"呼吸纵向浮动：idle body_y ∈ [{idle_float[0]:+.2f}, "
          f"{idle_float[1]:+.2f}]px（期望 ≈±2.5px）")
    print(f"超低频漂移  ：idle body_angle ∈ [{idle_drift[0]:+.2f}, "
          f"{idle_drift[1]:+.2f}]°（期望 ≈±2°，3s 只采到部分周期）")
    print(f"眨眼脉冲    ：{blink_count} 次 / {seconds:.0f}s（期望约 1 次 / 3–6s）")
    print(f"走路律动    ：walk body_y ∈ [{walk_bob[0]:+.2f}, "
          f"{walk_bob[1]:+.2f}]px，gait_k(6s)={gait_k_end:.2f}（期望→1.0）")
    print(f"速度倾斜    ：walk 末 body_angle={tilt_end:+.2f}°（目标 5° + 漂移）")
    print(f"落地 squash  ：scale_y 谷={squash_min:.3f}（期望 ≈0.85，压缩15%）"
          f" / 峰值={squash_peak:.3f}（期望 >1.0 过冲回弹）")

    if csv_path:
        with open(csv_path, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        print(f"\n逐帧已导出 → {csv_path}")

    # 自检：关键量是否离开「死值」——全零说明引擎没动，直接判失败
    moved = (idle_float[1] - idle_float[0] > 0.5
             and walk_bob[1] - walk_bob[0] > 1.0
             and blink_count > 0
             and squash_min < 0.95)
    print(f"\n自检：引擎输出{'已离开死值 ✓' if moved else '疑似未动 ✗'}")
    return 0 if moved else 1


# --------------------------------------------------------------------------- #
# --view：GUI 目检
# --------------------------------------------------------------------------- #
def run_view(seconds: float, use_wind: bool, use_sun: bool) -> int:
    os.environ.setdefault("QT_QUICK_BACKEND", "")
    from PySide6.QtCore import QTimer
    from PySide6.QtGui import QGuiApplication, QScreen
    from PySide6.QtWidgets import QApplication

    from pet.asset_provider import AIArtProvider, SpriteRef
    from pet.rig.presenter import RigWindow

    app = QApplication.instance() or QApplication(sys.argv)
    provider = AIArtProvider()
    base = os.path.abspath(os.path.join("assets", "ai",
                                        "final_neglected_neutral.png"))
    spec = _load_spec()
    win = RigWindow(SpriteRef(path=base, width=320, height=320), spec)
    assert win.rig_active, "RigWindow 初始化失败（QML 不可用？）"

    scr: QScreen = QGuiApplication.primaryScreen()
    g = scr.availableGeometry()
    win.move_bottom_center(g.x() + int(g.width() * 0.62),
                           g.y() + int(g.height() * 0.86))
    win.show()

    # 风 / 光影源（懒加载，网络失败自动兜底，不阻断演示）
    wind_src = sun_src = None
    if use_wind:
        from pet.wind import build_wind_source
        wind_src = build_wind_source(DEMO_CFG["wind"])
    if use_sun:
        from pet.sun import build_sun_source
        sun_src = build_sun_source(DEMO_CFG["sun"])

    t = [0]

    def phase() -> None:
        t[0] += 1
        n = t[0]
        if n == 1:
            print("[view] idle 呼吸 + 漂移 + 眨眼（2s）")
        elif n == 2:
            print("[view] walk 律动循环（3s）")
            win.set_motion_params(tilt_deg=2.5, walking=True, walk_hz=1.3)
            win.play_frames(provider.frames_for("final", "walk"),
                            loop=True, interval_ms=90)
            QTimer.singleShot(1400, lambda: win.grab().save(
                os.path.abspath("spikes/_qa/motion_lab_walk.png")))
        elif n == 3:
            print("[view] 停步（1s）")
            win.set_motion_params(walking=False)
            win.stop_frames()
        elif n == 4:
            print("[view] 离地→落地 squash（1s 后触发）")
            win.set_motion_params(airborne=True)
        elif n == 5:
            win.set_motion_params(airborne=False)   # 下降沿 → 引擎 trigger_squash
            print("[view] squash 完成，保持至退出")

    timer = QTimer()
    timer.timeout.connect(phase)
    timer.start(2000)

    # 风/光影每 5s 刷新一次（真实网络请求走 daemon/惰性，此处直接 refresh）
    if wind_src is not None:
        def _wind_tick() -> None:
            try:
                wind_src.refresh()
                wg, wb = wind_src.current()
                win.set_motion_params(wind_gain=wg, wind_bias_deg=wb)
            except Exception as e:  # noqa: BLE001
                print(f"[view] wind 刷新失败（走兜底）：{e}")
        QTimer.singleShot(1500, _wind_tick)
    if sun_src is not None:
        def _sun_tick() -> None:
            try:
                sun_src.refresh()
                sh = sun_src.current()
                win.set_shadow(sh.alpha, sh.offset_x, sh.scale_x, sh.scale_y)
            except Exception as e:  # noqa: BLE001
                print(f"[view] sun 刷新失败（走兜底）：{e}")
        QTimer.singleShot(1500, _sun_tick)

    QTimer.singleShot(int(seconds * 1000), app.quit)
    rc = app.exec()
    print(f"[view] done rc={rc}")
    return rc


def main() -> int:
    ap = argparse.ArgumentParser(description="新动效引擎独立开发台")
    ap.add_argument("--dump", action="store_true", help="无 GUI 数值验证")
    ap.add_argument("--view", action="store_true", help="GUI 目检")
    ap.add_argument("--seconds", type=float, default=10.0, help="运行时长（dump 默认 10s）")
    ap.add_argument("--csv", help="dump 模式逐帧导出路径")
    ap.add_argument("--wind", action="store_true", help="view 模式接入风通道")
    ap.add_argument("--sun", action="store_true", help="view 模式接入光影通道")
    args = ap.parse_args()

    if args.view:
        return run_view(args.seconds, args.wind, args.sun)
    if args.dump:
        return run_dump(args.seconds, args.csv)

    # 无显式模式：无 GUI 走 dump（安全默认）
    return run_dump(args.seconds, args.csv)


if __name__ == "__main__":
    sys.exit(main())
