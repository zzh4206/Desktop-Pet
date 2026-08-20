"""v0.8 mac 权限自检桥接（QML ``PetPerm 1.0`` 上下文对象）。

mac 权限页做**系统特权自检**（与 win 运行时能力自检不同）：
Accessibility（吃鼠标/锁屏 keystroke）/ Automation（osascript 控制其他 app）/
Keychain（DS key 密钥库）/ 数据目录可写 / DS key 已设。共享层零依赖
（mac 专属文件；权限探测经 adapter / keyring / osascript，不直 import
mouse_lock_mac 的 Quartz 依赖）。

权限永久拒绝（系统设置里关掉本进程开关）：``_check_accessibility`` 探测
仍返 False，权限页显示"未授权，请到系统设置手动开启"+深链，不反复弹
（AXIsProcessTrusted 不会主动弹授权框，仅查询；首次弹框由 prompt_accessibility
引导，权限页只读不弹）。
"""

from __future__ import annotations

import logging
import os

from PySide6.QtCore import Property, QObject, Signal, Slot

_log = logging.getLogger("pet")


class PermBridgeMac(QObject):
    """QML 侧：``Perm.items``（list[dict{name,ok,detail}]）+ ``Perm.note``
    + ``Perm.refresh()`` / ``Perm.open_settings()``。"""

    itemsChanged = Signal()
    noteChanged = Signal()

    def __init__(self, adapter, parent=None) -> None:
        super().__init__(parent)
        self._adapter = adapter
        self._items: list = []
        self._note = "mac 端权限自检：缺权限项请到系统设置→隐私与安全性手动开启"
        self.refresh()

    @Property("QString", notify=noteChanged)
    def note(self) -> str:
        return self._note

    @Property("QVariantList", notify=itemsChanged)
    def items(self):
        return self._items

    @Slot()
    def refresh(self) -> None:
        self._items = [
            self._check("辅助功能 Accessibility（吃鼠标/锁屏）",
                        self._check_accessibility),
            self._check("自动化 Automation（控制其他 App）",
                        self._check_automation),
            self._check("钥匙串 Keychain（DS key 密钥库）",
                        self._check_keychain),
            self._check("数据目录可写", self._check_paths),
            self._check("DS key 已设", self._check_ds_key),
        ]
        self.itemsChanged.emit()

    @Slot()
    def open_settings(self) -> None:
        """深链到系统设置「隐私与安全性 → 辅助功能」。"""
        try:
            self._adapter.prompt_accessibility()
        except Exception as exc:  # 深链失败不崩
            _log.warning("[权限自检] 打开设置失败: %s", exc)

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

    def _check_accessibility(self):
        ok = self._adapter.is_accessibility_trusted()
        return (ok, "" if ok else "未授权，请到系统设置→隐私与安全性→辅助功能开启")

    def _check_automation(self):
        """Automation 权限探测：发一条无害 osascript 给 System Events，
        返 0=已授权；返非 0（-1743 errAEEventNotPermitted 等）=未授权。

        简化判定：osascript 成功即已授权 System Events 控制（锁屏/睡眠/音量
        osascript 都经此）。首次会触发系统授权弹框，故用最轻查询 ``get
        volume settings``（不改变状态）。"""
        import subprocess

        try:
            r = subprocess.run(
                ["osascript", "-e", "get volume settings"],
                capture_output=True, text=True, timeout=5,
                encoding="utf-8", errors="replace",
            )
            if r.returncode == 0:
                return (True, "")
            return (False, "未授权，请到系统设置→隐私与安全性→自动化")
        except (OSError, subprocess.TimeoutExpired) as e:
            return (False, f"探测失败: {e}")

    def _check_keychain(self):
        try:
            import keyring

            # 探测 keyring 后端可用：读一个不存在的项不应抛（返 None）；
            # 后端损坏/无授权时 set_password 抛 PermissionError 等。
            keyring.get_password("Desktop-Pet", "__perm_probe__")
            return (True, "")
        except Exception as exc:
            return (False, f"钥匙串不可用: {str(exc)[:48]}")

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
    """载入 mac 权限自检 QML。返回 (engine, window|None, bridge)。

    复用共享 ``perm.qml``（经 ``note`` 属性注入平台头部文案）；注册
    ``PetPerm 1.0`` singleton 注入 bridge 实例。
    """
    from PySide6.QtQml import QQmlApplicationEngine, qmlRegisterSingletonInstance

    qml_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "perm.qml"
    )
    bridge = PermBridgeMac(adapter)
    qmlRegisterSingletonInstance(PermBridgeMac, "PetPerm", 1, 0, "Perm", bridge)
    engine = QQmlApplicationEngine()
    engine.load(qml_path)
    if not engine.rootObjects():
        _log.error("QML 权限页载入失败: %s", qml_path)
        return (engine, None, bridge)
    win = engine.rootObjects()[0]
    # 深链按钮接 bridge.open_settings（perm.qml 按钮 onClicked: Perm.open_settings）
    return (engine, win, bridge)
