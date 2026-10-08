"""pet/swe_env.py —— mini-swe 沙箱命令执行器（v0.21 mini-swe 集成 · M1）。

对应 mini-swe-agent 的 ``environments/local.py``（约 60 行核心：``subprocess``
执行 + 超时杀整棵进程树），并按本项目安全铁律叠加**逻辑沙箱层**：

- **工作区锁定**：所有命令 ``cwd=<workspace>``；``..`` 穿越、``~`` 家目录、
  工作区外的绝对路径一律**硬拒**（fail-closed，不给确认机会）。
- **黑名单硬拒**：提权（sudo/su/doas）、``rm -rf``、磁盘破坏、关机重启、
  fork 炸弹、命令替换注入（``$(...)`` / 反引号）——永不执行。
- **危险二次确认**：删除/写文件系统/网络访问/改权限/结束进程/内联代码——
  经注入的 ``confirm_fn``（签名同 ``ToolRegistry`` 的 ``(title, command, risk)``），
  无确认通道则 fail-closed 拒绝。
- **资源上限**：单命令超时（POSIX ``killpg`` / win ``taskkill /T`` 杀整棵进程
  树，防孤儿）、输出截断（防灌爆上下文）。

纯 Python，零 Qt / 零 LLM 依赖，可直接单测（见 ``spikes/test_v21_swe_env.py``）。

安全边界诚实声明：``shell=True`` 属**逻辑沙箱**（靠黑名单/确认/工作区约束），
非 OS 级隔离（sandbox-exec / bwrap / Docker）。本版为 mini-swe 核心的轻量落地，
OS 级沙箱作为后续里程碑。
"""

from __future__ import annotations

import logging
import os
import re
import signal
import subprocess
import threading

log = logging.getLogger("pet")

# 默认资源上限（config swe 段可覆盖）
DEFAULT_TIMEOUT_S = 30.0
DEFAULT_OUTPUT_MAX_CHARS = 8000

# ---- 硬拒黑名单（fail-closed，永不执行）----
# (编译正则, 拦截原因)。顺序即优先级。
_BLOCK_PATTERNS = (
    (re.compile(r"^\s*(?:sudo|su|doas)(?:\s|$)", re.IGNORECASE), "禁止提权"),
    (re.compile(r"rm\s+-(?:[a-z]*r[a-z]*f[a-z]*|[a-z]*f[a-z]*r[a-z]*)\b",
                re.IGNORECASE), "禁止递归强删"),
    (re.compile(r"\b(?:rm|find)\b[^\n]*\s+-delete\b", re.IGNORECASE),
     "禁止批量删除"),
    (re.compile(r"\bmkfs\b", re.IGNORECASE), "禁止格式化"),
    (re.compile(r"\b(?:diskutil\s+erase|format\s+[a-z]:|fdisk|parted)\b",
                re.IGNORECASE), "禁止磁盘破坏"),
    (re.compile(r"\bdd\b[^\n]*\bof=/dev/", re.IGNORECASE), "禁止写块设备"),
    (re.compile(r"\b(?:shutdown|reboot|halt|poweroff)\b", re.IGNORECASE),
     "禁止关机/重启"),
    (re.compile(r"\binit\s+[06]\b", re.IGNORECASE), "禁止运行级切换"),
    (re.compile(r":\(\)\s*\{\s*:\|:&\s*\}\s*;"), "禁止 fork 炸弹"),
    # 命令替换 / 反引号：shell=True 下最大的注入面，整体硬拒。
    (re.compile(r"`|\$\("), "禁止命令替换注入"),
)

# ---- 危险模式（需 confirm_fn 放行）----
_DANGEROUS_PATTERNS = (
    (re.compile(r"\brm\b", re.IGNORECASE), "删除文件"),
    (re.compile(r"\b(?:mv|cp|install|mkdir|touch|tee)\b", re.IGNORECASE),
     "写文件系统"),
    (re.compile(r"\b(?:curl|wget|pip\s+install|git\s+(?:clone|push|fetch)"
                r"|npm\s+install|brew|apt|apt-get|dnf|yum|ssh|scp|ftp"
                r"|nc|ncat|telnet)\b", re.IGNORECASE), "网络/外部访问"),
    (re.compile(r"\b(?:chmod|chown|chattr)\b", re.IGNORECASE), "修改权限"),
    (re.compile(r"\b(?:kill|pkill|killall|taskkill)\b", re.IGNORECASE),
     "结束进程"),
    (re.compile(r"\b(?:python\w*|perl|ruby|node|sh|bash)\b\s+-\w*c\b",
                re.IGNORECASE), "内联执行代码"),
)

# 路径穿越 ``..`` 作为路径分量；``~`` 家目录逃逸（前导或跟在空白/分隔符后）
_TRAVERSAL = re.compile(r"(?:^|[\s/])\.\.(?:/|$|[\s;|&])")
_TILDE = re.compile(r"(?:^|[\s;|&\"'])~")
# 绝对路径 token（用于「越出工作区」判定；只匹配 /xx[/yy...]，不含裸 ``/``）
_ABS_TOKEN = re.compile(r"/[A-Za-z0-9_.\-]+(?:/[A-Za-z0-9_.\-]+)*")


def _outside_workspace(command: str, workspace: str):
    """返回第一个越出工作区的绝对路径 token；全在区内则返回 None。

    绝对路径经 normpath+abspath 归一后须落在 ``workspace`` 前缀内，否则判越界。
    """
    ws = os.path.realpath(workspace)
    for m in _ABS_TOKEN.finditer(command):
        tok = m.group(0)
        try:
            p = os.path.realpath(tok)
        except OSError:
            p = tok
        if p != ws and not p.startswith(ws + os.sep):
            return tok
    return None


def scan_command(command: str, workspace: str,
                 extra_block_patterns=()) -> tuple:
    """判定命令安全级别 → ``(decision, reason)``。

    decision ∈ {"block"（硬拒）, "confirm"（需确认）, "allow"（直接执行）}。
    纯函数便于单测。
    """
    if not command or not command.strip():
        return "block", "空命令"
    for rx, why in _BLOCK_PATTERNS:
        if rx.search(command):
            return "block", why
    for rx in extra_block_patterns:
        if rx.search(command):
            return "block", "自定义黑名单"
    if _TRAVERSAL.search(command):
        return "block", "路径穿越"
    if _TILDE.search(command):
        return "block", "家目录逃逸"
    outside = _outside_workspace(command, workspace)
    if outside is not None:
        return "block", f"越出工作区路径: {outside}"
    for rx, why in _DANGEROUS_PATTERNS:
        if rx.search(command):
            return "confirm", why
    return "allow", ""


def _kill_tree(proc, pgid, posix: bool) -> None:
    """杀整棵进程树：POSIX ``killpg(pgid)``（shell=True + start_new_session
    使子进程为独立组）；win ``taskkill /T /F``。兜底 ``proc.kill()``。"""
    if posix:
        if pgid is not None:
            try:
                os.killpg(pgid, signal.SIGKILL)
                return
            except (ProcessLookupError, PermissionError, OSError):
                pass
    elif os.name == "nt":
        try:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                           capture_output=True, timeout=10)
            return
        except Exception:
            pass
    try:
        proc.kill()
    except OSError:
        pass


def _run(command: str, cwd: str, timeout: float,
         output_max_chars: int) -> dict:
    """执行命令（``shell=True``），有界读取输出，超时杀整棵进程树。

    返回 ``{output, returncode, exception_info, truncated, blocked,
    reason, decision}``。
    """
    posix = os.name == "posix"
    popen_kwargs = {
        "shell": True,
        "cwd": cwd,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
    }
    if posix:
        popen_kwargs["start_new_session"] = True  # 独立进程组，便于 killpg
    elif hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP

    try:
        proc = subprocess.Popen(command, **popen_kwargs)
    except OSError as exc:
        return {"output": "", "returncode": -1, "exception_info": str(exc),
                "truncated": False, "blocked": False, "reason": "",
                "decision": "allow"}

    pgid = None
    if posix:
        try:
            pgid = os.getpgid(proc.pid)
        except OSError:
            pgid = None

    buf = bytearray()
    state = {"overflow": False}

    def _drain():
        try:
            while True:
                chunk = proc.stdout.read(65536)
                if not chunk:
                    break
                if len(buf) + len(chunk) > output_max_chars:
                    if len(buf) < output_max_chars:
                        buf.extend(chunk[: output_max_chars - len(buf)])
                    state["overflow"] = True
                    continue  # 丢弃余量继续读，防子进程管道写满阻塞
                buf.extend(chunk)
        except Exception:
            pass

    reader = threading.Thread(target=_drain, daemon=True)
    reader.start()

    try:
        proc.wait(timeout=timeout)
        exception_info = ""
    except subprocess.TimeoutExpired:
        _kill_tree(proc, pgid, posix)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        exception_info = f"命令超时（>{timeout:g}s），已终止"

    # 正常退出后仍尽力清后台 straggler（如 ``sleep 60 &``），保证无孤儿
    if posix and pgid is not None and not exception_info:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass

    reader.join(timeout=3)

    text = buf.decode("utf-8", errors="replace")
    if state["overflow"]:
        text += f"\n[输出超过 {output_max_chars} 字符，已截断]"
    return {"output": text, "returncode": proc.returncode,
            "exception_info": exception_info, "truncated": state["overflow"],
            "blocked": False, "reason": "", "decision": "allow"}


class SweEnvironment:
    """mini-swe 沙箱环境：受约束地执行单条 shell 命令。

    ``confirm_fn(title, command, risk) -> bool`` 注入（对齐 ``ToolRegistry``
    的确认语义）；缺省 None 时危险命令 fail-closed 拒绝。
    """

    def __init__(self, workspace_dir: str, *,
                 command_timeout_s: float = DEFAULT_TIMEOUT_S,
                 output_max_chars: int = DEFAULT_OUTPUT_MAX_CHARS,
                 block_patterns=None, confirm_fn=None) -> None:
        if not workspace_dir:
            raise ValueError("workspace_dir 不能为空")
        self.workspace = os.path.realpath(os.path.expanduser(workspace_dir))
        try:
            os.makedirs(self.workspace, exist_ok=True)
        except OSError as exc:
            log.warning("工作区创建失败: %s", exc)
        self.timeout = float(command_timeout_s)
        self.output_max_chars = int(output_max_chars)
        self._confirm_fn = confirm_fn
        self._block_patterns = tuple(
            re.compile(p, re.IGNORECASE) for p in (block_patterns or [])
        )

    def execute(self, command: str, cwd: str = "",
                timeout=None) -> dict:
        """执行命令，返回结果 dict（见 ``_run`` 返回结构）。"""
        cwd = cwd or self.workspace
        decision, reason = scan_command(command, self.workspace,
                                        self._block_patterns)
        if decision == "block":
            return {"output": "", "returncode": -1, "exception_info": "",
                    "truncated": False, "blocked": True,
                    "reason": f"已拦截：{reason}", "decision": "block"}
        if decision == "confirm":
            if self._confirm_fn is None:
                return {"output": "", "returncode": -1, "exception_info": "",
                        "truncated": False, "blocked": True,
                        "reason": f"需确认但无确认通道：{reason}",
                        "decision": "confirm"}
            try:
                ok = bool(self._confirm_fn("危险命令确认", command, reason))
            except Exception:
                log.warning("危险命令确认回调异常，按拒绝处理", exc_info=True)
                ok = False
            if not ok:
                return {"output": "", "returncode": -1, "exception_info": "",
                        "truncated": False, "blocked": True,
                        "reason": "用户取消", "decision": "confirm"}
        return _run(command, cwd,
                    float(timeout if timeout is not None else self.timeout),
                    self.output_max_chars)
