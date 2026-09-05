"""v0.4 聊天桥接验证 —— 流式累积 / cancel 中断 / 失败 history 追加（C1/C2/C5）。

FakeChatWorker 替代真实 ChatWorker（monkeypatch llm.ChatWorker），可控触发
delta/done/failed/offline 信号，验证 ChatBridge 行为：
- C1: _on_delta 累积 streamingText（真流式，非空 pass）
- C2: cancel() 断 done 信号 + 清 _worker（cancel 后迟到 done 不进 history）
- C5: _on_failed/_on_offline 追加 _history（UI 与 DS 上下文一致）

运行：python spikes/test_v04_chat_bridge.py
"""

from __future__ import annotations

import os
import sys
import tempfile

# 批次H/M12（REVIEW-2026-08-31 F31）：缺省 offscreen（不依赖真显示会话）
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, ".")

from PySide6.QtCore import QCoreApplication, QTimer, QThread, Signal, Slot  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from pet import llm as llm_mod  # noqa: E402
from pet.llm import ChatTurn  # noqa: E402
from pet.ui.chat_bridge import ChatBridge, _md_to_html  # noqa: E402

PASS, FAIL = [], []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("  ✅ " if cond else "  ❌ ") + name)


class FakeChatWorker(QThread):
    """可控 ChatWorker 替身：测试调 emit_* 触发信号，不真发网络。"""

    delta = Signal(str)
    done = Signal(object)
    offline = Signal()
    failed = Signal(str)

    def __init__(self, client, history, user_text, ctx, parent=None):
        super().__init__(parent)
        self._cancelled = False
        self._history = history
        self._user_text = user_text

    def start(self, *a, **k):
        pass   # 替身不起真线程（run 为空，信号由 emit_* 手动驱动）——真
               # start 的空线程在进程退出时与 C++ 收尾竞态，曾致 ~30% 概率
               # 无栈段错误（历史抖动根因）

    def run(self):
        # 不做真网络；测试通过 emit_* 手动驱动（run 默认空，start 已 no-op）
        pass

    def cancel(self):
        self._cancelled = True

    # 测试驱动入口
    def emit_delta(self, chunk):
        self.delta.emit(chunk)

    def emit_done(self, appended):
        self.done.emit(appended)

    def emit_failed(self, reply):
        self.failed.emit(reply)

    def emit_offline(self):
        self.offline.emit()


def main() -> int:
    app = QCoreApplication.instance() or QApplication(sys.argv)

    # monkeypatch llm.ChatWorker 为 FakeChatWorker（send() 内 from ..llm import）
    real_worker = llm_mod.ChatWorker
    llm_mod.ChatWorker = FakeChatWorker

    bridge = ChatBridge(
        client=object(),  # 不真用，FakeChatWorker 不调 client
        registry=object(),
        make_ctx=lambda: None,
    )

    # ---- C1: _on_delta 累积 streamingText（真流式） ----
    bridge.send("hi")
    worker = bridge._worker
    worker.emit_delta("你")
    worker.emit_delta("好")
    worker.emit_delta("呀")
    check("C1 delta 累积 streamingText", bridge.streamingText == "你好呀")

    # done 落定：streaming 并入 message 并清空
    worker.emit_done([
        ChatTurn("user", "hi"),
        ChatTurn("assistant", "你好呀"),
    ])
    check("C1 done 落定 streaming 清空", bridge.streamingText == "")
    check("C1 done assistant 进 messages",
          bridge._messages[-1]["role"] == "assistant"
          and bridge._messages[-1]["content"] == "你好呀")
    check("C1 done history 含 assistant turn",
          any(t.role == "assistant" and t.content == "你好呀"
              for t in bridge._history))

    # ---- C2: cancel 断 done + 清 worker（迟到 done 不进 history） ----
    bridge.send("second")
    worker2 = bridge._worker
    hist_before = len(bridge._history)
    bridge.cancel()
    check("C2 cancel 清 _worker", bridge._worker is None)
    # 模拟 cancel 后迟到的 done（幽灵回复）
    worker2.emit_done([
        ChatTurn("user", "second"),
        ChatTurn("assistant", "幽灵回复"),
    ])
    check("C2 cancel 后迟到 done 不进 history",
          len(bridge._history) == hist_before)
    check("C2 幽灵回复不进 messages",
          all(m["content"] != "幽灵回复" for m in bridge._messages))

    # ---- C5: _on_failed 追加 _history（UI 与 DS 上下文一致） ----
    # 批次D/F15 修：失败轮的 user turn 也要入史（旧版只补 assistant，DS
    # 历史"无问之答"且摘要按 user 计数删 UI 行错位——本断言曾固化该 bug）
    bridge.send("third")
    worker3 = bridge._worker
    hist_before_fail = len(bridge._history)
    worker3.emit_failed("我开小差了～")
    check("C5 failed assistant 进 messages",
          bridge._messages[-1]["content"] == "我开小差了～")
    check("C5 failed 补 user+assistant 入 _history（顺序正确）",
          len(bridge._history) == hist_before_fail + 2
          and bridge._history[-2].role == "user"
          and bridge._history[-2].content == "third"
          and bridge._history[-1].role == "assistant"
          and bridge._history[-1].content == "我开小差了～")

    # ---- C5b: 离线路径同样补 user turn（批次D/F15）----
    bridge.send("fourth")
    worker4 = bridge._worker
    hist_before_off = len(bridge._history)
    worker4.emit_offline()
    from pet.llm import OFFLINE_REPLY
    check("C5b offline 补 user+assistant 入 _history",
          len(bridge._history) == hist_before_off + 2
          and bridge._history[-2].role == "user"
          and bridge._history[-2].content == "fourth"
          and bridge._history[-1].content == OFFLINE_REPLY)
    check("C5b offline 置 _offline 标志", bridge._offline is True)
    # 离线后再 send 应触发 offlineRequested 不发 worker
    offline_triggered = [False]

    def on_off():
        offline_triggered[0] = True

    bridge.offlineRequested.connect(on_off)
    bridge.send("fifth")
    check("C5 离线后 send 触发 offlineRequested 不发 worker",
          offline_triggered[0] and bridge._worker is None)
    bridge.offlineRequested.disconnect(on_off)

    # ---- 低危: _md_to_html code 占位保护 ----
    html_code = _md_to_html("`a*b*c`")
    check("低危 _md_to_html code 内 * 不被 italic 误匹配",
          "<code>a*b*c</code>" in html_code and "<i>" not in html_code)

    # ---- T16 批次H/M11（REVIEW-2026-08-31 F30）：真 ChatWorker 生命周期 ----
    # 旧版 FakeChatWorker.start() no-op——真线程的 cancel 中断/迟到信号/
    # 离线分类零覆盖。此处还原本体 + stub client（无网络）真跑 QThread
    llm_mod.ChatWorker = real_worker
    import time as _time

    from pet.llm import OFFLINE_REPLY, OfflineError

    class _StubClient:
        """无网络 client：可控延迟/异常；_resp=None 供 cancel 探测。"""

        def __init__(self, reply="在的～", delay=0.05, exc=None):
            self._reply, self._delay, self._exc = reply, delay, exc
            self._resp = None

        def chat_once(self, history, ctx, on_delta=None):
            if self._delay:
                _time.sleep(self._delay)
            if self._exc is not None:
                raise self._exc
            if on_delta:
                on_delta(self._reply)
            return self._reply, [ChatTurn("assistant", self._reply)]

    from PySide6.QtTest import QTest

    def _pump(ms: int) -> None:
        """跨线程信号投递泵事件循环。实测（批次H）：QTest.qWait 在
        QCoreApplication 下不投递 queued 信号，QApplication 下可——
        显式泵两态通吃，不再依赖该差异"""
        t0 = _time.monotonic()
        while ( _time.monotonic() - t0) * 1000 < ms:
            QCoreApplication.processEvents()
            _time.sleep(0.005)

    # 16a 正常完成：真线程跑完 + done 信号送达 → 回复落定、worker 自清
    b2 = ChatBridge(client=_StubClient(), registry=object(),
                    make_ctx=lambda: None)
    n0 = len(b2._messages)
    b2.send("真线程问好")
    _pump(700)
    check("T16a 真 ChatWorker 完成：回复落定",
          len(b2._messages) == n0 + 2
          and b2._messages[-1]["content"] == "在的～")
    check("T16a worker 完成自清（_worker=None）", b2._worker is None)

    # 16b cancel 真中断：长延迟 stub，cancel 后无幽灵回复、可再发
    b3 = ChatBridge(client=_StubClient(delay=1.0), registry=object(),
                    make_ctx=lambda: None)
    m0 = len(b3._messages)
    b3.send("会被取消")
    _pump(100)          # 线程已在 stub 里 sleep
    b3.cancel()               # wait(2000) 内线程自然跑完（cancelled 不 emit）
    _pump(1200)         # 等 stub 返回点过去
    check("T16b cancel 后无幽灵回复（仅 user 入列）",
          len(b3._messages) == m0 + 1)
    check("T16b cancel 后 _worker 清空可再发", b3._worker is None)

    # 16c 离线路径：stub 抛 OfflineError → offline 信号语义 + 入史
    b4 = ChatBridge(client=_StubClient(delay=0.0, exc=OfflineError("断网")),
                    registry=object(), make_ctx=lambda: None)
    b4.send("离线消息")
    _pump(400)
    check("T16c 离线入史 + _offline 置位",
          b4._offline is True
          and b4._messages[-1]["content"] == OFFLINE_REPLY)

    # M4（REVIEW-2026-09-04）：在飞一轮 send 被拒返回 False、消息不入史
    # （旧版静默 return + QML 无条件清空输入=消息丢失零反馈）
    class _RunningStub:
        def isRunning(self):
            return True

    b4._worker = _RunningStub()
    n_busy = len(b4._messages)
    ok_busy = b4.send("会被拒绝")
    check("M4 在飞 send 返回 False 且不入史",
          ok_busy is False and len(b4._messages) == n_busy)
    check("M4 空文本 send 返回 False", b4.send("   ") is False)
    b4._worker = None

    # ---- 批次B/P2-10（REVIEW-2026-09-05）：confirm 阻塞等待有超时兜底 ----
    import time as _time
    from pet.tools_schema import _ConfirmCaller
    _app = QApplication.instance()
    _caller = _ConfirmCaller(lambda t, c, r: True)  # 若主线程能处理即通过
    _caller.moveToThread(_app.thread())
    # 同线程（caller 已在主线程）→ 信号直连，行为不变
    _r_same = _caller.call_blocking("t2", "c2", "r2", timeout_ms=1000)
    check("P2-10a 同线程 confirm 直连路径不变", _r_same is True)

    class _WaitWorker(QThread):
        def __init__(self):
            super().__init__(_app)
            self.result = None

        def run(self):
            self.result = _caller.call_blocking("t", "c", "r", timeout_ms=150)

    _w = _WaitWorker()
    _w.finished.connect(_w.deleteLater)
    _w.start()
    _time.sleep(0.4)  # 主线程忙睡——queued 槽不执行，只能等 worker 侧超时
    _w.wait(2000)
    check("P2-10b confirm 超时 fail-closed（主线程被占住时不再永久阻塞）",
          _w.result is False)

    print(f"\n结果：{len(PASS)} 通过 / {len(FAIL)} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
