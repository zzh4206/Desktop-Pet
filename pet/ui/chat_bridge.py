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
    QThread,
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


# 摘要轮 system（挂 system_override 顺带让 chat_once 走 tools_override=None
# 分支——摘要请求不挂工具，不触 dispatch）
_SUM_SYSTEM = "你是对话摘要助手。把对话压缩为要点，只输出摘要正文。"


class _SummarizeWorker(QThread):
    """H4 修（REVIEW-2026-08-25）：滚屏摘要后台线程。

    旧版在 send() 主线程 Slot 里同步 chat_once（connect 10s + read 120s
    超时，冻整个 UI 含宠物 50ms tick）。M2 修：FALLBACK_REPLY 降级文案/
    空摘要按失败处理（保留原文），不当有效摘要写回。
    """

    done = Signal(int, object, str)   # cut, old_first(ChatTurn), summary
    failed = Signal()

    def __init__(self, client, prompt: str, cut: int, old_first,
                 owns_client: bool = False, parent=None) -> None:
        super().__init__(parent)
        self._client = client
        self._prompt = prompt
        self._cut = cut
        self._old_first = old_first
        self._owns_client = owns_client

    def cancel(self) -> None:
        """中断摘要流式。仅独占客户端时关 _resp——共享实例上关闭会误伤
        在飞的聊天/主动关怀流（M5 竞态）。"""
        if not self._owns_client:
            return
        try:
            if self._client._resp is not None:
                self._client._resp.close()
        except Exception:
            pass

    def run(self) -> None:
        from ..llm import FALLBACK_REPLY, ChatTurn

        try:
            summary, _ = self._client.chat_once(
                [ChatTurn("user", self._prompt)], None,
                system_override=_SUM_SYSTEM, tools_override=None,
            )
        except Exception:
            log.warning("[记忆] 滚动摘要请求失败(保留原文)", exc_info=True)
            self.failed.emit()
            return
        summary = (summary or "").strip()
        if not summary or summary == FALLBACK_REPLY:
            self.failed.emit()
            return
        self.done.emit(self._cut, self._old_first, summary)


class ChatBridge(QAbstractListModel):
    """QML ↔ DS 桥。messages 走 QAbstractListModel（insertRows 刷新 ListView）。"""

    _RoleRole = Qt.UserRole + 1
    _ContentRole = Qt.UserRole + 2
    _RichRole = Qt.UserRole + 3

    streamingChanged = Signal()
    offlineRequested = Signal()
    failedReply = Signal(str)

    def __init__(self, client, registry, make_ctx, parent=None,
                 sum_client=None) -> None:
        super().__init__(parent)
        self._client = client
        self._registry = registry
        self._make_ctx = make_ctx
        # H4/M5 修：摘要专用客户端（app 注入独立实例；缺省回落共享实例）
        self._sum_client = sum_client
        self._messages: list = []
        self._history: list = []  # list[ChatTurn] 喂 DS（与 messages 同步）
        self._streaming = ""
        self._worker = None
        self._sum_worker = None   # 滚动摘要后台线程（同一时刻至多一个）
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
        # v0.9 滚动摘要：不再在 send 前同步做（H4 修）——改在轮次完成后
        # （_on_done/_on_failed）后台异步触发，见 _maybe_summarize
        if self.on_user_message is not None:
            try:
                self.on_user_message(text)  # v0.6 follow-up 启发式（不阻塞聊天）
            except Exception:
                pass
        self._set_streaming("")
        from ..llm import ChatWorker

        self._worker = ChatWorker(
            self._client, self._history, text, self._make_ctx(), parent=self
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
        v0.10.18（H4 修收尾）：摘要移后台线程（旧版在 send() 主线程同步
        chat_once，read 超时 120s 冻 UI）；触发点从"send 前"挪到"轮次完成
        后"（_on_done/_on_failed）——避免与在飞 ChatWorker 并发共享客户端。
        失败/降级保留原文（下次再试），不阻塞任何路径。
        """
        if (self._client is None
                or len(self._history) <= self._SUMMARIZE_THRESHOLD):
            return
        if self._sum_worker is not None and self._sum_worker.isRunning():
            return  # 上一轮摘要还在飞
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
        old_turns = self._history[:cut]
        transcript = "\n".join(
            f"{t.role}: {t.content[:200]}" for t in old_turns
            if t.role in ("user", "assistant")
        )
        prompt = (
            "把以下对话压缩成一段不超过150字的要点摘要"
            "（保留人名/偏好/约定/结论），只输出摘要：\n\n" + transcript
        )
        client = self._sum_client if self._sum_client is not None else self._client
        self._sum_worker = _SummarizeWorker(
            client, prompt, cut, old_turns[0],
            owns_client=self._sum_client is not None,
            parent=self,
        )
        self._sum_worker.done.connect(self._on_summarized)
        self._sum_worker.failed.connect(self._on_summarize_failed)
        # 线程对象挂 parent=self（C++ 生命周期归桥管）——done 送达时线程
        # 可能仍在收尾，Python 引用先丢会触发"销毁运行中 QThread"的原生
        # 崩溃（perm worker 实测段错误）；删除只走 finished→deleteLater
        self._sum_worker.finished.connect(self._sum_worker.deleteLater)
        self._sum_worker.start()

    @Slot(int, object, str)
    def _on_summarized(self, cut: int, old_first, summary: str) -> None:
        """摘要后台完成 → 主线程替换历史（前缀校验防陈旧应用）。"""
        from ..llm import ChatTurn

        # worker 在飞期间历史头若已变（不该发生——单飞+尾部追加，保险），
        # 丢弃本次防错切
        if not self._history or self._history[0] is not old_first:
            return
        # M3 修：UI messages 按轮数对齐删除——_history 的 tool 轮不进
        # _messages（每轮固定 user+assistant 两条），旧版 cut*2 按"每轮
        # 两条 history"假设切片，有工具调用的会话删多。头部若已是上一次
        # 的摘要标记 turn（user 角色、无 UI 行），计数扣 1。
        users = sum(1 for t in self._history[:cut] if t.role == "user")
        if (self._history[0].role == "user"
                and self._history[0].content.startswith("[此前对话摘要]")):
            users -= 1
        self._history = (
            [ChatTurn("user", f"[此前对话摘要]\n{summary}")]
            + self._history[cut:]
        )
        self._sum_worker = None   # done 先于 finished 送达，此刻清理安全
        pairs = max(0, min(users, len(self._messages) // 2))
        if pairs:
            self.beginRemoveRows(QModelIndex(), 0, pairs * 2 - 1)
            self._messages = self._messages[pairs * 2:]
            self.endRemoveRows()
        log.info("[记忆] 滚屏摘要: cut=%d→摘要%.0f字（UI 删 %d 轮）",
                 cut, len(summary), pairs)

    @Slot()
    def _on_summarize_failed(self) -> None:
        # 保留原文（下次轮次完成再试）；失败细节 worker 已打日志
        self._sum_worker = None   # failed 先于 finished 送达，此刻清理安全

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
        # H4 修：摘要线程一并收口（shutdown 也走这里——QThread 挂后台
        # 不等会 "Destroyed while thread is still running"）。deleteLater
        # 由 finished→deleteLater 连接兜底（含自然完成路径），此处不重复
        # 手删——对象可能已被事件循环回收。
        sw = self._sum_worker
        if sw is not None:
            try:
                sw.done.disconnect(self._on_summarized)
                sw.failed.disconnect(self._on_summarize_failed)
            except (TypeError, RuntimeError):
                pass
            try:
                running = sw.isRunning()
            except RuntimeError:
                running = False   # 已被 finished→deleteLater 回收
            if running:
                sw.cancel()
                sw.wait(2000)
        self._sum_worker = None
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
        # H4 修：轮次完成后异步触发滚屏摘要（旧版在 send 前 同步做）
        self._maybe_summarize()

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
        # 降级轮也查摘要（历史持续增长；DS 恢复后下次触发补上）
        self._maybe_summarize()

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
