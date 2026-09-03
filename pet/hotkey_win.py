"""win 全局热键（v0.11）—— 平台适配与分工.md §六。

持久热键线程（独立于吃鼠标钩子线程）：
- **Ctrl+Alt+P** → 唤出/隐藏聊天面板
- **Ctrl+Alt+T** → 强制吐出（吃鼠标时释放；平时 no-op 幂等）
- 注册失败（被占用）→ 冲突检测，日志 warning + 回调通知上层气泡提示改键
- config `hotkeys.chat/spit` 可自定义（v0.11 版本规划 Must）

热键字符串解析："ctrl+alt+p" → (MOD_CONTROL|MOD_ALT, VK_P)。
支持修饰 ctrl/alt/shift/win + 字母数字键。
平台库隔离：ctypes 只进本 ``_win`` 文件（+ platform.py 注入点）。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import logging
import threading
import time

_log = logging.getLogger("pet")

_user32 = ctypes.WinDLL("user32", use_last_error=True)

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012

_ID_CHAT = 1
_ID_SPIT = 2

_MOD_MAP = {
    "ctrl": MOD_CONTROL, "control": MOD_CONTROL,
    "alt": MOD_ALT, "option": MOD_ALT,
    "shift": MOD_SHIFT, "win": MOD_WIN, "meta": MOD_WIN,
    # H2 修（REVIEW-2026-08-25）：config.example 的 hotkeys 是跨平台 mac 键位
    # "cmd+option+p/t"，深合并后 win 端恒读到——cmd 在 win 解释为 ctrl
    # （option 已映射 alt），mac 风格串在 win 即 Ctrl+Alt+P/T，与文档一致。
    "cmd": MOD_CONTROL, "command": MOD_CONTROL,
}

_VK_MAP = {}
for _i, _c in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZ"):
    _VK_MAP[_c] = 0x41 + _i       # 大写
    _VK_MAP[_c.lower()] = 0x41 + _i  # 小写（parse 统一 lower 后查）
for _d in range(10):
    _VK_MAP[str(_d)] = 0x30 + _d


_user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int,
                                   wintypes.UINT, wintypes.UINT]
_user32.RegisterHotKey.restype = wintypes.BOOL
_user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
_user32.UnregisterHotKey.restype = wintypes.BOOL
_user32.GetMessageW.argtypes = [
    ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT,
]
_user32.GetMessageW.restype = wintypes.BOOL
_user32.PostThreadMessageW.argtypes = [
    wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
]
_user32.PostThreadMessageW.restype = wintypes.BOOL

_kernel32 = ctypes.WinDLL("kernel32")
_kernel32.GetCurrentThreadId.restype = wintypes.DWORD


def parse_hotkey(s: str) -> tuple:
    """解析 "ctrl+alt+p" → (mods, vk)；不合法返 (0, 0)。

    批次J/L6（REVIEW-2026-08-31）：必须含至少一个修饰键——裸字母
    （如 config 误写 "p"）会注册全局单键热键，挡住正常打字。"""
    parts = [p.strip().lower() for p in (s or "").split("+") if p.strip()]
    if not parts:
        return (0, 0)
    mods, vk = 0, 0
    for p in parts:
        if p in _MOD_MAP:
            mods |= _MOD_MAP[p]
        elif p in _VK_MAP:
            vk = _VK_MAP[p]
        else:
            return (0, 0)
    return (mods, vk) if (vk and mods) else (0, 0)


class _HotkeySignalBridge:
    """M7 修：热键线程回调经 Qt 信号转发主线程（跨线程 GUI 是 UB）。

    HotkeyManager 保持纯 Python（无 Qt 依赖，测试友好）；app 在 start()
    后用 bridge 的信号 connect 到实际 handler。
    M12 修（REVIEW-2026-08-25）：注册冲突回调（热键线程内触发）也编码为
    ``conflict(name, key_str)`` 信号——旧版直调 on_conflict（app 侧
    bubble.show 跨线程碰 Qt 未定义行为）。
    """

    def __init__(self) -> None:
        from PySide6.QtCore import QObject, Signal

        class _Sig(QObject):
            fired = Signal(int)       # hotkey id
            conflict = Signal(str, str)   # M12: (名称, 键串) 注册冲突

        self._obj = _Sig()

    @property
    def fired(self):
        return self._obj.fired

    @property
    def conflict(self):
        return self._obj.conflict


class HotkeyManager:
    """持久全局热键线程（chat/spit）；注册失败检测+回调通知。"""

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._callbacks = {}    # id → callable
        self._active = False
        self._lock = threading.Lock()
        self._bridge = None  # M7: Qt Signal 桥（app 注入后回调走信号）

    @property
    def active(self) -> bool:
        return self._active

    def start(self, chat_key: str, spit_key: str,
              on_chat, on_spit, on_conflict=None, bridge=None) -> bool:
        """注册热键并启动消息循环。

        chat_key/spit_key 形如 "ctrl+alt+p"；on_conflict(name, key) 在
        注册失败时回调（气泡提示改键）。返 True 至少一个注册成功。
        """
        with self._lock:
            if self._active:
                return True  # 幂等（已在跑）
            self._callbacks = {
                _ID_CHAT: on_chat,
                _ID_SPIT: on_spit,
            }
            self._keys = {  # 解析好的 (mods, vk)
                _ID_CHAT: parse_hotkey(chat_key),
                _ID_SPIT: parse_hotkey(spit_key),
            }
            # 原始键串（日志/冲突提示用——旧版硬编码 "ctrl+alt+p/t"，
            # 用户改键或 cmd 别名解析后提示与实际不符）
            self._key_strs = {_ID_CHAT: chat_key, _ID_SPIT: spit_key}
            self._on_conflict = on_conflict
            self._bridge = bridge
            # M11 修（REVIEW-2026-08-25）：_ready/_reg_ok 必须在 start() 前
            # 创建——旧版 Event 在 start() 后建，子线程先跑到 _ready.set()
            # 则主线程 AttributeError；_reg_ok 由子线程写入，主线程 wait
            # 超时后读未初始化属性同样炸（_loop 内不再重建 _reg_ok）。
            # 批次F/L15（REVIEW-2026-09-04）：同时清陈旧 _thread_id——
            # stop→start 复用时旧死线程 id 残留，stop 抢在新线程首行赋值
            # 前投递 WM_QUIT 会打到死线程（改键场景旧键残留）
            self._thread_id = 0
            self._ready = threading.Event()
            self._reg_ok = {}
            self._thread = threading.Thread(
                target=self._loop, daemon=True, name="global-hotkey",
            )
            self._thread.start()
            # 等线程就绪
            self._ready.wait(timeout=2.0)
            self._active = any(v != (0, 0) and ok
                               for v, ok in self._reg_ok.items())
            return self._active

    def stop(self) -> None:
        """注销热键 + 结束线程（shutdown 用；幂等）。

        批次E/L2（REVIEW-2026-08-28）：线程 id 由子线程首行写入——stop
        早于赋值时旧版 PostThreadMessageW(0,...) 静默失败，线程永远停在
        GetMessage，join(timeout=2.0) 把 shutdown 卡满 2s。先短暂等 id
        就位再投递退出消息。"""
        with self._lock:
            if not self._active and not self._thread:
                return
            self._active = False
        if self._thread is not None and self._thread.is_alive():
            deadline = time.monotonic() + 0.5
            while not self._thread_id and time.monotonic() < deadline:
                time.sleep(0.01)   # 子线程首行就会写 id，微秒级
        if self._thread_id:
            _user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        if self._thread:
            self._thread.join(timeout=2.0)

    def _loop(self) -> None:
        """热键线程体：注册 → GetMessage 循环 → 清理。"""
        self._thread_id = _kernel32.GetCurrentThreadId()
        # _reg_ok 已在 start() 内预创建（M11），此处只填结果

        for hid, (mods, vk) in self._keys.items():
            if (mods, vk) == (0, 0):
                self._reg_ok[hid] = False
                _log.warning("[热键] 无效组合 id=%d", hid)
                continue
            ok = _user32.RegisterHotKey(None, hid, mods, vk)
            self._reg_ok[hid] = bool(ok)
            key_str = self._key_strs.get(hid, "?")
            if not ok:
                _log.warning("[热键] %s 注册失败(被占用?) err=%s",
                             key_str, ctypes.get_last_error())
                if self._on_conflict:
                    name = "聊天" if hid == _ID_CHAT else "吐出"
                    # M12 修：经 bridge 信号转主线程（旧版热键线程直调
                    # on_conflict → bubble.show 跨线程 GUI 未定义行为）
                    if self._bridge is not None:
                        self._bridge.conflict.emit(name, key_str)
                    else:
                        try:
                            self._on_conflict(name, key_str)
                        except Exception:
                            pass
            else:
                _log.info("[热键] %s 注册成功 (%s)",
                          "聊天" if hid == _ID_CHAT else "吐出", key_str)

        self._ready.set()

        msg = wintypes.MSG()
        while _user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY:
                hid = int(msg.wParam)
                if self._bridge is not None:
                    self._bridge.fired.emit(hid)   # M7: 经信号转主线程
                else:
                    cb = self._callbacks.get(hid)
                    if cb:
                        try:
                            cb()
                        except Exception:
                            _log.warning("[热键] 回调异常", exc_info=True)
            elif msg.message == WM_QUIT:
                break

        for hid in self._keys:
            _user32.UnregisterHotKey(None, hid)


# ---- v0.11 自启（注册表 Run） ----

_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_APP_NAME = "DesktopPet"
import winreg


def set_autostart(enabled: bool, exe_path: str = "") -> bool:
    """写/删 HKCU Run 键。enabled=False → 删。"""
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY,
                             0, winreg.KEY_SET_VALUE)
        if enabled:
            import sys

            if not exe_path:
                exe_path = sys.executable
                # python app.py → 加引号
                import os

                script = os.path.abspath(
                    os.path.join(os.path.dirname(__file__), "..", "app.py")
                )
                winreg.SetValueEx(key, _APP_NAME, 0, winreg.REG_SZ,
                                  f'"{exe_path}" "{script}"')
            else:
                winreg.SetValueEx(key, _APP_NAME, 0, winreg.REG_SZ,
                                  f'"{exe_path}"')
        else:
            try:
                winreg.DeleteValue(key, _APP_NAME)
            except FileNotFoundError:
                pass
        winreg.CloseKey(key)
        _log.info("[自启] %s", "已启用" if enabled else "已关闭")
        return True
    except OSError as e:
        _log.warning("[自启] 注册表操作失败: %s", e)
        return False


def is_autostart_enabled() -> bool:
    """读 HKCU Run 键是否已写入。"""
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY,
                             0, winreg.KEY_READ)
        val, _ = winreg.QueryValueEx(key, _APP_NAME)
        winreg.CloseKey(key)
        return bool(val)
    except (FileNotFoundError, OSError):
        return False
