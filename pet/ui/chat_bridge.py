"""QML 聊天面板桥接 —— 设计思路.md §五 / 版本规划 v0.4。

``ChatBridge(QAbstractListModel)``：messages 走 QAbstractListModel（beginInsertRows
触发 QML ListView 刷新——singleton Property notify 在 PySide6 6.10 不刷新 ListView、
setContextProperty QML 见 null，QAbstractListModel 是唯一可靠方案）。``send(text)``
发起对话，``streamingText`` 是流式占位。markdown→HTML 最小转换（**粗**/*斜*/`code`）。

ChatBridge **不 import requests/keyring/AppKit**——DS 经注入的 ``DeepSeekClient``+
``ChatWorker``，工具经 ``ToolRegistry``，key 经 ``app``。

v0.17.1 多会话：``_messages``/``_history`` 单份字段移除，改走 ``SessionStore``
会话（``_cur``）。在飞回复（含离线/失败轮）按发起时的 ``_worker_sid`` 落回
原会话——用户切走不中断、不串会话；滚动摘要只压缩 session.history、
UI messages 全量保留（摘要解耦）。
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

    done = Signal(str, int, object, str)  # sid, cut, old_first(ChatTurn), summary
    failed = Signal()

    def __init__(self, client, prompt: str, cut: int, old_first,
                 sid: str = "", owns_client: bool = False, parent=None) -> None:
        super().__init__(parent)
        self._client = client
        self._prompt = prompt
        self._cut = cut
        self._old_first = old_first
        self._sid = sid   # v0.17.1 摘要归属会话（完成时落回，防跨会话错切）
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
        self.done.emit(self._sid, self._cut, self._old_first, summary)


class ChatBridge(QAbstractListModel):
    """QML ↔ DS 桥。messages 走 QAbstractListModel（insertRows 刷新 ListView）。"""

    _RoleRole = Qt.UserRole + 1
    _ContentRole = Qt.UserRole + 2
    _RichRole = Qt.UserRole + 3

    streamingChanged = Signal()
    offlineRequested = Signal()
    failedReply = Signal(str)
    petAvatarChanged = Signal()
    sessionListChanged = Signal()   # v0.17.2 会话列表/标题/active 变化

    def __init__(self, client, registry, make_ctx, parent=None,
                 sum_client=None, store=None) -> None:
        super().__init__(parent)
        self._client = client
        self._registry = registry
        self._make_ctx = make_ctx
        # H4/M5 修：摘要专用客户端（app 注入独立实例；缺省回落共享实例）
        self._sum_client = sum_client
        # v0.17.1 多会话：store 缺省内存态（不落盘，测试/降级兼容）；
        # 构造即保证 active 会话存在（空库自动 new）
        from .session_store import SessionStore
        self._store = store if store is not None else SessionStore()
        if self._store.active is None:
            self._store.new_session()
        # v0.17.6 每会话独立在飞：sid → worker（旧版单 worker 单飞——切换
        # 会话后立即发消息被拒）。流式按会话缓冲 _stream_bufs：sid → 已
        # 流式文本（切走不丢，切回 streamingText 直接续显全量）。
        self._workers: dict = {}
        self._worker_handlers: dict = {}   # sid → {信号名: partial}（cancel 断连用）
        self._stream_bufs: dict = {}
        self._sum_worker = None   # 滚动摘要后台线程（同一时刻至多一个）
        # 批次D/F15：本轮 user 文本——失败/离线路径也要把 user turn 补进
        # 发起会话 history（旧版只有 _on_done 的 head 带 user，失败轮 DS 历史
        # 出现"无问之答"，摘要按 user 计数删 UI 行时错位）。
        # v0.17.6 sid 化：多会话同时在飞时补账不串
        self._pending: dict = {}
        # 批次D/F10：cancel() 等 2s 未退的 worker 保引用于此，finished 后清
        # ——旧版无条件 deleteLater = 销毁可能仍在运行的 QThread（原生崩溃）
        # v0.17.6 多 worker：列表化
        self._dying: list = []
        self.on_user_message = None  # v0.6 可选钩子：app 侧 follow-up 启发式
        self._offline = False
        self._pet_avatar = ""  # 对方头像 file:// URL；空=未注入（QML 回退 🐱）

    # ---- v0.17.1 会话视图 ----
    @property
    def _cur(self):
        """当前活跃会话（active 保证非 None：构造/新建兜底）。"""
        session = self._store.active
        if session is None:   # 防御：store 被外清空
            session = self._store.new_session()
        return session

    @Slot(result=str)
    def newSession(self) -> str:
        """新建空会话并切换（在飞回复不中断，落回原会话）。返新会话 id。"""
        s = self._store.new_session()
        self.beginResetModel()
        self.endResetModel()
        # v0.17.6：切换只重发流式信号——getter 按 active 取缓冲，切走
        # 自动为空、切回直接续显该会话已流式的全量前半段
        self.streamingChanged.emit()
        self.sessionListChanged.emit()
        return s.id

    @Slot(str, result=bool)
    def switchSession(self, sid: str) -> bool:
        """切换会话。未命中返 False 不动 active；在飞轮继续跑、
        完成时写回发起会话（不串当前 UI）。"""
        if self._store.switch(sid) is None:
            return False
        self.beginResetModel()
        self.endResetModel()
        self.streamingChanged.emit()   # 流式按 active 重取（同 newSession）
        self.sessionListChanged.emit()
        return True

    @Slot(str, str, result=bool)
    def renameSession(self, sid: str, title: str) -> bool:
        """v0.17.3 手动重命名会话（弹层行内 ✎ 编辑）。

        空标题/未知 sid 拒绝返 False（QML 保持编辑态）；标题不截断
        （显示层 elide 兜底——与自动标题的 16 字截断区分：手动名是
        用户意志）。改名会 touch（排序浮到最近）+ 落盘。
        """
        session = self._store.get(sid)
        title = (title or "").strip()
        if session is None or not title:
            return False
        if session.title == title:
            return True   # 无变化：不触发列表刷新/落盘
        session.title = title
        session.touch()
        self._store.save()
        self.sessionListChanged.emit()
        return True

    @Slot(str, result=bool)
    def deleteSession(self, sid: str) -> bool:
        """v0.17.5 删除会话（右键菜单，QML 侧已确认）。

        删活跃会话 → 自动切到剩余最近会话（空库新建空会话）；在飞轮
        属于被删会话 → 不中断（worker 自跑完），完成回调发现会话已删
        丢弃（见 _on_done 等的 None 分支），不串到其他会话。
        """
        if self._store.get(sid) is None:
            return False
        was_active = sid == self._cur.id
        self._store.delete(sid)
        if self._store.active is None:
            self._store.new_session()
        if was_active:
            self.beginResetModel()
            self.endResetModel()
            self.streamingChanged.emit()
        self._store.save()
        self.sessionListChanged.emit()
        return True

    # ---- QAbstractListModel ----
    def roleNames(self):
        return {
            self._RoleRole: b"role",
            self._ContentRole: b"content",
            self._RichRole: b"rich",
        }

    def rowCount(self, parent=QModelIndex()) -> int:
        return len(self._cur.messages)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid() or role < Qt.UserRole:
            return None
        row = index.row()
        messages = self._cur.messages
        if row < 0 or row >= len(messages):
            return None
        msg = messages[row]
        if role == self._RoleRole:
            return msg["role"]
        if role == self._ContentRole:
            return msg["content"]
        if role == self._RichRole:
            # load 恢复的行不带 rich（只存 role/content）——首次访问现算
            # 并缓存回会话，后续不再重算
            rich = msg.get("rich")
            if rich is None:
                rich = msg["rich"] = _md_to_html(msg.get("content", ""))
            return rich
        return None

    # ---- 流式占位 Property ----
    def streamingText(self) -> str:
        # M13 修：流式文本经 _md_to_html（与落定消息同一管道）——旧版直接
        # 返回原始 DS 增量，RichText 下未转义的 <h1> 等构成 HTML 注入面。
        # v0.17.6 按会话取缓冲：切走后再切回，已流式的前半段直接续显
        raw = self._stream_bufs.get(self._cur.id, "")
        return _md_to_html(raw) if raw else ""

    streamingText = Property(str, fget=streamingText, notify=streamingChanged)

    # ---- 宠物头像（对方立绘 → file:// URL，QML Image 显示）----
    def petAvatar(self) -> str:
        return self._pet_avatar

    def set_pet_avatar(self, url: str) -> None:
        url = url or ""
        if url != self._pet_avatar:
            self._pet_avatar = url
            self.petAvatarChanged.emit()

    petAvatar = Property(str, fget=petAvatar, notify=petAvatarChanged)

    # ---- v0.17.2 会话列表（顶栏下拉数据源；同一 notify 复用——三者
    # 变化时机重合：新建/切换/标题更新/轮次落定） ----
    def _session_list(self) -> list:
        from .session_store import rel_time

        return [
            {"sid": s.id, "title": s.title, "rel": rel_time(s.updated_at)}
            for s in self._store.list_sessions()
        ]

    sessionList = Property("QVariant", fget=_session_list,
                           notify=sessionListChanged)

    def _session_title(self) -> str:
        return self._cur.title

    sessionTitle = Property(str, fget=_session_title,
                            notify=sessionListChanged)

    def _active_sid(self) -> str:
        return self._cur.id

    activeSid = Property(str, fget=_active_sid, notify=sessionListChanged)

    # ---- 发送一轮 ----
    @Slot(str, result=bool)
    def send(self, text: str) -> bool:
        """发起一轮对话。

        返回 False 表示本轮未被接受（空文本/离线/上一轮仍在飞）——QML
        据此保留输入框文本不清空（M4，REVIEW-2026-09-04：旧版静默丢弃，
        流式最长 180s 窗口内消息丢失零反馈）。
        """
        text = (text or "").strip()
        if not text:
            return False
        if self._client is None or self._offline:
            self.offlineRequested.emit()
            return False
        sid = self._cur.id
        w = self._workers.get(sid)
        if w is not None and w.isRunning():
            return False  # 该会话在飞：丢弃，输入由 QML 侧保留
            # v0.17.6 前是全局单飞——切到别的会话发消息也被拒；现每会话
            # 独立在飞，互不阻塞

        self._append_message("user", text)
        self._pending[sid] = text   # 批次D/F15：失败/离线路径补 user turn 用
        # v0.9 滚动摘要：不再在 send 前同步做（H4 修）——改在轮次完成后
        # （_on_done/_on_failed）后台异步触发，见 _maybe_summarize
        if self.on_user_message is not None:
            try:
                self.on_user_message(text)  # v0.6 follow-up 启发式（不阻塞聊天）
            except Exception:
                # L22：钩子异常留痕（app 侧各段已有内部 try，此处兜底）
                log.exception("on_user_message 钩子异常")
        self._stream_bufs.pop(sid, None)   # 新轮清残留流式
        self.streamingChanged.emit()       # 若为 active：气泡复位
        from functools import partial

        from ..llm import ChatWorker

        worker = ChatWorker(
            self._client, self._cur.history, text, self._make_ctx(), parent=self
        )
        # v0.17.6：信号经 partial(sid) 分发——每会话独立路由到对应回调，
        # 不依赖 QObject.sender()（跨线程 queued 下 Python 侧偶发 None）
        handlers = {
            "delta": partial(self._on_delta, sid),
            "done": partial(self._on_done, sid),
            "offline": partial(self._on_offline, sid),
            "failed": partial(self._on_failed, sid),
        }
        worker.delta.connect(handlers["delta"])
        worker.done.connect(handlers["done"])
        worker.offline.connect(handlers["offline"])
        worker.failed.connect(handlers["failed"])
        # 批次D/F10：finished→deleteLater 唯一删除通道（QThread 铁律）——
        # 旧版自然完成路径不接，worker 挂 parent 之下永不回收=每轮泄漏
        worker.finished.connect(worker.deleteLater)
        self._workers[sid] = worker
        self._worker_handlers[sid] = handlers
        worker.start()
        return True

    _SUMMARIZE_THRESHOLD = 20   # 超过触发
    _SUMMARIZE_BATCH = 10       # 压缩最老 N 轮

    def _maybe_summarize(self, session) -> None:
        """v0.9 滚屏摘要：session.history > 20 轮 → DS 压缩最老 10 轮。

        v0.17.1：按轮次完成的会话触发（参数化，不再假定 active）。
        v0.9.3(H4 修)：切片边界按**完整轮次**对齐——从 _SUMMARIZE_BATCH
        向后扫描，切点前若是 assistant(tool_calls) 或紧邻的 tool 结果则
        推迟一位（保证配对不切断；切断致 DS 收非法序列永久 400）。
        v0.10.18（H4 修收尾）：摘要移后台线程（旧版在 send() 主线程同步
        chat_once，read 超时 120s 冻 UI）；触发点从"send 前"挪到"轮次完成
        后"（_on_done/_on_failed）——避免与在飞 ChatWorker 并发共享客户端。
        失败/降级保留原文（下次再试），不阻塞任何路径。
        """
        history = session.history
        if (self._client is None
                or len(history) <= self._SUMMARIZE_THRESHOLD):
            return
        if self._sum_worker is not None and self._sum_worker.isRunning():
            return  # 上一轮摘要还在飞
        # 安全切点：从 batch 开始，跳过 tool 配对边界
        cut = self._SUMMARIZE_BATCH
        while cut < len(history):
            t = history[cut - 1]
            if t.role == "assistant" and t.tool_calls:
                cut += 1  # 切点前是带调用的 assistant → 推迟
                continue
            if t.role == "tool" and cut >= 2:
                prev = history[cut - 2]
                if prev.role == "assistant" and prev.tool_calls:
                    cut += 1  # 切点前是配对尾部 → 推迟
                    continue
            break
        old_turns = history[:cut]
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
            sid=session.id,
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

    @Slot(str, int, object, str)
    def _on_summarized(self, sid: str, cut: int, old_first, summary: str) -> None:
        """摘要后台完成 → 替换发起会话 history（前缀校验防陈旧应用）。

        v0.17.1 摘要解耦：只压缩 session.history（喂 DS 的上下文），
        UI messages 全量保留——旧版 beginRemoveRows 删 UI 行与"回看
        历史"冲突，废除。
        """
        from ..llm import ChatTurn

        session = self._store.get(sid)
        # worker 在飞期间会话头若已变（不该发生——单飞+尾部追加，保险），
        # 丢弃本次防错切
        if (session is None or not session.history
                or session.history[0] is not old_first):
            return
        session.history = (
            [ChatTurn("user", f"[此前对话摘要]\n{summary}")]
            + session.history[cut:]
        )
        self._sum_worker = None   # done 先于 finished 送达，此刻清理安全
        self._store.save()
        log.info("[记忆] 滚屏摘要(%s): cut=%d→摘要%.0f字（UI 全量保留）",
                 sid[:6], cut, len(summary))

    @Slot()
    def _on_summarize_failed(self) -> None:
        # 保留原文（下次轮次完成再试）；失败细节 worker 已打日志
        self._sum_worker = None   # failed 先于 finished 送达，此刻清理安全

    def swap_clients(self, client, sum_client=None) -> None:
        """v0.20 运行时切换模型（app._switch_model 调）：换 chat/摘要客户端。

        在飞 ChatWorker/_SummarizeWorker 构造时已捕获旧 client 引用，跑完
        不受影响；新 send/_maybe_summarize 即走新 client。_resp 各实例隔离，
        互不误伤（H4/M5 语义保持）。"""
        self._client = client
        if sum_client is not None:
            self._sum_client = sum_client

    @Slot()
    def cancel(self) -> None:
        """shutdown 收口用：真中断流式 + 等待 worker 退出 + 断信号。

        批次J/L10（REVIEW-2026-08-31）：调用点对齐——实际仅
        ``app.shutdown`` 调用；关面板**不**调（隐藏不中断在飞回复，
        重开可见完整消息——旧 docstring 误写"关面板时调"）。

        v0.4.12：cancel() 调 worker.cancel()（关 resp socket 真中断，非旧版只置
        标志跑满 120s）；wait(2000) 等 worker 退出；断 done 信号防"幽灵回复"
        （cancel 后 worker 若恰好完成仍 emit done → _on_done 把回复追加进已
        关闭面板 history，下次打开看到幽灵消息）；清 _worker 让 send() 不被
        isRunning() 静默吞新消息。
        批次D/F10（REVIEW-2026-08-28）：2s 未退（慢网络/确认框阻塞）不再
        无条件 deleteLater——那是"销毁可能仍在运行的 QThread"的原生崩溃
        （同文件摘要线程/proactive.shutdown 早已是保引用模式）。改保引用
        至 _dying，由 finished→deleteLater（send 已接）单通道收尾。
        """
        for sid, w in list(self._workers.items()):
            handlers = self._worker_handlers.pop(sid, None)
            if handlers is not None:
                for sig, fn in handlers.items():
                    try:
                        getattr(w, sig).disconnect(fn)
                    except (TypeError, RuntimeError, AttributeError):
                        pass
            if w.isRunning():
                w.cancel()
                if not w.wait(2000):
                    log.warning("[聊天] cancel 等待 2s 未退，保留引用待 "
                                "finished 自清（不销毁运行中线程）")
                    self._dying.append(w)
                    try:
                        w.finished.connect(self._on_dying_finished)
                    except (TypeError, RuntimeError):
                        pass
        self._workers.clear()
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
        self._stream_bufs.clear()
        self.streamingChanged.emit()
        # v0.17.1：shutdown 收口把内存态会话落盘（轮次完成路径已即时
        # save，此处兜底 send 后未完成即退出的 user 行）
        self._store.save()

    # ---- worker 信号（v0.17.6：partial(sid) 分发，签名带 sid） ----
    def _on_delta(self, sid: str, chunk: str) -> None:
        # 真流式：累加会话流式缓冲逐字显示（v0.4 Must "DS 回复流式打字机"）。
        # v0.17.6：delta 恒入发起会话缓冲（切走后再切回，前半段直接续显）；
        # 仅发起会话是 active 时 emit——流式气泡不进别的会话 UI。
        if sid not in self._workers:
            return  # cancel 已断连/done 后迟到的 delta
        self._stream_bufs[sid] = self._stream_bufs.get(sid, "") + chunk
        if sid == self._cur.id:
            self.streamingChanged.emit()

    def _on_done(self, sid: str, appended: list) -> None:
        if self._workers.pop(sid, None) is None:
            return  # cancel 后迟到的/重复的 done，丢弃（防幽灵回复）
        session = self._store.get(sid)
        if session is None:
            # v0.17.5：发起会话已被删除——回复无处落，丢弃（不串当前）
            self._pending.pop(sid, None)
            self._clear_stream(sid)
            return
        self._pending.pop(sid, None)   # head 已含 user，pending 清账
        final_text = ""
        for turn in appended:
            session.history.append(turn)
            if turn.role == "assistant":
                final_text = turn.content
        if not final_text:
            final_text = self._stream_bufs.get(sid, "")
        if session is self._cur:
            self._append_message("assistant", final_text)
        else:
            # v0.17.1：轮次发起后用户已切走——写回原会话（切回可见），
            # 不动当前 UI（beginInsertRows 会与 active 会话错位）
            session.messages.append(
                {"role": "assistant", "content": final_text,
                 "rich": _md_to_html(final_text)}
            )
            session.touch()
            self.sessionListChanged.emit()
        self._clear_stream(sid)
        self._store.save()
        # H4 修：轮次完成后异步触发滚屏摘要（旧版在 send 前 同步做）
        self._maybe_summarize(session)

    @Slot()
    def _on_dying_finished(self) -> None:
        """批次D/F10：cancel 超时未退的 worker 结束后释放保命引用。"""
        w = self.sender()
        if w in self._dying:
            self._dying.remove(w)

    def _on_offline(self, sid: str) -> None:
        # 失败/离线路径也追加发起会话（user 已在 send 追加 UI，这里补
        # user+assistant turn 进 DS history），否则下次 send 喂 DS 的
        # history 缺这轮，上下文脱节（批次D/F15：user turn 旧版永不入史）
        from ..llm import OFFLINE_REPLY, ChatTurn
        if self._workers.pop(sid, None) is None:
            return
        session = self._store.get(sid)
        if session is None:
            # v0.17.5：发起会话已被删除——丢弃（同 _on_done）
            self._pending.pop(sid, None)
            self._clear_stream(sid)
            self.offlineRequested.emit()
            return
        self._offline = True
        pending = self._pending.pop(sid, "")
        if pending:
            session.history.append(ChatTurn("user", pending))
        if session is self._cur:
            self._append_message("assistant", OFFLINE_REPLY)
        else:
            session.messages.append(
                {"role": "assistant", "content": OFFLINE_REPLY,
                 "rich": _md_to_html(OFFLINE_REPLY)}
            )
            session.touch()
            self.sessionListChanged.emit()
        session.history.append(ChatTurn("assistant", OFFLINE_REPLY))
        self._clear_stream(sid)
        self._store.save()
        self.offlineRequested.emit()

    def _on_failed(self, sid: str, reply: str) -> None:
        # 降级回复也进发起会话 history（同 _on_offline 理由；批次D/F15
        # 补 user turn）
        from ..llm import ChatTurn
        if self._workers.pop(sid, None) is None:
            return
        session = self._store.get(sid)
        if session is None:
            # v0.17.5：发起会话已被删除——丢弃（同 _on_done）
            self._pending.pop(sid, None)
            self._clear_stream(sid)
            return
        pending = self._pending.pop(sid, "")
        if pending:
            session.history.append(ChatTurn("user", pending))
        if session is self._cur:
            self._append_message("assistant", reply)
        else:
            session.messages.append(
                {"role": "assistant", "content": reply,
                 "rich": _md_to_html(reply)}
            )
            session.touch()
            self.sessionListChanged.emit()
        session.history.append(ChatTurn("assistant", reply))
        self._clear_stream(sid)
        self._store.save()
        # 降级轮也查摘要（历史持续增长；DS 恢复后下次触发补上）
        self._maybe_summarize(session)

    # ---- 内部 ----
    def _append_message(self, role: str, content: str) -> None:
        """UI 行进当前会话 messages——QAbstractListModel insertRows 触发
        QML ListView 刷新（可靠，不靠 Property notify）。history 由
        _on_done 的 appended（ChatTurn）管，避免重复/类型混。
        v0.17.1：append 不重建 list（保 session.messages 引用一致）；
        首条 user 消息落地时会话标题从"新对话"更新为消息摘要。"""
        from .session_store import default_title

        session = self._cur
        row = len(session.messages)
        self.beginInsertRows(QModelIndex(), row, row)
        session.messages.append(
            {"role": role, "content": content, "rich": _md_to_html(content)}
        )
        self.endInsertRows()
        if (role == "user" and session.title == "新对话"
                and content.strip()):
            session.title = default_title(content)
        session.touch()
        self.sessionListChanged.emit()   # 标题/排序可能变化

    def _clear_stream(self, sid: str) -> None:
        """清发起会话的流式缓冲；该会话是 active 时刷 UI（落定消息接管
        气泡）。非 active 不 emit——它本来就没在显示。"""
        if self._stream_bufs.pop(sid, None) is not None and sid == self._cur.id:
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
