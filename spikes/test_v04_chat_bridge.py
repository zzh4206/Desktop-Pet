"""v0.4 聊天桥接验证 —— 流式累积 / cancel 中断 / 失败 history 追加（C1/C2/C5）。

FakeChatWorker 替代真实 ChatWorker（monkeypatch llm.ChatWorker），可控触发
delta/done/failed/offline 信号，验证 ChatBridge 行为：
- C1: _on_delta 累积 streamingText（真流式，非空 pass）
- C2: cancel() 断 done 信号 + 清 _worker（cancel 后迟到 done 不进 history）
- C5: _on_failed/_on_offline 追加 _history（UI 与 DS 上下文一致）

运行：python spikes/test_v04_chat_bridge.py
"""

from __future__ import annotations

import sys
import tempfile

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

    def run(self):
        # 不做真网络；测试通过 emit_* 手动驱动（run 默认空，start 后立即 finish）
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
    bridge.send("third")
    worker3 = bridge._worker
    hist_before_fail = len(bridge._history)
    worker3.emit_failed("我开小差了～")
    check("C5 failed assistant 进 messages",
          bridge._messages[-1]["content"] == "我开小差了～")
    check("C5 failed 追加 _history（DS 上下文一致）",
          len(bridge._history) == hist_before_fail + 1
          and bridge._history[-1].role == "assistant"
          and bridge._history[-1].content == "我开小差了～")

    # ---- C5: _on_offline 追加 _history ----
    bridge.send("fourth")  # _offline 已由 _on_failed? 否，failed 不置 offline
    worker4 = bridge._worker
    hist_before_off = len(bridge._history)
    worker4.emit_offline()
    check("C5 offline 置 _offline 标志", bridge._offline is True)
    check("C5 offline 追加 _history",
          len(bridge._history) == hist_before_off + 1)
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

    # 还原 monkeypatch
    llm_mod.ChatWorker = real_worker

    print(f"\n结果：{len(PASS)} 通过 / {len(FAIL)} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
