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
    """最小 markdown→HTML（**粗**/*斜*/`code`/换行），转义防 XSS。

    code span 先替换为占位符，bold/italic 处理完再还原——防 `` `a*b*c` `` 里
    code 内的 ``*`` 被 _ITALIC 误匹配成斜体（v0.4.12）。
    """
    s = html.escape(text)
    # code 占位保护：先收 code span，避免内部 * 被后续 italic 误匹配
    codes: list[str] = []

    def _stash_code(m: re.Match) -> str:
        codes.append(m.group(1))
        return f"\x00CODE{len(codes) - 1}\x00"

    s = _CODE.sub(_stash_code, s)
    s = _BOLD.sub(r"<b>\1</b>", s)
    s = _ITALIC.sub(r"<i>\1</i>", s)
    # 还原 code span
    for i, c in enumerate(codes):
        s = s.replace(f"\x00CODE{i}\x00", f"<code>{c}</code>")
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
        self.on_user_message = None  # v0.6 可选钩子：app 侧 follow-up 启发式
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
        # M13 修：流式文本经 _md_to_html（与落定消息同一管道）——旧版直接
        # 返回原始 DS 增量，RichText 下未转义的 <h1> 等构成 HTML 注入面
        return _md_to_html(self._streaming) if self._streaming else ""

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
        self._maybe_summarize()   # v0.9 滚屏摘要（发送前检查历史长度）
        if self.on_user_message is not None:
            try:
                self.on_user_message(text)  # v0.6 follow-up 启发式（不阻塞聊天）
            except Exception:
                pass
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

    _SUMMARIZE_THRESHOLD = 20   # 超过触发
    _SUMMARIZE_BATCH = 10       # 压缩最老 N 轮

    def _maybe_summarize(self) -> None:
        """v0.9 滚屏摘要：history > 20 轮 → DS 压缩最老 10 轮为一段摘要。

        v0.9.3(H4 修)：切片边界按**完整轮次**对齐——从 _SUMMARIZE_BATCH
        向后扫描，切点前若是 assistant(tool_calls) 或紧邻的 tool 结果则
        推迟一位（保证配对不切断；切断致 DS 收非法序列永久 400）。
        DS 失败保留原文（下次再试），不阻塞发送。
        """
        if (self._client is None
                or len(self._history) <= self._SUMMARIZE_THRESHOLD):
            return
        # 安全切点：从 batch 开始，跳过 tool 配对边界
        cut = self._SUMMARIZE_BATCH
        while cut < len(self._history):
            t = self._history[cut - 1]
            if t.role == "assistant" and t.tool_calls:
                cut += 1  # 切点前是带调用的 assistant → 推迟
                continue
            if t.role == "tool" and cut >= 2:
                prev = self._history[cut - 2]
                if prev.role == "assistant" and prev.tool_calls:
                    cut += 1  # 切点前是配对尾部 → 推迟
                    continue
            break
        try:
            old_turns = self._history[:cut]
            transcript = "\n".join(
                f"{t.role}: {t.content[:200]}" for t in old_turns
                if t.role in ("user", "assistant")
            )
            prompt = (
                "把以下对话压缩成一段不超过150字的要点摘要"
                "（保留人名/偏好/约定/结论），只输出摘要：\n\n" + transcript
            )
            from ..llm import ChatTurn

            summary, _ = self._client.chat_once(
                [ChatTurn("user", prompt)], None,
            )
            summary = (summary or "").strip()
            if not summary:
                return
            self._history = (
                [ChatTurn("user", f"[此前对话摘要]\n{summary}")]
                + self._history[cut:]
            )
            self._messages = self._messages[cut * 2:]
            log.info("[记忆] 滚屏摘要: %d轮(cut=%d)→摘要%.0f字",
                     len(old_turns), cut, len(summary))
        except Exception:
            log.warning("[记忆] 滚屏摘要失败(保留原文)", exc_info=True)

    @Slot()
    def cancel(self) -> None:
        """关面板时调，真中断流式 + 等待 worker 退出 + 断信号（不泄漏线程）。

        v0.4.12：cancel() 调 worker.cancel()（关 resp socket 真中断，非旧版只置
        标志跑满 120s）；wait(2000) 等 worker 退出；断 done 信号防"幽灵回复"
        （cancel 后 worker 若恰好完成仍 emit done → _on_done 把回复追加进已
        关闭面板 history，下次打开看到幽灵消息）；清 _worker 让 send() 不被
        isRunning() 静默吞新消息。
        """
        w = self._worker
        if w is not None:
            try:
                w.done.disconnect(self._on_done)
            except (TypeError, RuntimeError):
                pass
            if w.isRunning():
                w.cancel()
                w.wait(2000)
            w.deleteLater()
        self._worker = None
        self._set_streaming("")

    # ---- worker 信号 ----
    @Slot(str)
    def _on_delta(self, chunk: str) -> None:
        # 真流式：累加 streaming 逐字显示（v0.4 Must "DS 回复流式打字机"）。
        # _on_done 时把 streaming 并入正式 assistant message 并清空。
        if self._worker is None:
            return  # cancel 已断信号，迟到的 delta 丢弃
        self._set_streaming(self._streaming + chunk)

    @Slot(object)
    def _on_done(self, appended: list) -> None:
        if self._worker is None:
            return  # cancel 后迟到的 done，丢弃（防幽灵回复）
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
        # 失败/离线路径也追加 _history（user 已在 send 追加 UI，这里补 assistant
        # turn 进 DS history），否则下次 send 喂 DS 的 history 缺这轮，上下文脱节
        from ..llm import OFFLINE_REPLY, ChatTurn
        self._offline = True
        self._set_streaming("")
        self._append_message("assistant", OFFLINE_REPLY)
        self._history.append(ChatTurn("assistant", OFFLINE_REPLY))
        self._worker = None
        self.offlineRequested.emit()

    @Slot(str)
    def _on_failed(self, reply: str) -> None:
        # 降级回复也进 _history（同 _on_offline 理由）
        from ..llm import ChatTurn
        self._append_message("assistant", reply)
        self._history.append(ChatTurn("assistant", reply))
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

    @Slot()
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
