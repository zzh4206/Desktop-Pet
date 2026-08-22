"""v0.9 记忆管理桥接（QML `PetMem 1.0` 上下文对象）。

Mem.items / Mem.count / Mem.refresh() / Mem.forget(id) / Mem.clear()。
UI 删除后立即从 store 移除（recall 源即 store——删后不再注入，Must）。
"""

from __future__ import annotations

import logging
import os

from PySide6.QtCore import Property, QObject, Signal, Slot

_log = logging.getLogger("pet")


class MemBridge(QObject):
    itemsChanged = Signal()
    countChanged = Signal()

    def __init__(self, store, save_fn, parent=None) -> None:
        super().__init__(parent)
        self._store = store
        self._save = save_fn   # callable() 落盘（app 注入 debounce save）
        self._items: list = []
        self.refresh()

    @Property("QVariantList", notify=itemsChanged)
    def items(self):
        return self._items

    @Property(int, notify=countChanged)
    def count(self):
        return len(self._store)

    @Slot()
    def refresh(self) -> None:
        self._items = [
            {
                "id": m["id"],
                "fact": m["fact"],
                "importance": round(m["importance"], 2),
                "recall_count": m.get("recall_count", 0),
            }
            for m in self._store.all()
        ]
        self.itemsChanged.emit()
        self.countChanged.emit()

    @Slot(str)
    def forget(self, mem_id: str) -> None:
        self._store.forget(mem_id)
        self._save()
        self.refresh()

    @Slot()
    def clear(self) -> None:
        self._store.clear()
        self._save()
        self.refresh()


_SINGLETON_REGISTERED = False


def register_mem_singleton(bridge: "MemBridge") -> None:
    """注册 ``PetMem 1.0`` singleton（注入 bridge 实例）。

    必须在任何 ``QQmlApplicationEngine`` 创建前调用——PySide6 6.10 下
    engine 创建后再注册的新 singleton 不会被后续 engine 解析，表现为
    mem.qml 里 ``Text`` 无法挂进 ``ColumnLayout.data``（报
    "Cannot assign QQuickText to list property data; expected QObject"）。
    idempotent：重复调用 no-op（防 lazy 路径二次注册）。"""
    global _SINGLETON_REGISTERED
    if _SINGLETON_REGISTERED:
        return
    from PySide6.QtQml import qmlRegisterSingletonInstance

    qmlRegisterSingletonInstance(MemBridge, "PetMem", 1, 0, "Mem", bridge)
    _SINGLETON_REGISTERED = True


def load_mem_qml() -> tuple:
    """载入记忆管理 QML（singleton 需已注册）。返回 (engine, window|None)。"""
    from PySide6.QtQml import QQmlApplicationEngine

    qml_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "mem.qml"
    )
    engine = QQmlApplicationEngine()
    engine.load(qml_path)
    if not engine.rootObjects():
        _log.error("QML 记忆页载入失败: %s", qml_path)
        return (engine, None)
    return (engine, engine.rootObjects()[0])


def load_mem_panel(store, save_fn) -> tuple:
    """载入记忆管理 QML。返回 (engine, window|None, bridge)。

    兼容一次性载入（建 bridge + 注册 singleton + 载入）。app 主路径已
    预注册时 register_mem_singleton no-op——故主路径应直接用
    register_mem_singleton + load_mem_qml 复用同一 bridge，不走本函数。"""
    bridge = MemBridge(store, save_fn)
    register_mem_singleton(bridge)
    engine, win = load_mem_qml()
    return (engine, win, bridge)
