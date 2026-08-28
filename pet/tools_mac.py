"""mac 平台工具实现 —— 平台适配与分工.md §五工具命令映射。

样板 ``open_app``：``open -a "App"`` 开程序 / ``open URL`` 开网页。
按 §2.2 冻结的 ``ToolHandler.execute(args, ctx) -> ToolResult`` 写，
由 ``ToolRegistry`` 注册（``build_mac_tools``），不 import 共享层。

危险操作确认不经此层（``dispatch`` 走注入的 ``confirm_fn``）。open_app 不危险
（v0.4 不触发 NSAlert，框架就位见补遗#5）。

v0.8 mac 工具补全（§五映射）：``clipboard``(pbcopy/pbpaste)、``mdfind``
(Spotlight 文件搜索)、``volume``(osascript 音量)、``process``(pkill/killall，
危险)、``lock``(osascript 锁屏，需 Accessibility)、``sleep``(osascript 睡眠，
危险)。危险=``True`` 的经 ``dispatch`` 自动走 ``confirm_dangerous``（NSAlert）。
"""

from __future__ import annotations

import logging
import re
import subprocess

from .tools_schema import ToolContext, ToolHandler, ToolResult, ToolSchema

log = logging.getLogger("pet")

# 程序名：字母数字空格点横点，禁路径分隔符（防 ``open -a "../../xxx"`` 越界）
# v0.8.1：禁纯点号串（".." "." "..."，防 open -a .. 报错/意外行为）
_APP_NAME = re.compile(r"^(?![.\s]+$)[A-Za-z0-9 .\-]+$")
# URL：仅 http/https，防 file://、smb:// 等本机协议
_HTTP_URL = re.compile(r"^https?://", re.IGNORECASE)
# mac 进程名：字母数字空格点横加号下划线（无 .exe 后缀；可含空格如 "Google Chrome"）
# 禁纯点号串；禁路径分隔符（防 pkill 越界名注入）
_PROC_NAME = re.compile(r"^(?![.\s]+$)[A-Za-z0-9 .+_-]+$")
# 关键进程硬拒（NSAlert 都不让过——杀掉会登出/内核恐慌/系统失能）
_PROC_DENYLIST = {
    "loginwindow", "WindowServer", "launchd", "kernel_task",
    "init", "PID1", "kernel",
}
# mdfind 结果上限（§五 文件数上限，防一次性灌爆 DS 上下文）
_MDFIND_CAP = 20

OPEN_APP_SCHEMA = ToolSchema(
    name="open_app",
    description=(
        "打开一个 macOS 应用程序或网页。用于满足用户'打开 Safari'"
        "'打开浏览器'等请求。传 app（应用名，如 Safari）或 url（http(s) 网址）。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "app": {
                "type": "string",
                "description": "应用程序名，如 Safari、访达、计算器",
            },
            "url": {
                "type": "string",
                "description": "要打开的 http(s) 网址",
            },
        },
        # 二选一；不强制都给（additionalProperties False 防 DS 塞别的）
        "additionalProperties": False,
    },
    dangerous=False,
    # 批次A（REVIEW-2026-08-28 H4/F4）：url 可被注入驱向钓鱼页，按参数确认
    dangerous_when=lambda a: bool((a.get("url") or "").strip()),
)


class OpenAppHandler:
    """``open -a`` / ``open URL``。"""

    def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        app = (args.get("app") or "").strip()
        url = (args.get("url") or "").strip()
        if app and url:
            return ToolResult(False, "app 与 url 二选一，不要同时给。")
        if app:
            if not _APP_NAME.match(app):
                return ToolResult(False, f"应用名不合法: {app!r}")
            argv = ["open", "-a", app]
            label = app
        elif url:
            if not _HTTP_URL.match(url):
                return ToolResult(False, "仅支持 http/https 网址。")
            argv = ["open", url]
            label = url
        else:
            return ToolResult(False, "需要 app 或 url 参数。")

        try:
            subprocess.run(argv, check=True, capture_output=True, timeout=10)
        except subprocess.CalledProcessError as e:
            stderr = (e.stderr or b"").decode(errors="replace").strip()
            log.warning("open_app 失败 %s: %s", label, stderr)
            return ToolResult(False, f"打开 {label} 失败: {stderr or '未知错误'}")
        except subprocess.TimeoutExpired:
            return ToolResult(False, f"打开 {label} 超时。")
        log.info("open_app 打开 %s", label)
        return ToolResult(True, f"已打开 {label}。", data={"target": label})


# ================= v0.8 mac 工具补全 =================


def _run(argv: list[str], *, timeout: float = 10.0,
         input_text: str | None = None) -> tuple[bool, str]:
    """跑子进程，返回 (returncode==0, stdout/err 文本)。

    argv 列表传入不经 shell（无命令注入面）；统一 capture + utf-8 容错解码。
    """
    try:
        r = subprocess.run(
            argv, input=input_text, capture_output=True, text=True,
            timeout=timeout, encoding="utf-8", errors="replace",
        )
        out = (r.stdout or r.stderr or "").strip()
        return (r.returncode == 0, out)
    except subprocess.TimeoutExpired:
        return (False, "命令超时")
    except OSError as e:
        return (False, str(e))


def cap_results(paths: list[str], limit: int = _MDFIND_CAP) -> list[str]:
    """文件搜索结果封顶（§五 文件数上限）。纯函数便于单元测。"""
    return [p for p in paths if p and p.strip()][:limit]


# ---- Accessibility 探测（锁屏前置；ctypes 直取 AXIsProcessTrusted，
# 内联不耦合 mouse_lock_mac，免拉 Quartz/CGEventTap 依赖）----

_AX_LIB = None


def _ax_trusted() -> bool:
    """Accessibility 是否授权（``AXIsProcessTrusted``）。查询失败返 False
    （fail-closed：未确认授权就不发锁屏 keystroke）。"""
    global _AX_LIB
    try:
        import ctypes

        if _AX_LIB is None:
            _AX_LIB = ctypes.CDLL(
                "/System/Library/Frameworks/ApplicationServices.framework/"
                "ApplicationServices"
            )
            _AX_LIB.AXIsProcessTrusted.restype = ctypes.c_bool
            _AX_LIB.AXIsProcessTrusted.argtypes = []
        return bool(_AX_LIB.AXIsProcessTrusted())
    except Exception:
        log.warning("AXIsProcessTrusted 查询失败", exc_info=True)
        return False


# ---------- clipboard ----------

CLIPBOARD_SCHEMA = ToolSchema(
    name="clipboard",
    description=(
        "读写 macOS 剪贴板。action=set 时需 text（写入文本）；"
        "action=get 返回当前剪贴板文本。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["set", "get"]},
            "text": {"type": "string", "description": "set 时要写入的文本"},
        },
        "required": ["action"],
        "additionalProperties": False,
    },
    # 批次A（REVIEW-2026-08-28 H5）：剪贴板敏感内容回灌 LLM/覆盖用户数据，
    # 读写都过确认框（与 win 对齐）；并补 L10 text_fields——mac 端此前缺失，
    # 含 ".." 字样的正常文本会被黑名单误拒（双端行为分叉）
    dangerous=True,
    text_fields=("text",),
)


class ClipboardHandler:
    """``pbcopy``（写，stdin 灌文本）/ ``pbpaste``（读）。"""

    def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        action = args.get("action")
        if action == "set":
            text = args.get("text")
            if not isinstance(text, str) or not text:
                return ToolResult(False, "set 需要 text 参数。")
            ok, out = _run(["pbcopy"], input_text=text)
            if not ok:
                return ToolResult(False, f"写剪贴板失败: {out}")
            log.info("clipboard set %d 字符", len(text))
            return ToolResult(True, "已复制到剪贴板。")
        if action == "get":
            ok, out = _run(["pbpaste"])
            if not ok:
                return ToolResult(False, f"读剪贴板失败: {out}")
            return ToolResult(
                True, out or "(剪贴板为空)", data={"text": out}
            )
        return ToolResult(False, "action 只能是 set 或 get。")


# ---------- mdfind ----------

MDFIND_SCHEMA = ToolSchema(
    name="mdfind",
    description=(
        "用 Spotlight 搜索文件，返回最多 20 条路径。pattern 为文件名或"
        "内容关键词（如 报告 *.pdf invoice）；scope 可选子目录名"
        "（如 Desktop/Documents/Downloads），缺省搜整个用户目录。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "pattern": {"type": "string",
                        "description": "文件名或关键词，如 *.pdf report"},
            "scope": {"type": "string",
                      "description": "子目录名，缺省整个用户目录"},
        },
        "required": ["pattern"],
        "additionalProperties": False,
    },
    dangerous=False,
)

# pattern/scope 禁 shell 元与路径分隔符（argv 直传无注入面，但仍防越界名）
_QUERY_BAD = set('<>|:$"\\/')
_SCOPE_BAD = set('<>|:$"\\/') | {".."}


class MdfindHandler:
    """``mdfind -onlyin <root> <pattern>``，根限定用户目录（防全盘扫）。"""

    def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        import os

        pattern = (args.get("pattern") or "").strip()
        scope = (args.get("scope") or "").strip()
        if not pattern or any(c in pattern for c in _QUERY_BAD):
            return ToolResult(False, f"pattern 不合法: {pattern!r}")
        root = os.path.expanduser("~")
        if scope:
            if any(c in scope for c in _SCOPE_BAD):
                return ToolResult(False, f"scope 不合法: {scope!r}")
            root = os.path.join(root, scope)
            if not os.path.isdir(root):
                return ToolResult(False, f"目录不存在: {scope}")
        ok, out = _run(
            ["mdfind", "-onlyin", root, pattern], timeout=15.0
        )
        if not ok:
            return ToolResult(False, f"搜索失败: {out}")
        files = cap_results(out.splitlines())
        if not files:
            return ToolResult(True, f"没找到匹配 {pattern} 的文件。")
        body = "找到：\n" + "\n".join(files)
        if len(out.splitlines()) > _MDFIND_CAP:
            body += f"\n（仅前 {_MDFIND_CAP} 条，更多结果请缩小范围）"
        return ToolResult(True, body, data={"files": files})


# ---------- volume ----------

VOLUME_SCHEMA = ToolSchema(
    name="volume",
    description=(
        "读/设置系统主音量（0-100）或静音开关。"
        "action=get 返回当前音量；action=set 需 percent(0-100)；"
        "action=mute 需 on(true/false)。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["get", "set", "mute"]},
            "percent": {"type": "number"},
            "on": {"type": "boolean"},
        },
        "required": ["action"],
        "additionalProperties": False,
    },
    dangerous=False,
)


class VolumeHandler:
    """``osascript`` 调 ``set volume`` / ``get volume settings``。"""

    def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        action = args.get("action")
        try:
            if action == "get":
                ok, out = _run(
                    ["osascript", "-e",
                     "output volume of (get volume settings)"]
                )
                return ToolResult(ok, out if ok else f"读取失败: {out}",
                                  data={"volume": out} if ok else {})
            if action == "set":
                p = args.get("percent")
                if not isinstance(p, (int, float)) or not 0 <= p <= 100:
                    return ToolResult(False, "percent 需为 0-100。")
                ok, out = _run(
                    ["osascript", "-e",
                     f"set volume output volume {int(round(p))}"]
                )
                return ToolResult(ok, out if ok else f"设置失败: {out}")
            if action == "mute":
                on = args.get("on")
                if not isinstance(on, bool):
                    return ToolResult(False, "on 需为 true/false。")
                flag = "true" if on else "false"
                ok, out = _run(
                    ["osascript", "-e", f"set volume output muted {flag}"]
                )
                return ToolResult(ok, out if ok else f"静音失败: {out}")
        except Exception as e:
            log.warning("volume osascript 失败: %s", e, exc_info=True)
            return ToolResult(False, f"音量接口失败: {e}")
        return ToolResult(False, "action 只能是 get/set/mute。")


# ---------- process（危险）----------

PROCESS_SCHEMA = ToolSchema(
    name="process",
    description=(
        "结束 macOS 进程。name=进程名（如 Safari、Spotify）或 pid=数字。"
        "危险操作，会先弹确认框。关键系统进程一律拒绝。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "pid": {"type": "number"},
        },
        "additionalProperties": False,
    },
    dangerous=True,
)


class ProcessHandler:
    """``pkill -x``（精确名匹配）/ ``kill -PID``。"""

    def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        name = (args.get("name") or "").strip()
        pid = args.get("pid")
        if pid is not None:
            try:
                pid_i = int(pid)
                argv = ["kill", str(pid_i)]
                label = f"PID {pid_i}"
            except (TypeError, ValueError):
                return ToolResult(False, "pid 需为数字。")
        elif name:
            if not _PROC_NAME.match(name):
                return ToolResult(False, f"进程名不合法: {name!r}")
            if name in _PROC_DENYLIST:
                return ToolResult(
                    False, f"拒绝结束关键系统进程 {name}（会登出/失能）。"
                )
            argv = ["pkill", "-x", name]
            label = name
        else:
            return ToolResult(False, "需要 name 或 pid。")
        try:
            r = subprocess.run(
                argv, capture_output=True, text=True, timeout=10,
                encoding="utf-8", errors="replace",
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            return ToolResult(False, f"进程操作失败: {e}")
        out = (r.stdout or r.stderr or "").strip()
        # pkill 无匹配返非 0（No matching processes）；kill 信号 0 返 0
        ok = r.returncode == 0
        log.info("process %s -> rc=%d %s", label, r.returncode, out[:80])
        return ToolResult(
            ok, out or (f"已结束 {label}" if ok else f"未找到 {label}")
        )


# ---------- lock（需 Accessibility）----------

LOCK_SCHEMA = ToolSchema(
    name="lock",
    description=(
        "锁屏（Ctrl+Cmd+Q）。需要辅助功能（Accessibility）权限，"
        "未授权时返回明确提示。"
    ),
    parameters={"type": "object", "properties": {},
                "additionalProperties": False},
    dangerous=False,
)


class LockHandler:
    """``osascript`` 发 Ctrl+Cmd+Q（锁屏快捷键）。"""

    _SCRIPT = (
        'tell application "System Events" to keystroke "q" '
        "using {control down, command down}"
    )

    def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        if not _ax_trusted():
            return ToolResult(
                False,
                "需要辅助功能权限（系统设置→隐私与安全性→辅助功能，"
                "开启 Desktop-Pet）。",
            )
        ok, out = _run(["osascript", "-e", self._SCRIPT])
        if not ok:
            return ToolResult(
                False, f"锁屏失败: {out or '（可能缺少自动化权限）'}"
            )
        log.info("lock 锁屏")
        return ToolResult(True, "已锁屏。")


# ---------- sleep（危险）----------

SLEEP_SCHEMA = ToolSchema(
    name="sleep",
    description="让电脑进入睡眠（危险操作，会先弹确认框）。",
    parameters={"type": "object", "properties": {},
                "additionalProperties": False},
    dangerous=True,
)


class SleepHandler:
    """``osascript`` 调 ``tell System Events to sleep``。"""

    def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        ok, out = _run(
            ["osascript", "-e",
             'tell application "System Events" to sleep']
        )
        if not ok:
            return ToolResult(
                False, f"睡眠失败: {out or '（可能缺少自动化权限）'}"
            )
        log.info("sleep 睡眠")
        return ToolResult(True, "已发起睡眠。")


def build_mac_tools() -> list[tuple[ToolSchema, ToolHandler]]:
    """mac 工具注册清单（供 app 喂 ToolRegistry.register）。

    v0.4 open_app；v0.8 补全 clipboard/mdfind/volume/process/lock/sleep
    （§五映射：pbcopy/pbpaste、mdfind、osascript 音量、pkill/kill、
    osascript 锁屏/睡眠）。危险=``True`` 的经 dispatch 自动走 NSAlert 确认。
    """
    return [
        (OPEN_APP_SCHEMA, OpenAppHandler()),
        (CLIPBOARD_SCHEMA, ClipboardHandler()),
        (MDFIND_SCHEMA, MdfindHandler()),
        (VOLUME_SCHEMA, VolumeHandler()),
        (PROCESS_SCHEMA, ProcessHandler()),
        (LOCK_SCHEMA, LockHandler()),
        (SLEEP_SCHEMA, SleepHandler()),
    ]
