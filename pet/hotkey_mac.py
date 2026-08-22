"""mac 全局热键（v0.11）—— 平台适配与分工.md §六。

Carbon ``RegisterEventHotKey`` + ``InstallEventHandler``（HIToolbox，经
ctypes 直取——与 mouse_lock_mac 取 ``AXIsProcessTrusted`` 同一思路；pyobjc
Carbon 子包不保证安装，故不依赖）：

- **Cmd+Option+P** → 唤出/隐藏聊天面板（``on_chat``）
- **Cmd+Option+T** → 强制吐出吃鼠标（``on_spit``，未吃时 ``force_spit``
  幂等 no-op）
- 注册失败（已被其他 app 占用）→ ``on_conflict(name, key)`` 冲突回调

线程模型：与 win 的专用热键线程不同——``RegisterEventHotKey`` 事件由系统
派发到注册目标（``GetApplicationEventTarget``）的 run loop，即主 CFRunLoop。
Qt 的 mac 事件循环就是主 CFRunLoop，故热键回调**天然在主线程**触发，跨线程
GUI 问题不存在。``_HotkeySignalBridge`` 仍提供以与 win 对齐（app 注入后走
Qt Signal 派发）；bridge 为 None 时直接调回调（主线程安全）。

v0.7 整合：``mouse_lock_mac`` 原有独立 ``Cmd+Option+T`` 键盘 listen-tap
热键移除，统一走本模块。``RegisterEventHotKey`` 不需 Accessibility（区别于
CGEventTap listen），故吐出热键即便 Accessibility 未授权也能触发；吃鼠标
抑制本身仍需 Accessibility（``mouse_lock_mac`` 不变）。

平台库隔离：ctypes/Carbon 只进本 ``_mac`` 文件（+ platform.py 注入点）。
"""

from __future__ import annotations

import ctypes
import logging
import os
import struct
import threading

_log = logging.getLogger("pet")

# ---- Carbon 事件常量 ----
noErr = 0

# Carbon 修饰键位（Events.h RegisterEventHotKey 用同一组位）
cmdKey = 0x0100
shiftKey = 0x0200
alphaKey = 0x0800     # Option
controlKey = 0x1000


def _four(s: str) -> int:
    """四字符码 → UInt32（大端）。"""
    return struct.unpack(">I", s.encode("ascii"))[0]


kEventClassKeyboard = _four("keyb")
kEventHotKeyPressed = 5
kEventParamDirectObject = _four("----")
typeEventHotKeyID = _four("hkid")
eventAttributeNone = 0

# 应用级 EventHotKeyID 签名（4 字符任意，仅本进程内唯一即可）
_HOTKEY_SIG = _four("dpet")

_ID_CHAT = 1   # 与 win 对齐：1=聊天 / 2=吐出（app._on_hotkey_fired 按此分发）
_ID_SPIT = 2


# ---- kVK_ANSI_* 虚拟键码（HIToolbox/Events.h）----
# 字母非顺序排列，必须显式表。数字亦然。
_VK = {
    "a": 0x00, "s": 0x01, "d": 0x02, "f": 0x03, "h": 0x04, "g": 0x05,
    "z": 0x06, "x": 0x07, "c": 0x08, "v": 0x09, "b": 0x0B,
    "q": 0x0C, "w": 0x0D, "e": 0x0E, "r": 0x0F, "y": 0x10, "t": 0x11,
    "1": 0x12, "2": 0x13, "3": 0x14, "4": 0x15, "6": 0x16, "5": 0x17,
    "9": 0x19, "7": 0x1A, "8": 0x1C, "0": 0x1D,
    "o": 0x1F, "u": 0x20, "i": 0x22, "p": 0x23,
    "l": 0x25, "j": 0x26, "k": 0x28,
    "n": 0x2D, "m": 0x2E,
}

_MOD_MAP = {
    "cmd": cmdKey, "command": cmdKey, "meta": cmdKey,
    "option": alphaKey, "alt": alphaKey,
    "shift": shiftKey,
    "ctrl": controlKey, "control": controlKey,
}


def parse_hotkey(s: str) -> tuple:
    """解析 "cmd+option+p" → (modifier_bits, keycode)；不合法返 (0, 0)。"""
    parts = [p.strip().lower() for p in (s or "").split("+") if p.strip()]
    if not parts:
        return (0, 0)
    mods, code = 0, 0
    for p in parts:
        if p in _MOD_MAP:
            mods |= _MOD_MAP[p]
        elif p in _VK:
            code = _VK[p]
        else:
            return (0, 0)
    return (mods, code) if code else (0, 0)


# ---- Carbon 符号探测 ----
_HAS_CARBON = False
_carbon = None
try:
    _carbon = ctypes.CDLL(
        "/System/Library/Frameworks/Carbon.framework/Carbon"
    )

    class _EventHotKeyID(ctypes.Structure):
        _fields_ = [
            ("signature", ctypes.c_uint32),
            ("id", ctypes.c_uint32),
        ]

    class _EventTypeSpec(ctypes.Structure):
        _fields_ = [
            ("eventClass", ctypes.c_uint32),
            ("eventKind", ctypes.c_uint32),
        ]

    # OSStatus (*)(EventHandlerCallRef, EventRef, void*)
    _EventHandlerProc = ctypes.CFUNCTYPE(
        ctypes.c_int32, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p
    )

    _carbon.GetApplicationEventTarget.argtypes = []
    _carbon.GetApplicationEventTarget.restype = ctypes.c_void_p

    _carbon.InstallEventHandler.argtypes = [
        ctypes.c_void_p,                 # target
        _EventHandlerProc,               # handler
        ctypes.c_uint,                   # numTypes
        ctypes.POINTER(_EventTypeSpec),  # list
        ctypes.c_void_p,                 # userData
        ctypes.POINTER(ctypes.c_void_p), # outRef
    ]
    _carbon.InstallEventHandler.restype = ctypes.c_int32

    _carbon.RemoveEventHandler.argtypes = [ctypes.c_void_p]
    _carbon.RemoveEventHandler.restype = ctypes.c_int32

    _carbon.RegisterEventHotKey.argtypes = [
        ctypes.c_uint32,                 # keyCode
        ctypes.c_uint32,                 # modifiers
        _EventHotKeyID,                  # hotKeyID（按值）
        ctypes.c_void_p,                 # target
        ctypes.c_uint,                   # options
        ctypes.POINTER(ctypes.c_void_p), # outRef
    ]
    _carbon.RegisterEventHotKey.restype = ctypes.c_int32

    _carbon.UnregisterEventHotKey.argtypes = [ctypes.c_void_p]
    _carbon.UnregisterEventHotKey.restype = ctypes.c_int32

    _carbon.GetEventKind.argtypes = [ctypes.c_void_p]
    _carbon.GetEventKind.restype = ctypes.c_uint32

    _carbon.GetEventParameter.argtypes = [
        ctypes.c_void_p,                       # event
        ctypes.c_uint32,                        # name
        ctypes.c_uint32,                        # type
        ctypes.POINTER(ctypes.c_uint32),        # outActualType
        ctypes.c_ulong,                         # bufferSize
        ctypes.POINTER(ctypes.c_ulong),         # outActualSize
        ctypes.c_void_p,                        # outData
    ]
    _carbon.GetEventParameter.restype = ctypes.c_int32

    _HAS_CARBON = True
except Exception:
    _log.warning("Carbon 热键符号不可用，全局热键禁用", exc_info=True)


class _HotkeySignalBridge:
    """与 win 对齐：热键回调经 Qt Signal 派发（主线程）。

    mac 的 Carbon 回调本就在主线程，bridge 非必需；提供以保持平台间接线
    一致——app 注入后走 ``fired.emit(hid)``，未注入则直接调回调。
    """

    def __init__(self) -> None:
        from PySide6.QtCore import QObject, Signal

        class _Sig(QObject):
            fired = Signal(int)       # hotkey id

        self._obj = _Sig()

    @property
    def fired(self):
        return self._obj.fired


class HotkeyManager:
    """Carbon 全局热键（chat/spit）；注册失败检测+回调通知。

    生命周期：
    - ``start(chat_key, spit_key, ...)`` → 装事件处理器 + 注册两键；返
      True=至少一个注册成功。
    - ``stop()`` → 注销 + 移除处理器（shutdown 用；幂等）。
    """

    def __init__(self) -> None:
        self._target: int | None = None
        self._handler_ref = None     # EventHandlerRef（c_void_p，防 GC）
        self._cb = None              # CFUNCTYPE 回调引用（防 GC）
        self._specs = None           # EventTypeSpec 数组引用
        self._refs: dict[int, object] = {}   # hid → EventHotKeyRef(c_void_p)
        self._callbacks: dict[int, object] = {}
        self._on_conflict = None
        self._bridge = None
        self._active = False
        self._lock = threading.Lock()

    @property
    def active(self) -> bool:
        return self._active

    def start(self, chat_key: str, spit_key: str,
              on_chat, on_spit, on_conflict=None, bridge=None) -> bool:
        """注册热键并装事件处理器。

        chat_key/spit_key 形如 "cmd+option+p"。返 True=至少一个注册成功。
        """
        if not _HAS_CARBON:
            _log.warning("[热键] Carbon 不可用，跳过")
            return False
        with self._lock:
            if self._active:
                return True  # 幂等
            self._callbacks = {_ID_CHAT: on_chat, _ID_SPIT: on_spit}
            self._on_conflict = on_conflict
            self._bridge = bridge
            keys = {
                _ID_CHAT: parse_hotkey(chat_key),
                _ID_SPIT: parse_hotkey(spit_key),
            }
            names = {_ID_CHAT: "唤聊天", _ID_SPIT: "吐出"}
            keystrs = {_ID_CHAT: chat_key, _ID_SPIT: spit_key}

            if not self._install_handler():
                return False

            for hid, (mods, code) in keys.items():
                if (mods, code) == (0, 0):
                    _log.warning("[热键] 无效组合 %s", keystrs[hid])
                    continue
                ref = ctypes.c_void_p()
                hid_id = _EventHotKeyID(_HOTKEY_SIG, hid)
                status = _carbon.RegisterEventHotKey(
                    code, mods, hid_id, self._target,
                    eventAttributeNone, ctypes.byref(ref),
                )
                if status != noErr:
                    _log.warning("[热键] %s 注册失败(被占用?) status=%d",
                                 keystrs[hid], status)
                    if self._on_conflict:
                        try:
                            self._on_conflict(names[hid], keystrs[hid])
                        except Exception:
                            pass
                else:
                    self._refs[hid] = ref
                    _log.info("[热键] %s 注册成功 (%s)",
                              names[hid], keystrs[hid])

            self._active = bool(self._refs)
            return self._active

    def _install_handler(self) -> bool:
        """装 kEventHotKeyPressed 处理器于应用事件目标。返 False=装失败。"""
        try:
            self._target = _carbon.GetApplicationEventTarget()
            self._specs = (_EventTypeSpec * 1)(
                _EventTypeSpec(kEventClassKeyboard, kEventHotKeyPressed)
            )
            self._cb = _EventHandlerProc(self._on_hotkey)
            ref = ctypes.c_void_p()
            status = _carbon.InstallEventHandler(
                self._target, self._cb, 1, self._specs,
                None, ctypes.byref(ref),
            )
            if status != noErr:
                _log.warning("[热键] InstallEventHandler 失败 status=%d",
                             status)
                return False
            self._handler_ref = ref
            return True
        except Exception:
            _log.warning("[热键] 装事件处理器异常", exc_info=True)
            return False

    def _on_hotkey(self, _call_ref, event, _user_data) -> int:
        """Carbon 回调（主线程）。读 EventHotKeyID.id 分发。"""
        try:
            if _carbon.GetEventKind(event) != kEventHotKeyPressed:
                return noErr
            hid_id = _EventHotKeyID()
            status = _carbon.GetEventParameter(
                event, kEventParamDirectObject, typeEventHotKeyID,
                None, ctypes.sizeof(_EventHotKeyID),
                None, ctypes.byref(hid_id),
            )
            if status != noErr:
                _log.warning("[热键] GetEventParameter 失败 status=%d",
                             status)
                return noErr
            hid = hid_id.id
            if self._bridge is not None:
                self._bridge.fired.emit(hid)
            else:
                cb = self._callbacks.get(hid)
                if cb:
                    try:
                        cb()
                    except Exception:
                        _log.warning("[热键] 回调异常", exc_info=True)
        except Exception:
            _log.warning("[热键] 处理器异常", exc_info=True)
        return noErr

    def stop(self) -> None:
        """注销热键 + 移除处理器（幂等）。"""
        with self._lock:
            if not self._active and self._handler_ref is None:
                return
            refs = dict(self._refs)
            self._refs.clear()
            handler_ref = self._handler_ref
            self._handler_ref = None
            self._active = False

        for ref in refs.values():
            try:
                _carbon.UnregisterEventHotKey(ref)
            except Exception:
                _log.warning("[热键] Unregister 异常", exc_info=True)
        if handler_ref is not None:
            try:
                _carbon.RemoveEventHandler(handler_ref)
            except Exception:
                _log.warning("[热键] RemoveEventHandler 异常",
                              exc_info=True)


# ---- v0.11 自启（LaunchAgents plist） ----

_LAUNCH_AGENT = "com.zzh4206.desktop-pet"


def _plist_path() -> str:
    home = os.path.expanduser("~")
    return os.path.join(home, "Library", "LaunchAgents",
                        _LAUNCH_AGENT + ".plist")


def set_autostart(enabled: bool, exe_path: str = "") -> bool:
    """写/删 ``~/Library/LaunchAgents/com.zzh4206.desktop-pet.plist``。

    enabled=True → 写 plist（RunAtLoad，ProgramArguments=[venv python, app.py]，
    WorkingDirectory=仓库根）。enabled=False → 删。返 True=成功。
    """
    import plistlib

    path = _plist_path()
    try:
        if enabled:
            import sys

            repo = os.path.dirname(
                os.path.dirname(os.path.abspath(__file__))
            )  # pet/ → 仓库根（app.py 所在）
            script = os.path.join(repo, "app.py")
            exe = exe_path or sys.executable
            os.makedirs(os.path.dirname(path), exist_ok=True)
            plist = {
                "Label": _LAUNCH_AGENT,
                "ProgramArguments": [exe, script],
                "RunAtLoad": True,
                "WorkingDirectory": repo,
            }
            with open(path, "wb") as f:
                plistlib.dump(plist, f)
        else:
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
        _log.info("[自启] %s", "已启用" if enabled else "已关闭")
        return True
    except OSError as e:
        _log.warning("[自启] plist 操作失败: %s", e)
        return False


def is_autostart_enabled() -> bool:
    """plist 存在即已启用。"""
    return os.path.exists(_plist_path())
