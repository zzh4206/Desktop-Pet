"""状态板桥接（QML ``PetStatus 1.0`` 上下文对象，QAbstractListModel）。

把桌宠内部不可见状态暴露成模型供 QML 渲染，用于诊断与可视化。每行 dict：
``{"type":"section"|"field", "name":..., "value":..., "level":"ok"|"warn"|"bad"}``，
对应角色 type/name/value/level。

数据经注入的 ``get_rows(dev_mode)`` callable（app 侧方法）按需拉取——
bridge 不 import 业务模块。``refresh()`` 只对变化行发 ``dataChanged``（行数
不变时），ListView 滚动位置不重置；仅切换开发模式导致行数变化才全量 reset。
"""

from __future__ import annotations

import logging
import os

from PySide6.QtCore import (
    Property,
    QAbstractListModel,
    QModelIndex,
    QObject,
    Qt,
    Signal,
    Slot,
)

_log = logging.getLogger("pet")


class StatusBridge(QAbstractListModel):
    """QML 侧：``Status`` 即模型，``Status.refresh()`` / ``Status.devMode``。"""

    _TypeRole = Qt.UserRole + 1
    _NameRole = Qt.UserRole + 2
    _ValueRole = Qt.UserRole + 3
    _LevelRole = Qt.UserRole + 4

    devModeChanged = Signal()

    def __init__(self, get_rows, parent=None) -> None:
        super().__init__(parent)
        self._get_rows = get_rows   # callable(dev_mode: bool) -> list[dict]
        self._rows: list = []
        self._dev_mode = False

    # ---- QAbstractListModel ----
    def roleNames(self):
        return {
            self._TypeRole: b"type",
            self._NameRole: b"name",
            self._ValueRole: b"value",
            self._LevelRole: b"level",
        }

    def rowCount(self, parent=QModelIndex()) -> int:
        return len(self._rows)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid() or not (0 <= index.row() < len(self._rows)):
            return None
        row = self._rows[index.row()]
        if role == self._TypeRole:
            return row.get("type", "field")
        if role == self._NameRole:
            return row.get("name", "")
        if role == self._ValueRole:
            return row.get("value", "")
        if role == self._LevelRole:
            return row.get("level", "ok")
        return None

    # ---- 开发模式（行为/传感器默认隐藏，开开关后展示） ----
    @Property(bool, notify=devModeChanged)
    def devMode(self) -> bool:
        return self._dev_mode

    @devMode.setter
    def devMode(self, value: bool) -> None:
        value = bool(value)
        if value != self._dev_mode:
            self._dev_mode = value
            self.devModeChanged.emit()
            self.refresh()

    @Slot(bool)
    def setDevMode(self, value: bool) -> None:
        self.devMode = value

    @Slot()
    def refresh(self) -> None:
        rows: list = []
        try:
            if self._get_rows is not None:
                rows = self._get_rows(self._dev_mode) or []
        except Exception as exc:  # 诊断工具自身绝不崩
            _log.warning("[状态板] 拉取失败: %s", exc)
            rows = [{"type": "field", "name": "状态板", "value": f"读取失败: {exc}", "level": "bad"}]
        self._set_rows(rows)

    def _set_rows(self, rows: list) -> None:
        old = self._rows
        # 行数变化（切开发模式）→ 全量 reset；此后滚动从顶开始（可接受）
        if len(rows) != len(old):
            self.beginResetModel()
            self._rows = rows
            self.endResetModel()
            return
        # 行数不变：就地 diff，仅 dataChanged 变化行，滚动位置保留
        roles = [self._TypeRole, self._NameRole, self._ValueRole, self._LevelRole]
        for i, (new, cur) in enumerate(zip(rows, old)):
            if new != cur:
                self._rows[i] = new
                idx = self.index(i)
                self.dataChanged.emit(idx, idx, roles)


_SINGLETON_REGISTERED = False


def register_status_singleton(bridge: "StatusBridge") -> None:
    """注册 ``PetStatus 1.0`` singleton（注入 bridge 实例）。

    与 mem/perm 同约束：必须在任何 ``QQmlApplicationEngine`` 创建前调用。
    idempotent：重复调用 no-op。
    """
    global _SINGLETON_REGISTERED
    if _SINGLETON_REGISTERED:
        return
    from PySide6.QtQml import qmlRegisterSingletonInstance

    qmlRegisterSingletonInstance(StatusBridge, "PetStatus", 1, 0, "Status", bridge)
    _SINGLETON_REGISTERED = True


def load_status_qml() -> tuple:
    """载入状态板 QML（singleton 需已注册）。返回 (engine, window|None)。"""
    from PySide6.QtQml import QQmlApplicationEngine

    qml_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "status.qml"
    )
    engine = QQmlApplicationEngine()
    engine.load(qml_path)
    if not engine.rootObjects():
        _log.error("QML 状态板载入失败: %s", qml_path)
        return (engine, None)
    return (engine, engine.rootObjects()[0])
