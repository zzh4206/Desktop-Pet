"""win 平台工具实现 —— 平台适配与分工.md §六工具映射。

v0.4 ``open_app``（os.startfile 样板）；
v0.8 补全（§六映射）：``clipboard``(clip/Get-Clipboard)、``file_search``
(Get-ChildItem 限定用户目录)、``volume``(ctypes 直调 CoreAudio)、
``process``(taskkill，危险)、``sleep``(SetSuspendState，危险)。
危险确认不经此层（dispatch 走注入的 confirm_fn，框架 v0.4.14 就位）。
"""

from __future__ import annotations

import logging
import os
import re
import subprocess

from .tools_schema import ToolContext, ToolHandler, ToolResult, ToolSchema

log = logging.getLogger("pet")

# 程序名：字母数字空格点横点，禁路径分隔符（防 "../../xxx" 越界）
# v0.8.1：禁纯点号串（".." "."，防 os.startfile("..") 弹上级目录 Explorer）
_APP_NAME = re.compile(r"^(?![.\s]+$)[A-Za-z0-9 .\-]+$")
# URL：仅 http/https，防 file:// 等本机协议
_HTTP_URL = re.compile(r"^https?://", re.IGNORECASE)
# 进程名：xxx.exe（taskkill /IM 格式）
_PROC_NAME = re.compile(r"^[A-Za-z0-9 _\-.]+\.exe$", re.IGNORECASE)
# 批次A/F6（REVIEW-2026-08-28）：系统关键进程硬拒——explorer/dwm 一杀黑屏，
# csrss/winlogon/lsass 等系统进程任务管理器都不该碰。mac 端早有
# _PROC_DENYLIST，win 端此前只靠确认框（一键"继续"即杀），双端对齐。
_PROC_DENYLIST = frozenset({
    "explorer.exe", "dwm.exe", "csrss.exe", "winlogon.exe", "lsass.exe",
    "services.exe", "svchost.exe", "smss.exe", "wininit.exe", "logonui.exe",
    "sihost.exe", "ctfmon.exe", "fontdrvhost.exe", "searchhost.exe",
    "shellexperiencehost.exe", "startmenuexperiencehost.exe",
})
# 批次F/L12（REVIEW-2026-09-04）：open_app 系统程序黑名单——_APP_NAME 放行
# "cmd"/"regedit" 等（regex 禁 / 无参数面，危害限开窗口，但提示注入可借
# 记忆通道诱导开系统程序，process 有 denylist 而 open_app 无=防护不对称）
_APP_DENYLIST = frozenset({
    "cmd", "cmd.exe", "powershell", "powershell.exe", "pwsh", "pwsh.exe",
    "regedit", "regedit.exe", "reg", "reg.exe", "taskmgr", "taskmgr.exe",
    "msconfig", "msconfig.exe", "diskpart", "diskpart.exe", "shutdown",
    "shutdown.exe", "vssadmin", "vssadmin.exe", "bcdedit", "bcdedit.exe",
    "netsh", "netsh.exe", "schtasks", "schtasks.exe", "sc", "sc.exe",
    "wscript", "wscript.exe", "cscript", "cscript.exe", "mshta", "mshta.exe",
    "rundll32", "rundll32.exe", "control", "control.exe",
})

OPEN_APP_SCHEMA = ToolSchema(
    name="open_app",
    description=(
        "打开一个 Windows 应用程序或网页。用于满足用户'打开记事本'"
        "'打开浏览器'等请求。传 app（应用名，如 notepad、calc）或"
        " url（http(s) 网址）。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "app": {
                "type": "string",
                "description": "应用程序名，如 notepad、calc、explorer",
            },
            "url": {
                "type": "string",
                "description": "要打开的 http(s) 网址",
            },
        },
        "additionalProperties": False,
    },
    # 批次A（REVIEW-2026-08-28 H4/F4）：url 可被注入驱向钓鱼页（含经记忆
    # 通道的存储型注入），与纯 app 名不同，按参数走确认框
    dangerous=False,
    dangerous_when=lambda a: bool((a.get("url") or "").strip()),
)


class OpenAppHandler:
    """``os.startfile``（ShellExecute：注册应用名 / URL / 文档均可）。"""

    def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        app = (args.get("app") or "").strip()
        url = (args.get("url") or "").strip()
        if app and url:
            return ToolResult(False, "app 与 url 二选一，不要同时给。")
        if app:
            if not _APP_NAME.match(app):
                return ToolResult(False, f"应用名不合法: {app!r}")
            # L12：系统程序黑名单（大小写归一，含 .exe 形态）
            if app.lower() in _APP_DENYLIST:
                return ToolResult(
                    False,
                    f"出于安全考虑不能代开系统程序 {app}，请自行从开始菜单打开。",
                )
            target = app
        elif url:
            if not _HTTP_URL.match(url):
                return ToolResult(False, "仅支持 http/https 网址。")
            target = url
        else:
            return ToolResult(False, "需要 app 或 url 参数。")

        try:
            os.startfile(target)  # noqa: S606（已过白名单校验）
        except OSError as e:
            log.warning("open_app 失败 %s: %s", target, e)
            return ToolResult(
                False,
                f"打开 {target} 失败: {e}（未注册的应用名？）",
            )
        log.info("open_app 打开 %s", target)
        return ToolResult(True, f"已打开 {target}。", data={"target": target})


# ================= v0.8 win 工具补全 =================


def _ps_run(args: list, timeout: float = 10.0) -> tuple[bool, str]:
    """跑 PowerShell，返回 (ok, stdout/err)。

    批次C/M2（REVIEW-2026-08-31）：命令前缀强制 UTF-8 输出——PS 5.1 管道
    输出默认系统 OEM 代码页（zh-CN=936），旧版按 utf-8 解码致中文路径/
    剪贴板内容全成替换符（本机实证）。``errors="replace"`` 保留兜底。
    """
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "[Console]::OutputEncoding=[Text.Encoding]::UTF8;", *args],
            capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
        )
        return (r.returncode == 0, (r.stdout or r.stderr or "").strip())
    except subprocess.TimeoutExpired:
        return (False, "PowerShell 超时")


CLIPBOARD_SCHEMA = ToolSchema(
    name="clipboard",
    description=(
        "读写 Windows 剪贴板。action=set 时需 text（写入文本）；"
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
    # 批次A（REVIEW-2026-08-28 H5）：剪贴板常含密码/token 等敏感内容，
    # get 整段回灌 LLM=直送第三方 API、set 覆盖用户数据——读写都过确认框
    dangerous=True,
    # L10：text 是任意用户文本载荷，跳过路径/命令黑名单扫描
    text_fields=("text",),
)


class ClipboardHandler:
    def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        action = args.get("action")
        if action == "set":
            text = args.get("text")
            if not isinstance(text, str) or not text:
                return ToolResult(False, "set 需要 text 参数。")
            # 批次C/M2（REVIEW-2026-08-31）：弃 clip.exe——clip 按 OEM
            # 代码页读 stdin，UTF-8 写入的中文进剪贴板即乱码（本机实证
            # "中文剪贴板测试"→"中文剪贴板测?"）。改 PowerShell：
            # InputEncoding=UTF8 后从 stdin 读全文写剪贴板。
            try:
                r = subprocess.run(
                    ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                     "[Console]::InputEncoding=[Text.Encoding]::UTF8; "
                     "[Console]::In.ReadToEnd() | Set-Clipboard"],
                    input=text, text=True, timeout=10, encoding="utf-8",
                    errors="replace", capture_output=True,
                )
                if r.returncode != 0:
                    return ToolResult(
                        False, f"写剪贴板失败: {(r.stderr or '').strip()[:120]}")
            except (OSError, subprocess.TimeoutExpired) as e:
                return ToolResult(False, f"写剪贴板失败: {e}")
            log.info("clipboard set %d 字符", len(text))
            return ToolResult(True, "已复制到剪贴板。")
        if action == "get":
            ok, out = _ps_run(["Get-Clipboard"])
            if not ok:
                return ToolResult(False, f"读剪贴板失败: {out}")
            return ToolResult(True, out or "(剪贴板为空)",
                              data={"text": out})
        return ToolResult(False, "action 只能是 set 或 get。")


FILE_SEARCH_SCHEMA = ToolSchema(
    name="file_search",
    description=(
        "在用户目录（或其子目录，如 Desktop/Documents/Downloads）按名"
        "搜文件，返回最多 20 条路径。例：pattern=*.txt scope=Desktop。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "pattern": {"type": "string",
                        "description": "文件名通配符，如 *.pdf report*"},
            "scope": {"type": "string",
                      "description": "子目录名，缺省整个用户目录"},
        },
        "required": ["pattern"],
        "additionalProperties": False,
    },
    dangerous=False,
)

_PATTERN_BAD = set("<>|:$'\"")
_SCOPE_BAD = set("<>|:$'\"/\\") | {".."}


def _ps_sq(s: str) -> str:
    """PowerShell 单引号字符串转义（批次A/H6）：``'`` → ``''``。

    root 取自 expanduser("~")（Windows 用户名可含 ``'``，如 O'Brien），
    pattern/scope 是 LLM 可控串——任何一处裸 ``'`` 都会提前闭合引号，
    让后续 -Filter 内容脱离引号上下文成为独立语句（任意命令执行）。
    pattern 虽已禁 ``'``，这里对三处统一转义做纵深防御。
    """
    return s.replace("'", "''")


class FileSearchHandler:
    """Get-ChildItem -Recurse -Filter，根限定 %USERPROFILE%（防全盘扫）。"""

    def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        pattern = (args.get("pattern") or "").strip()
        scope = (args.get("scope") or "").strip()
        if not pattern or any(c in pattern for c in _PATTERN_BAD):
            return ToolResult(False, f"pattern 不合法: {pattern!r}")
        root = os.path.expanduser("~")
        if scope:
            if any(c in scope for c in _SCOPE_BAD):
                return ToolResult(False, f"scope 不合法: {scope!r}")
            root = os.path.join(root, scope)
            if not os.path.isdir(root):
                return ToolResult(False, f"目录不存在: {scope}")
        ok, out = _ps_run(
            [f"Get-ChildItem -Path '{_ps_sq(root)}' -Recurse "
             f"-Filter '{_ps_sq(pattern)}' "
             f"-File -ErrorAction SilentlyContinue "
             f"| Select-Object -First 20 -ExpandProperty FullName"],
            timeout=15.0,
        )
        if not ok:
            return ToolResult(False, f"搜索失败: {out}")
        files = [line for line in out.splitlines() if line.strip()]
        if not files:
            return ToolResult(True, f"没找到匹配 {pattern} 的文件。")
        return ToolResult(True, "找到：\n" + "\n".join(files),
                          data={"files": files[:20]})


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


def _volume_com(action: str, percent=None, mute=None):
    """ctypes 直调 CoreAudio IAudioEndpointVolume（免 PowerShell 嵌 C#）。

    返回 (ok, info)。COM vtable 手工调用：枚举器 idx4
    GetDefaultAudioEndpoint；设备 idx3 Activate；端点 SetScalar idx7 /
    GetScalar idx9 / SetMute idx14。
    """
    import ctypes
    import uuid
    from ctypes import POINTER, byref, c_float, c_int, c_void_p

    class _GUID(ctypes.Structure):
        _fields_ = [("d", ctypes.c_ubyte * 16)]

        @classmethod
        def from_str(cls, s):
            g = cls()
            g.d[:] = (ctypes.c_ubyte * 16).from_buffer_copy(
                uuid.UUID(s).bytes_le
            )
            return g

    CLSID_ENUM = _GUID.from_str(
        "{BCDE0395-E52F-467C-8E3D-C4579291692E}")   # MMDeviceEnumerator
    IID_ENUM = _GUID.from_str(
        "{A95664D2-9614-4F35-A746-DE8DB63617E6}")
    IID_ENDPT = _GUID.from_str(
        "{5CDF2C82-841E-4546-9722-0CF74078229A}")   # IAudioEndpointVolume

    def call_as(obj, idx, argtypes, *argv):
        # obj → *(void**)obj = vtable 地址 → vtable[idx] = 函数地址
        vtbl_addr = ctypes.cast(obj, POINTER(c_void_p)).contents.value
        entries = ctypes.cast(
            ctypes.c_void_p(vtbl_addr), POINTER(c_void_p * 32)
        ).contents
        fn = ctypes.WINFUNCTYPE(c_int, c_void_p, *argtypes)(entries[idx])
        return fn(obj, *argv)

    ole32 = ctypes.oledll.ole32
    ole32.CoInitializeEx(None, 2)   # APARTMENTTHREADED（幂等）
    try:
        enum = c_void_p()
        ole32.CoCreateInstance(
            byref(CLSID_ENUM), None, 23, byref(IID_ENUM), byref(enum))
        device = c_void_p()
        hr = call_as(enum, 4, (c_int, c_int, POINTER(c_void_p)),
                     0, 1, byref(device))
        if hr != 0:
            return (False, f"GetDefaultAudioEndpoint hr={hr:#x}")
        endvol = c_void_p()
        hr = call_as(device, 3, (POINTER(_GUID), c_void_p, c_int,
                                 POINTER(c_void_p)),
                     byref(IID_ENDPT), None, 1, byref(endvol))
        if hr != 0:
            return (False, f"Activate hr={hr:#x}")
        if action == "set":
            hr = call_as(endvol, 7, (c_float, c_void_p),
                         float(percent) / 100.0, None)
            # hr=1(S_FALSE)=已是目标值，同 0 计成功
            return (hr in (0, 1),
                    f"hr={hr:#x}" if hr not in (0, 1)
                    else f"音量已设为 {percent:.0f}%")
        if action == "mute":
            hr = call_as(endvol, 14, (c_int, c_void_p),
                         1 if mute else 0, None)
            return (hr in (0, 1),
                    f"hr={hr:#x}" if hr not in (0, 1)
                    else ("已静音" if mute else "已取消静音"))
        level = c_float()
        hr = call_as(endvol, 9, (POINTER(c_float), c_void_p),
                     byref(level), None)
        if hr != 0:
            return (False, f"GetScalar hr={hr:#x}")
        return (True, f"当前音量 {level.value * 100:.0f}%")
    finally:
        ole32.CoUninitialize()


class VolumeHandler:
    def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        action = args.get("action")
        try:
            if action == "get":
                ok, info = _volume_com("get")
                return ToolResult(ok, info)
            if action == "set":
                p = args.get("percent")
                if not isinstance(p, (int, float)) or not 0 <= p <= 100:
                    return ToolResult(False, "percent 需为 0-100。")
                ok, info = _volume_com("set", percent=float(p))
                return ToolResult(ok, info)
            if action == "mute":
                on = args.get("on")
                if not isinstance(on, bool):
                    return ToolResult(False, "on 需为 true/false。")
                ok, info = _volume_com("mute", mute=on)
                return ToolResult(ok, info)
        except Exception as e:
            log.warning("volume COM 调用失败: %s", e, exc_info=True)
            return ToolResult(False, f"音量接口失败: {e}")
        return ToolResult(False, "action 只能是 get/set/mute。")


PROCESS_SCHEMA = ToolSchema(
    name="process",
    description=(
        "结束 Windows 进程。name=进程名(如 notepad.exe)或 pid=数字。"
        "危险操作，会先弹确认框。"
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
    def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        name = (args.get("name") or "").strip()
        pid = args.get("pid")
        argv = ["taskkill", "/F"]
        if pid is not None:
            try:
                pid_i = int(pid)
            except (TypeError, ValueError):
                return ToolResult(False, "pid 需为数字。")
            # 批次E/M5（REVIEW-2026-08-31）：pid 路径也过硬拒名单——
            # 解析进程名再比对（denylist 的设计意图是"确认框都不让过"，
            # 旧版 pid 路径确认框即直达 taskkill）
            okn, pname = _ps_run(
                [f"(Get-Process -Id {pid_i}).ProcessName"])
            if okn and pname:
                if (pname.strip().lower() + ".exe") in _PROC_DENYLIST:
                    return ToolResult(
                        False,
                        f"{pname.strip()} 是系统关键进程，不允许通过宠物结束。")
            argv += ["/PID", str(pid_i)]
            label = f"PID {pid_i}"
        elif name:
            if not _PROC_NAME.match(name):
                return ToolResult(False, f"进程名不合法: {name!r}")
            if name.lower() in _PROC_DENYLIST:
                return ToolResult(
                    False, f"{name} 是系统关键进程，不允许通过宠物结束。")
            argv += ["/IM", name]
            label = name
        else:
            return ToolResult(False, "需要 name 或 pid。")
        try:
            r = subprocess.run(argv, capture_output=True, text=True,
                               timeout=10, encoding="utf-8", errors="replace")
        except (OSError, subprocess.TimeoutExpired) as e:
            return ToolResult(False, f"taskkill 失败: {e}")
        out = (r.stdout or r.stderr or "").strip()
        ok = r.returncode == 0
        log.info("process kill %s -> %s", label, out[:80])
        return ToolResult(ok, out or ("已结束 " + label if ok else "失败"))


SLEEP_SCHEMA = ToolSchema(
    name="sleep",
    description="让电脑进入睡眠（危险操作，会先弹确认框）。",
    parameters={"type": "object", "properties": {},
                "additionalProperties": False},
    dangerous=True,
)


class SleepHandler:
    def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        try:
            # SetSuspendState(0=睡眠)；shutdown /h 为休眠(慢/占盘)不用
            subprocess.run(
                ["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"],
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            return ToolResult(False, f"睡眠失败: {e}")
        return ToolResult(True, "已发起睡眠。")


def build_win_tools() -> list[tuple[ToolSchema, ToolHandler]]:
    """win 工具注册清单（供 app 喂 ToolRegistry.register）。

    v0.4: open_app；v0.8 补全 clipboard/file_search/volume/process/sleep
    （§六映射：clip/Windows搜索/COM音量/taskkill/SetSuspendState）。
    """
    return [
        (OPEN_APP_SCHEMA, OpenAppHandler()),
        (CLIPBOARD_SCHEMA, ClipboardHandler()),
        (FILE_SEARCH_SCHEMA, FileSearchHandler()),
        (VOLUME_SCHEMA, VolumeHandler()),
        (PROCESS_SCHEMA, ProcessHandler()),
        (SLEEP_SCHEMA, SleepHandler()),
    ]
