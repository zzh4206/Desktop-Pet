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

from PySide6.QtCore import Property, QObject, QThread, Signal, Slot

_log = logging.getLogger("pet")


class _PermCheckWorker(QThread):
    """M8 修（REVIEW-2026-08-25）：自检项后台执行（与 win perm_bridge 同款）。

    旧版 refresh 在主线程同步跑全部检查（osascript Automation 探测最长
    5s + keyring），启动与"重新检测"都冻结 UI。检查函数为无状态只读
    探测，跨线程安全；结果经 done 信号回主线程更新 items。"""

    done = Signal(object)   # list[dict{name,ok,detail}]

    def __init__(self, checks: list, parent=None) -> None:
        super().__init__(parent)
        self._checks = checks   # [(name, fn)]

    def run(self) -> None:
        items = []
        for name, fn in self._checks:
            try:
                ok, detail = fn()
            except Exception as exc:  # 自检自身不许崩
                _log.warning("[权限自检] %s 异常: %s", name, exc)
                ok, detail = False, str(exc)[:60]
            items.append({"name": name, "ok": ok, "detail": detail or "-"})
        self.done.emit(items)


class PermBridgeMac(QObject):
    """QML 侧：``Perm.items``（list[dict{name,ok,detail}]）+ ``Perm.note``
    + ``Perm.refresh()`` / ``Perm.open_settings()``。"""

    itemsChanged = Signal()
    noteChanged = Signal()

    def __init__(self, adapter, parent=None) -> None:
        super().__init__(parent)
        self._adapter = adapter
        self._items: list = []
        self._worker: _PermCheckWorker | None = None
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
        """触发自检（M8：后台线程执行；上一轮在飞时忽略重复请求）。"""
        if self._worker is not None and self._worker.isRunning():
            return
        checks = [
            ("辅助功能 Accessibility（吃鼠标/锁屏）",
             self._check_accessibility),
            ("自动化 Automation（控制其他 App）",
             self._check_automation),
            ("钥匙串 Keychain（DS key 密钥库）",
             self._check_keychain),
            ("数据目录可写", self._check_paths),
            ("DS key 已设", self._check_ds_key),
        ]
        self._worker = _PermCheckWorker(checks, parent=self)
        self._worker.done.connect(self._on_checks_done)
        # 线程对象挂 parent=self（C++ 生命周期归桥管）——done 送达时线程
        # 可能仍在收尾，Python 引用先丢会触发"销毁运行中 QThread"的原生
        # 崩溃（win 实测段错误）；删除只走 finished→deleteLater 单通道
        self._worker.finished.connect(self._worker.deleteLater)
        self._worker.start()

    @Slot(object)
    def _on_checks_done(self, items: list) -> None:
        self._items = items
        self.itemsChanged.emit()
        self._worker = None

    @Slot()
    def open_settings(self) -> None:
        """深链到系统设置「隐私与安全性 → 辅助功能」。"""
        try:
            self._adapter.prompt_accessibility()
        except Exception as exc:  # 深链失败不崩
            _log.warning("[权限自检] 打开设置失败: %s", exc)

    # ---- 各项检测（worker 线程执行，须无状态只读）----

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


_SINGLETON_REGISTERED = False


def register_perm_singleton(bridge: "PermBridgeMac") -> None:
    """注册 ``PetPerm 1.0`` singleton（注入 bridge 实例）。

    必须在任何 ``QQmlApplicationEngine`` 创建前调用——PySide6 6.10 下
    engine 创建后再注册的新 singleton 不被后续 engine 解析，perm.qml
    报 "Cannot assign QQuickText to list property data; expected QObject"。
    idempotent：重复调用 no-op。"""
    global _SINGLETON_REGISTERED
    if _SINGLETON_REGISTERED:
        return
    from PySide6.QtQml import qmlRegisterSingletonInstance

    qmlRegisterSingletonInstance(PermBridgeMac, "PetPerm", 1, 0, "Perm", bridge)
    _SINGLETON_REGISTERED = True


def load_perm_qml() -> tuple:
    """载入 mac 权限自检 QML（singleton 需已注册）。返回 (engine, window|None)。"""
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
    """载入 mac 权限自检 QML。返回 (engine, window|None, bridge)。

    兼容一次性载入（建 bridge + 注册 + 载入）。app 主路径已预注册时
    register_perm_singleton no-op——主路径用 register_perm_singleton +
    load_perm_qml 复用同一 bridge。"""
    bridge = PermBridgeMac(adapter)
    register_perm_singleton(bridge)
    engine, win = load_perm_qml()
    if win is None:
        return (engine, None, bridge)
    # 深链按钮接 bridge.open_settings（perm.qml 按钮 onClicked: Perm.open_settings）
    return (engine, win, bridge)
