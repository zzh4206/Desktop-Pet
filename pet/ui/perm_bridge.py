"""v0.8 win 权限自检桥接（QML `PetPerm 1.0` 上下文对象）。

win 无 Accessibility 等特权概念（平台适配 §六"基本无需特权"）——权限页
做**运行时能力自检**：LL 钩子可装 / 热键可注册 / 剪贴板读写 / 音量 COM /
配置与日志目录可写。共享层零依赖（win 专属文件）。
"""

from __future__ import annotations

import logging
import os

from PySide6.QtCore import Property, QObject, Signal, Slot

_log = logging.getLogger("pet")


class PermBridge(QObject):
    """QML 侧：Perm.items（list[dict{name,ok,detail}]）+ Perm.refresh()。"""

    itemsChanged = Signal()
    noteChanged = Signal()

    def __init__(self, adapter, parent=None) -> None:
        super().__init__(parent)
        self._adapter = adapter
        self._items: list = []
        self._note = "Windows 端无需系统授权；以下为运行时能力自检"
        self.refresh()

    @Property("QString", notify=noteChanged)
    def note(self) -> str:
        return self._note

    @Property("QVariantList", notify=itemsChanged)
    def items(self):
        return self._items

    @Slot()
    def open_settings(self) -> None:
        """win 无系统授权页；no-op（perm.qml 按钮双端共用，win 点了无效）。"""
        pass

    @Slot()
    def refresh(self) -> None:
        self._items = [
            self._check("鼠标抑制钩子（吃鼠标）", self._check_ll_hook),
            self._check("强制吐出热键 Ctrl+Alt+T", self._check_hotkey),
            self._check("剪贴板读写", self._check_clipboard),
            self._check("音量控制（CoreAudio）", self._check_volume),
            self._check("数据目录可写", self._check_paths),
            self._check("DS key（凭据管理器）", self._check_ds_key),
        ]
        self.itemsChanged.emit()

    # ---- 单项包装 ----

    @staticmethod
    def _check(name, fn) -> dict:
        try:
            ok, detail = fn()
        except Exception as exc:  # 自检自身不许崩
            _log.warning("[权限自检] %s 异常: %s", name, exc)
            ok, detail = False, str(exc)[:60]
        return {"name": name, "ok": ok, "detail": detail or "-"}

    # ---- 各项检测 ----

    def _check_ll_hook(self):
        # M4 修：结构性检测——SetWindowsHookEx 后立即 Unhook（不进
        # 消息循环、不吞事件、<10ms 无感；旧版真装 0.3s 冻结鼠标）
        import ctypes
        import ctypes.wintypes as wintypes

        u = ctypes.WinDLL("user32")
        WH_MOUSE_LL = 14
        # 最小回调（不抑制，直透）
        _CB = ctypes.WINFUNCTYPE(
            ctypes.c_long, ctypes.c_int, ctypes.c_size_t, ctypes.c_void_p
        )
        cb_ref = _CB(lambda nc, wp, lp: u.CallNextHookEx(
            None, nc, wp, lp))
        hmod = ctypes.WinDLL("kernel32").GetModuleHandleW(None)
        hook = u.SetWindowsHookExW(WH_MOUSE_LL, cb_ref, hmod, 0)
        if hook:
            u.UnhookWindowsHookEx(hook)
            return (True, "")
        return (False, f"LL 钩子安装失败 err={ctypes.get_last_error()}")

    def _check_hotkey(self):
        # M3 修：不真注册（v0.11 HotkeyManager 已持有 Ctrl+Alt+T，
        # 再注册必失败恒假阴性）——改为查询 HotkeyManager 注册状态
        try:
            mgr = getattr(self._adapter, "_hotkey_mgr", None)
            if mgr is not None and mgr.active:
                # 查具体键的注册结果
                reg_ok = getattr(mgr, "_reg_ok", {})
                chat_ok = reg_ok.get(1, False)   # _ID_CHAT = 1
                spit_ok = reg_ok.get(2, False)   # _ID_SPIT = 2
                if chat_ok and spit_ok:
                    return (True, "")
                detail = []
                if not chat_ok:
                    detail.append("聊天键冲突")
                if not spit_ok:
                    detail.append("吐出键冲突")
                return (False, "；".join(detail))
            return (False, "热键管理器未运行")
        except Exception:
            return (False, "状态查询失败")

    def _check_clipboard(self):
        # M4 修：只读探测（旧版写 "perm-check" 覆盖用户剪贴板数据）
        from pet.tools_win import ClipboardHandler
        from pet.tools_schema import ToolContext

        ctx = ToolContext(pet_state=None, user_name="u", config={},
                          window_info=None)
        r = ClipboardHandler().execute({"action": "get"}, ctx)
        # get 成功即读写通道健康（不修改内容）
        return (r.success, "" if r.success else r.message[:60])

    def _check_volume(self):
        from pet.tools_win import VolumeHandler
        from pet.tools_schema import ToolContext

        ctx = ToolContext(pet_state=None, user_name="u", config={},
                          window_info=None)
        r = VolumeHandler().execute({"action": "get"}, ctx)
        return (r.success, r.message if r.success else r.message[:60])

    def _check_paths(self):
        paths = self._adapter.get_paths()
        try:
            probe = os.path.join(paths["data_dir"], ".perm_probe")
            with open(probe, "w", encoding="utf-8") as f:
                f.write("ok")
            os.remove(probe)
            return (True, "")
        except OSError as e:
            return (False, str(e)[:60])

    def _check_ds_key(self):
        key = self._adapter.get_ds_key()
        return (bool(key), "已设置" if key else "未设置（聊天不可用）")


_SINGLETON_REGISTERED = False


def register_perm_singleton(bridge: "PermBridge") -> None:
    """注册 ``PetPerm 1.0`` singleton（注入 bridge 实例）。

    必须在任何 ``QQmlApplicationEngine`` 创建前调用（PySide6 6.10 后注册
    的 singleton 不被后续 engine 解析）。idempotent：重复调用 no-op。"""
    global _SINGLETON_REGISTERED
    if _SINGLETON_REGISTERED:
        return
    from PySide6.QtQml import qmlRegisterSingletonInstance

    qmlRegisterSingletonInstance(PermBridge, "PetPerm", 1, 0, "Perm", bridge)
    _SINGLETON_REGISTERED = True


def load_perm_qml() -> tuple:
    """载入权限自检 QML（singleton 需已注册）。返回 (engine, window|None)。"""
    from PySide6.QtQml import QQmlApplicationEngine

    qml_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "perm.qml"
    )
    engine = QQmlApplicationEngine()
    engine.load(qml_path)
    if not engine.rootObjects():
        _log.error("QML 权限页载入失败: %s", qml_path)
        return (engine, None)
    return (engine, engine.rootObjects()[0])


def load_perm_panel(adapter) -> tuple:
    """载入权限自检 QML。返回 (engine, window|None, bridge)。

    兼容一次性载入；app 主路径已预注册时 register_perm_singleton no-op，
    主路径用 register_perm_singleton + load_perm_qml 复用同一 bridge。"""
    bridge = PermBridge(adapter)
    register_perm_singleton(bridge)
    engine, win = load_perm_qml()
    return (engine, win, bridge)
