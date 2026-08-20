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
        from pet.mouse_lock_win import MouseLockWin

        lk = MouseLockWin()
        ok = lk.start(0.3)   # 0.3s 最短锁定，看门狗即刻回收
        if ok:
            lk.force_spit()
            return (True, "")
        return (False, "钩子安装失败(UIPI/系统限制?)")

    def _check_hotkey(self):
        import ctypes

        u = ctypes.WinDLL("user32")
        # 独占注册探测：成功即注销（真实热键由吃鼠标期间注册）
        ok = u.RegisterHotKey(None, 0xB08, 0x3, 0x54)  # Ctrl+Alt+T
        if ok:
            u.UnregisterHotKey(None, 0xB08)
            return (True, "")
        return (False, "热键被占用，建议在设置中改键")

    def _check_clipboard(self):
        from pet.tools_win import ClipboardHandler
        from pet.tools_schema import ToolContext

        ctx = ToolContext(pet_state=None, user_name="u", config={},
                          window_info=None)
        r = ClipboardHandler().execute(
            {"action": "set", "text": "perm-check"}, ctx)
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


def load_perm_panel(adapter) -> tuple:
    """载入权限自检 QML。返回 (engine, window|None)。"""
    from PySide6.QtQml import QQmlApplicationEngine, qmlRegisterSingletonInstance

    qml_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "perm.qml"
    )
    bridge = PermBridge(adapter)
    qmlRegisterSingletonInstance(PermBridge, "PetPerm", 1, 0, "Perm", bridge)
    engine = QQmlApplicationEngine()
    engine.load(qml_path)
    if not engine.rootObjects():
        _log.error("QML 权限页载入失败: %s", qml_path)
        return (engine, None, bridge)
    return (engine, engine.rootObjects()[0], bridge)
