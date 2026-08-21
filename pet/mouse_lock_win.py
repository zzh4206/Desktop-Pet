"""win 平台鼠标抑制（吃鼠标）—— 平台适配与分工.md §六 / 安全六铁律。

与 ``mouse_lock_mac`` 对齐的 ``MouseLockWin``（W2 Spike 结论落地）：

- **WH_MOUSE_LL 低级钩子**：专用线程安装 + 消息循环（钩子要求泵消息）。
  抑制 move/按下/抬起（含拖动即 move+down 组合）；**滚轮放行**（mac 同样
  只吞 move/down/up/dragged）。键盘完全不挂键盘钩子——铁律1"只抑制鼠标"
  的物理保证（win 侧连 listen-only 键盘 tap 都不需要：热键走
  RegisterHotKey 系统级，钩子崩了热键也活着，纵深防御同 mac）。
- **钉光标**：每次吞掉 move 时把光标 SetCursorPos 回锚点（对注入类输入
  的双保险，与工作表 W2 描述一致）。
- **强制吐出热键 Ctrl+Alt+T**：RegisterHotKey 在钩子线程注册，WM_HOTKEY
  → force_spit（避开 Ctrl+Alt+Del；与 mac Cmd+Option+T 对齐 T=吐）。
- **看门狗先于抑制启动**（铁律2/6 同一机制）：daemon 线程记 deadline
  （duration 钳 [0.3,15]），每 0.25s 轮询到点强制 _release——独立于主逻辑，
  主线程崩溃也按 deadline 释放。
- **_release 幂等**：active 立即置 False + UnhookWindowsHookEx（可跨线程调）
  + UnregisterHotKey + WM_QUIT 结束钩子线程；force_spit/看门狗/托盘/
  shutdown 全走它。
- **UIPI 降级**（工作表 v0.7 win 项）：前台窗口进程 OpenProcess 被拒
  （ERROR_ACCESS_DENIED=管理员）→ start 返 False 不抑制，上层走气泡路径。

平台库隔离：ctypes 只进本 ``_win`` 文件（+ 注入点 platform.py）。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import logging
import threading
import time

_log = logging.getLogger("pet")

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# M6 修：TokenElevation UIPI 判定用
_advapi32 = ctypes.WinDLL("advapi32")
_advapi32.OpenProcessToken.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE),
]
_advapi32.OpenProcessToken.restype = wintypes.BOOL
_advapi32.GetTokenInformation.argtypes = [
    wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
]
_advapi32.GetTokenInformation.restype = wintypes.BOOL

# ---- 常量 ----
WH_MOUSE_LL = 14
WM_QUIT = 0x0012
WM_HOTKEY = 0x0312
MOD_ALT, MOD_CONTROL = 0x0001, 0x0002
_VK_T = 0x54                       # 'T' = 吐（与 mac Cmd+Option+T 对齐）
_HOTKEY_ID = 0xB07                 # 任意非零
_WM_SPIT = 0x8000 + 1              # PostThreadMessage 模拟热键（测试用）

_MOUSE_SUPPRESS = {                # 抑制集合（滚轮 0x020A/0x020E 放行）
    0x0200,  # WM_MOUSEMOVE（含拖动）
    0x0201, 0x0202, 0x0203,        # 左 按下/抬起/双击
    0x0204, 0x0205,                # 右 按下/抬起
    0x0207, 0x0208,                # 中 按下/抬起
    0x0209, 0x020B,                # X1/X2 按下/抬起
}

_DURATION_MIN, _DURATION_MAX = 0.3, 15.0   # 铁律2：单次锁定 ≤15s
_WATCHDOG_POLL_S = 0.25

_LLMOUSEPROC = ctypes.WINFUNCTYPE(
    ctypes.c_long, ctypes.c_int, ctypes.c_size_t, ctypes.c_void_p
)

_user32.SetWindowsHookExW.argtypes = [
    ctypes.c_int, _LLMOUSEPROC, wintypes.HINSTANCE, wintypes.DWORD,
]
_user32.SetWindowsHookExW.restype = ctypes.c_void_p
_user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
_user32.UnhookWindowsHookEx.restype = wintypes.BOOL
_user32.CallNextHookEx.argtypes = [
    ctypes.c_void_p, ctypes.c_int, ctypes.c_size_t, ctypes.c_void_p,
]
_user32.CallNextHookEx.restype = ctypes.c_long
_user32.GetMessageW.argtypes = [
    ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT,
]
_user32.GetMessageW.restype = wintypes.BOOL
_user32.PostThreadMessageW.argtypes = [
    wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
]
_user32.PostThreadMessageW.restype = wintypes.BOOL
_user32.RegisterHotKey.argtypes = [
    wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT,
]
_user32.RegisterHotKey.restype = wintypes.BOOL
_user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
_user32.UnregisterHotKey.restype = wintypes.BOOL
_user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
_user32.SetCursorPos.restype = wintypes.BOOL
_user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
_user32.GetCursorPos.restype = wintypes.BOOL
_user32.GetForegroundWindow.restype = wintypes.HWND
_user32.GetWindowThreadProcessId.argtypes = [
    wintypes.HWND, ctypes.POINTER(wintypes.DWORD),
]
_kernel32.OpenProcess.restype = ctypes.c_void_p
_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
_kernel32.GetModuleHandleW.restype = wintypes.HMODULE
_kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def _foreground_elevated() -> bool:
    """UIPI 判定：前台进程令牌是否提升（TokenElevation）→ True。

    M6 修：旧版 OpenProcess(LIMITED) 对管理员进程通常也成功，
    err=5 几乎不命中→门禁形同虚设。改用 GetTokenInformation
    (TokenElevation) 跨完整性级别可靠读取。
    """
    hwnd = _user32.GetForegroundWindow()
    if not hwnd:
        return False
    pid = wintypes.DWORD()
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return False
    h = _kernel32.OpenProcess(
        _PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value
    )
    if not h:
        return False  # 打不开就当非提升（保守不拦）
    try:
        token = wintypes.HANDLE()
        if not _advapi32.OpenProcessToken(
            h, 0x0008, ctypes.byref(token)   # TOKEN_QUERY
        ):
            return False
        try:
            buf = wintypes.DWORD()
            ret_len = wintypes.DWORD()
            ok = _advapi32.GetTokenInformation(
                token, 20, ctypes.byref(buf), 4, ctypes.byref(ret_len)
            )
            return bool(ok and buf.value != 0)
        finally:
            _kernel32.CloseHandle(token)
    finally:
        _kernel32.CloseHandle(h)


class MouseLockWin:
    """WH_MOUSE_LL 鼠标抑制（对齐 MouseLockMac 接口）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active = False
        self._hook = None            # 钩子句柄（钩子线程装）
        self._hook_thread: threading.Thread | None = None
        self._hook_thread_id = 0
        self._cb_ref = None          # 防 GC（回调必须存活）
        self._anchor = wintypes.POINT(0, 0)
        self._watchdog: threading.Thread | None = None
        self._deadline = 0.0

    # ---- 对外接口（与 mac 对齐） ----

    @property
    def active(self) -> bool:
        return self._active

    def start(self, duration_s: float) -> bool:
        """开始抑制。duration 钳 [0.3,15]（铁律2）；UIPI 前台提升 → False。

        看门狗先于钩子启动（铁律6：即便钩子线程起不来，看门狗也会在
        deadline 强制走一遍 _release 的幂等清理）。
        """
        with self._lock:
            if self._active:
                # 已在吃：仅顺延 deadline（幂等续期）
                self._deadline = time.monotonic() + self._clamp(duration_s)
                return True
            if _foreground_elevated():
                _log.warning("[吃鼠标] 前台为管理员窗口(UIPI)，降级只气泡")
                return False
            dur = self._clamp(duration_s)
            # 钉光标锚点 = 当前位置
            _user32.GetCursorPos(ctypes.byref(self._anchor))
            # 看门狗先启动
            self._deadline = time.monotonic() + dur
            self._watchdog = threading.Thread(
                target=self._watchdog_loop, daemon=True,
                name="eat-mouse-watchdog",
            )
            self._watchdog.start()
            # 钩子线程
            self._ready = threading.Event()
            self._hook_ok = True
            self._hook_thread = threading.Thread(
                target=self._hook_loop, daemon=True,
                name="eat-mouse-hook",
            )
            self._hook_thread.start()
            self._ready.wait(timeout=1.0)   # 等钩子装好（或失败）
            if not self._hook_ok:
                self._active = False
                return False
            self._active = True
            _log.info("[吃鼠标] 抑制开始 duration=%.1fs 热键=Ctrl+Alt+T", dur)
            return True

    def force_spit(self) -> None:
        """强制吐出（热键/托盘/看门狗/shutdown 共用；幂等）。"""
        self._release("force_spit")

    # ---- 内部 ----

    @staticmethod
    def _clamp(d: float) -> float:
        return min(_DURATION_MAX, max(_DURATION_MIN, float(d)))

    def _release(self, reason: str) -> None:
        with self._lock:
            if not self._active:
                return  # 幂等：inactive 上调不崩不做事
            self._active = False
            hook, tid = self._hook, self._hook_thread_id
            self._hook = None
        if hook:
            _user32.UnhookWindowsHookEx(hook)
        if tid:
            # 结束钩子线程消息循环（线程退出时自身也 UnregisterHotKey 兜底）
            _user32.PostThreadMessageW(tid, WM_QUIT, 0, 0)
        _log.info("[吃鼠标] 释放(%s)", reason)

    def _watchdog_loop(self) -> None:
        """铁律2+6：到点强制释放；独立于主逻辑。"""
        while True:
            time.sleep(_WATCHDOG_POLL_S)
            with self._lock:
                if not self._active:
                    return
                expired = time.monotonic() >= self._deadline
            if expired:
                self._release("看门狗超时")
                return

    # 钩子线程体（消息循环 + LL 回调 + 热键）
    def _hook_loop(self) -> None:
        self._hook_thread_id = _kernel32.GetCurrentThreadId()
        outer = self

        @_LLMOUSEPROC
        def _on_mouse(n_code, w_param, _l_param):
            if n_code >= 0 and w_param in _MOUSE_SUPPRESS and outer._active:
                # 钉光标（对注入类输入的双保险）
                _user32.SetCursorPos(outer._anchor.x, outer._anchor.y)
                return 1  # 吞掉（不 CallNextHookEx）
            return _user32.CallNextHookEx(
                outer._hook, n_code, w_param, _l_param
            )

        self._cb_ref = _on_mouse
        hmod = _kernel32.GetModuleHandleW(None)
        self._hook = _user32.SetWindowsHookExW(
            WH_MOUSE_LL, _on_mouse, hmod, 0
        )
        if not self._hook:
            _log.error("[吃鼠标] SetWindowsHookEx 失败 err=%s",
                       ctypes.get_last_error())
            self._hook_ok = False
            self._ready.set()
            return
        self._ready.set()
        # M2 修：热键注册统一走 HotkeyManager（v0.11 持久线程），此处
        # 不再自注册（双重注册必然冲突，旧版每次吃鼠标刷 warning）。
        # 钩子线程只泵消息（LL 回调 + WM_QUIT + 测试注入 _WM_SPIT）。
        msg = wintypes.MSG()
        while _user32.GetMessageW(
            ctypes.byref(msg), None, 0, 0
        ) > 0:
            if msg.message == _WM_SPIT:      # 测试注入（热键经 HotkeyManager）
                self.force_spit()
            elif msg.message == WM_QUIT:
                break
        # 线程退出兜底清理
        if self._hook:
            _user32.UnhookWindowsHookEx(self._hook)
            self._hook = None


_kernel32.GetCurrentThreadId.restype = wintypes.DWORD


def get_mouse_lock_win() -> MouseLockWin:
    """单例（平台工厂用；mac 同款惰性单例语义）。"""
    global _SINGLETON
    if _SINGLETON is None:
        _SINGLETON = MouseLockWin()
    return _SINGLETON


_SINGLETON: MouseLockWin | None = None
