"""QML 聊天面板桥接 —— 设计思路.md §五 / 版本规划 v0.4。

``ChatBridge``（QObject）暴露给 QML：``send(text)`` 发起一轮对话，
``messages``（QVariantList，role/content/rich）驱动 ListView，``streamingText``
是正在流式生成的助手消息。markdown→HTML 最小转换（加粗/斜体/代码/换行），
QML ``Text`` 用 ``RichText`` 渲染。

ChatBridge **不 import requests/keyring/AppKit**——DS 请求经注入的
``DeepSeekClient``+``ChatWorker``，工具经 ``ToolRegistry``，key 经 ``app``。
v0.11 真全局热键；v0.4 唤出经托盘/聚焦（``show()``/``hide()`` 由 app 调）。
"""

from __future__ import annotations

import html
import logging
import re
from typing import Optional

from PySide6.QtCore import Property, QObject, Qt, Signal, Slot
from PySide6.QtGui import QGuiApplication
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


class ChatBridge(QObject):
    """QML ↔ DS 的桥。生命周期由 app 持有。"""

    messagesChanged = Signal()
    streamingChanged = Signal()
    offlineRequested = Signal()  # app 接 → 气泡"当前离线"
    failedReply = Signal(str)  # 降级回复文本 → app 气泡（可选）

    def __init__(self, client, registry, make_ctx, parent=None) -> None:
        """``make_ctx``: callable() -> ToolContext（app 注入，按需取当前 state）。"""
        super().__init__(parent)
        self._client = client
        self._registry = registry
        self._make_ctx = make_ctx
        self._history: list = []  # list[ChatTurn]
        self._messages: list = []  # QVariantList: {role, content, rich}
        self._streaming = ""
        self._worker = None
        self._offline = False

    # ---- QML 属性 ----
    def messages(self) -> list:
        return self._messages

    messages = Property(list, fget=messages, notify=messagesChanged)

    def streamingText(self) -> str:
        return self._streaming

    streamingText = Property(
        str, fget=streamingText, notify=streamingChanged
    )

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
            return  # 上一轮未完，忽略（防并发竞态）

        # 立即显示用户消息
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
        """关面板时调，中断流式（T15 不泄漏线程）。"""
        if self._worker is not None and self._worker.isRunning():
            self._worker.cancel()
            self._worker.quit()  # 流式已 cancel，run 自然退出

    # ---- worker 信号 ----
    @Slot(str)
    def _on_delta(self, chunk: str) -> None:
        self._set_streaming(self._streaming + chunk)

    @Slot(object)
    def _on_done(self, appended: list) -> None:
        # appended = [user, assistant, (tool, assistant)*]
        final_text = ""
        for turn in appended:
            self._history.append(turn)
            if turn.role == "assistant":
                final_text = turn.content
        if not final_text:
            final_text = self._streaming
        # 把流式占位落定为正式助手消息
        self._append_message("assistant", final_text)
        self._set_streaming("")
        self._worker = None

    @Slot()
    def _on_offline(self) -> None:
        self._offline = True
        # 流式占位落空，清掉
        self._set_streaming("")
        self._worker = None
        self.offlineRequested.emit()

    @Slot(str)
    def _on_failed(self, reply: str) -> None:
        # 降级：把流式占位落为降级回复
        self._append_message("assistant", reply)
        self._set_streaming("")
        self._worker = None

    # ---- 内部 ----
    def _append_message(self, role: str, content: str) -> None:
        # 新 list 引用（非 in-place append）——QML model: Chat.messages 绑定
        # singleton Property notify 时 re-read，新引用强制 QML ListView 刷新
        # （in-place append 同引用，QML model 不检测内容变→不刷新）
        self._messages = self._messages + [
            {"role": role, "content": content, "rich": _md_to_html(content)}
        ]
        self.messagesChanged.emit()

    def _set_streaming(self, text: str) -> None:
        self._streaming = text
        self.streamingChanged.emit()

    def reset_offline(self) -> None:
        """app 重连/有 key 后重置离线态，重新允许聊天。"""
        self._offline = False


def load_chat_panel(bridge: ChatBridge, qml_path: str) -> QQmlApplicationEngine:
    """载入 QML 聊天面板。

    用 ``qmlRegisterSingletonInstance`` 注入 bridge（v0.4 在本机 PySide6
    6.10 实测：``setContextProperty`` 不解析（QML 见 null），singleton 稳；
    singleton 只注册一次（模块 flag 防重复）。QML 侧 ``import PetChat 1.0``
    用 ``Chat`` 访问 bridge。
    """
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
