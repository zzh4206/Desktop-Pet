"""QML 聊天面板桥接 —— 设计思路.md §五 / 版本规划 v0.4。

``ChatBridge(QAbstractListModel)``：messages 走 QAbstractListModel（beginInsertRows
触发 QML ListView 刷新——singleton Property notify 在 PySide6 6.10 不刷新 ListView、
setContextProperty QML 见 null，QAbstractListModel 是唯一可靠方案）。``send(text)``
发起对话，``streamingText`` 是流式占位。markdown→HTML 最小转换（**粗**/*斜*/`code`）。

ChatBridge **不 import requests/keyring/AppKit**——DS 经注入的 ``DeepSeekClient``+
``ChatWorker``，工具经 ``ToolRegistry``，key 经 ``app``。
"""

from __future__ import annotations

import html
import logging
import re
from typing import Optional

from PySide6.QtCore import (
    QAbstractListModel,
    QModelIndex,
    Property,
    Qt,
    Signal,
    Slot,
)
from PySide6.QtQml import QQmlApplicationEngine, qmlRegisterSingletonInstance

log = logging.getLogger("pet")

_BOLD = re.compile(r"\*\*(.+?)\*\*")
_ITALIC = re.compile(r"(?<!\*)\*(?!\*)(.+?)\*(?!\*)")
_CODE = re.compile(r"`(.+?)`")


def _md_to_html(text: str) -> str:
    """最小 markdown→HTML（**粗**/*斜*/`code`/换行），转义防 XSS。"""
    s = html.escape(text)
    s = _CODE.sub(r"<code>\1</code>", s)
    s = _BOLD.sub(r"<b>\1</b>", s)
    s = _ITALIC.sub(r"<i>\1</i>", s)
    s = s.replace("\n", "<br>")
    return s


class ChatBridge(QAbstractListModel):
    """QML ↔ DS 桥。messages 走 QAbstractListModel（insertRows 刷新 ListView）。"""

    _RoleRole = Qt.UserRole + 1
    _ContentRole = Qt.UserRole + 2
    _RichRole = Qt.UserRole + 3

    streamingChanged = Signal()
    offlineRequested = Signal()
    failedReply = Signal(str)

    def __init__(self, client, registry, make_ctx, parent=None) -> None:
        super().__init__(parent)
        self._client = client
        self._registry = registry
        self._make_ctx = make_ctx
        self._messages: list = []
        self._history: list = []  # list[ChatTurn] 喂 DS（与 messages 同步）
        self._streaming = ""
        self._worker = None
        self._offline = False

    # ---- QAbstractListModel ----
    def roleNames(self):
        return {
            self._RoleRole: b"role",
            self._ContentRole: b"content",
            self._RichRole: b"rich",
        }

    def rowCount(self, parent=QModelIndex()) -> int:
        return len(self._messages)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid() or role < Qt.UserRole:
            return None
        row = index.row()
        if row < 0 or row >= len(self._messages):
            return None
        msg = self._messages[row]
        if role == self._RoleRole:
            return msg["role"]
        if role == self._ContentRole:
            return msg["content"]
        if role == self._RichRole:
            return msg["rich"]
        return None

    # ---- 流式占位 Property ----
    def streamingText(self) -> str:
        return self._streaming

    streamingText = Property(str, fget=streamingText, notify=streamingChanged)

    # ---- 发送一轮 ----
    @Slot(str)
    def send(self, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        if self._client is None or self._offline:
            self.offlineRequested.emit()
            return
        if self._worker is not None and self._worker.isRunning():
            return

        self._append_message("user", text)
        self._set_streaming("")
        from ..llm import ChatWorker

        self._worker = ChatWorker(
            self._client, self._history, text, self._make_ctx()
        )
        self._worker.delta.connect(self._on_delta)
        self._worker.done.connect(self._on_done)
        self._worker.offline.connect(self._on_offline)
        self._worker.failed.connect(self._on_failed)
        self._worker.start()

    @Slot()
    def cancel(self) -> None:
        """关面板时调，中断流式（不泄漏线程）。"""
        if self._worker is not None and self._worker.isRunning():
            self._worker.cancel()
            self._worker.quit()

    # ---- worker 信号 ----
    @Slot(str)
    def _on_delta(self, chunk: str) -> None:
        self._set_streaming(self._streaming + chunk)

    @Slot(object)
    def _on_done(self, appended: list) -> None:
        final_text = ""
        for turn in appended:
            self._history.append(turn)
            if turn.role == "assistant":
                final_text = turn.content
        if not final_text:
            final_text = self._streaming
        self._append_message("assistant", final_text)
        self._set_streaming("")
        self._worker = None

    @Slot()
    def _on_offline(self) -> None:
        self._offline = True
        self._set_streaming("")
        self._worker = None
        self.offlineRequested.emit()

    @Slot(str)
    def _on_failed(self, reply: str) -> None:
        self._append_message("assistant", reply)
        self._set_streaming("")
        self._worker = None

    # ---- 内部 ----
    def _append_message(self, role: str, content: str) -> None:
        """QAbstractListModel insertRows——触发 QML ListView 刷新（可靠，
        不靠 Property notify）。只管 UI messages；_history 由 _on_done 的
        appended（ChatTurn）管，避免重复/类型混。"""
        row = len(self._messages)
        self.beginInsertRows(QModelIndex(), row, row)
        self._messages = self._messages + [
            {"role": role, "content": content, "rich": _md_to_html(content)}
        ]
        self.endInsertRows()

    def _set_streaming(self, text: str) -> None:
        self._streaming = text
        self.streamingChanged.emit()

    def reset_offline(self) -> None:
        self._offline = False


def load_chat_panel(bridge: "ChatBridge", qml_path: str) -> QQmlApplicationEngine:
    """载入 QML 聊天面板。singleton 注入 bridge（QAbstractListModel 实例），
    QML 侧 ``import PetChat 1.0`` 用 ``Chat`` 访问。"""
    global _SINGLETON_REGISTERED
    if not _SINGLETON_REGISTERED:
        qmlRegisterSingletonInstance(
            ChatBridge, "PetChat", 1, 0, "Chat", bridge
        )
        _SINGLETON_REGISTERED = True
    engine = QQmlApplicationEngine()
    engine.load(qml_path)
    if not engine.rootObjects():
        log.error("QML 聊天面板载入失败: %s", qml_path)
    return engine


_SINGLETON_REGISTERED = False
