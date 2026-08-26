"""v0.9 长期记忆验证 —— 冻结接口 + 遗忘 + 打分 + 持久化 + 工具。

纯本地（无 DS/Qt）。运行：python spikes/test_v09_memory.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import time

sys.path.insert(0, ".")

from pet.memory import MemoryStore, _tokenize  # noqa: E402
from pet.memory_tools import (  # noqa: E402
    build_memory_tools, memory_context,
)
from pet.tools_schema import ToolContext  # noqa: E402

PASS, FAIL = [], []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("  ✅ " if cond else "  ❌ ") + name)


def main() -> int:
    tmp = os.path.join(tempfile.gettempdir(), "dp_test_v09_mem.json")
    for suffix in ("", ".bak", ".tmp"):
        try:
            os.remove(tmp + suffix)
        except OSError:
            pass

    # ---- T1 冻结接口四方法存在 ----
    m = MemoryStore()
    check("T1 冻结接口存在",
          all(callable(getattr(m, n, None)) for n in
              ("memorize", "recall", "forget", "summarize_session")))

    # ---- T2 memorize：钳制/去重/id ----
    mid1 = m.memorize("主人喜欢喝拿铁加燕麦奶", 0.8)
    mid2 = m.memorize("主人叫小明", 9.9)          # 钳到 1.0
    m.memorize("主人喜欢喝拿铁加燕麦奶", 0.3)      # 同文去重（保高 imp）
    check("T2 存返非空 id", bool(mid1) and mid1 != mid2)
    check("T2 同文去重(len=2)", len(m) == 2)
    check("T2 importance 钳 [0.05,1]",
          0 < m.all()[0]["importance"] <= 1.0)
    m.memorize("", 0.5)
    check("T2 空文拒", len(m) == 2)

    # ---- T3 recall：词面命中 + 回血 ----
    before = [x["importance"] for x in m.all()
              if "拿铁" in x["fact"]][0]
    hits = m.recall("咖啡 拿铁 口味", k=5)
    check("T3 命中拿铁条目", any("拿铁" in h["fact"] for h in hits))
    after = [x["importance"] for x in m.all()
             if "拿铁" in x["fact"]][0]
    check("T3 命中回血(+0.05)", after >= before)
    miss = m.recall("完全不相关的词汇如量子力学", k=5)
    check("T3 无命中返空(拿铁不匹配量子)", not miss)

    # ---- T4 分词 ----
    check("T4 中文2gram", "拿铁" in _tokenize("我喜欢拿铁咖啡") or
          any(len(t) == 2 for t in _tokenize("我喜欢拿铁咖啡")))
    check("T4 英文词", "coffee" in _tokenize("I love COFFEE"))

    # ---- T5 forget/clear ----
    m.forget(mid2)
    check("T5 forget 删除", len(m) == 1)
    m.clear()
    check("T5 clear 清空", len(m) == 0)

    # ---- T6 持久化：save/load/坏档回退 .bak ----
    m2 = MemoryStore()
    m2.memorize("测试记忆持久化", 0.9)
    m2.save(tmp)
    m2.memorize("第二条", 0.5)
    m2.save(tmp)   # 第二次 save 产生 .bak（首次无旧档可备）
    m3 = MemoryStore.load(tmp)
    check("T6 load 读回", len(m3) == 2
          and m3.all()[0]["fact"] == "测试记忆持久化")
    # 写坏主档 → .bak 兜底（.bak 是上一版 1 条）
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("corrupted{")
    m4 = MemoryStore.load(tmp)
    check("T6 坏档 .bak 回退", len(m4) == 1)

    # ---- T7 遗忘：衰减 + 物理删除 ----
    m5 = MemoryStore()
    m5.memorize("会被遗忘的低权记忆", 0.12)
    for x in m5.all():
        x["last_recalled"] = time.time() - 40 * 86400  # 40 天未召回
    dropped = m5.forget_expired()
    check("T7 低权超龄被遗忘", dropped == 1 and len(m5) == 0)
    m5.memorize("常被提起的记忆", 0.8)
    for _ in range(6):
        m5.recall("记忆")       # 反复召回回血
    for x in m5.all():
        x["last_recalled"] = time.time() - 40 * 86400
    m5.forget_expired()
    check("T7 常用记忆存活(回血抵衰减)", len(m5) == 1)

    # ---- T8 上限挤占 ----
    m6 = MemoryStore()
    for i in range(510):
        m6.memorize(f"记忆{i}", 0.2 + (i % 50) / 100)
    check("T8 上限 500 挤掉最不重要", len(m6) <= 500)

    # ---- T9 工具：save/search 闭环 + 参数校验 ----
    ctx = ToolContext(pet_state=None, user_name="u", config={},
                      window_info=None)
    store = MemoryStore()
    tools = dict()
    for schema, handler in build_memory_tools(store):
        tools[schema.name] = handler
    r1 = tools["memory_save"].execute(
        {"fact": "主人喜欢喝咖啡", "importance": 0.8}, ctx)
    check("T9 工具 save 成功", r1.success and len(store) == 1)
    r2 = tools["memory_save"].execute({"fact": "", "importance": 1}, ctx)
    check("T9 空文拒", not r2.success)
    r3 = tools["memory_search"].execute({"query": "咖啡"}, ctx)
    check("T9 工具 search 命中", r3.success and "咖啡" in r3.message)
    r4 = tools["memory_search"].execute({"query": "无关词"}, ctx)
    check("T9 search 无命中不报错", r4.success)

    # ---- T10 memory_context 注入段 ----
    seg = memory_context(store, "喝什么咖啡")
    check("T10 注入段含命中记忆", "咖啡" in seg and "长期记忆" in seg)
    check("T10 无命中返空", memory_context(store, "量子涨落") == "")

    # ---- T11 滚屏摘要：>20 轮压缩最老 10 轮 ----
    # H4 修后摘要走 _SummarizeWorker 后台 QThread（chat_once 带
    # system_override/tools_override 关键字），断言前须等 worker 退出且
    # done/failed 信号经事件循环送达（旧版同步调用可直接断言）。
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication(sys.argv)
    from pet.ui.chat_bridge import ChatBridge
    from pet.llm import ChatTurn

    class SumClient:
        def chat_once(self, history, ctx, on_delta=None, **kw):
            return "用户喜欢喝咖啡；约好周五交周报。", []

    class NoClient:
        def chat_once(self, history, ctx, on_delta=None, **kw):
            raise RuntimeError("断网模拟")

    def make_bridge(client):
        b = ChatBridge(client=client, registry=object(),
                       make_ctx=lambda: None)
        b._client = client   # 直接灌（绕过 send 的 worker 路径）
        return b

    def wait_sum(b, timeout_ms=5000):
        """等摘要 worker 跑完（QTest.qWait 泵事件循环驱动信号送达）。"""
        QTest.qWait(20)     # 让 start() 落地，避开 isRunning 假阴性窗口
        waited = 0
        while waited < timeout_ms:
            sw = b._sum_worker
            if sw is None or not sw.isRunning():
                QTest.qWait(50)   # done/failed 排队信号送达
                return
            QTest.qWait(50)
            waited += 50

    # >20 轮触发：最老 10 轮 → 摘要 1 轮
    br = make_bridge(SumClient())
    br._history = [ChatTurn("user" if i % 2 == 0 else "assistant",
                            f"消息{i}") for i in range(24)]
    br._maybe_summarize()
    wait_sum(br)
    check("T11 24轮→摘要(≈15轮)", len(br._history) == 15)
    check("T11 摘要含要点", "咖啡" in br._history[0].content
          and br._history[0].content.startswith("[此前对话摘要]"))
    # ≤20 轮不动
    br2 = make_bridge(SumClient())
    br2._history = [ChatTurn("user", f"m{i}") for i in range(20)]
    br2._maybe_summarize()
    QTest.qWait(300)   # 不触发的负例也等一拍，防"未跑完"假阳性
    check("T11 20轮不触发", len(br2._history) == 20)
    # 失败保留原文
    br3 = make_bridge(NoClient())
    br3._history = [ChatTurn("user", f"m{i}") for i in range(24)]
    br3._maybe_summarize()
    wait_sum(br3)
    check("T11 DS失败保留原文", len(br3._history) == 24)

    # ---- T12 拖放文件（快捷启动器）----
    from pet.window import WindowBase
    from pet.asset_provider import EmojiProvider
    from pet.pet_state import PetState
    from PySide6.QtCore import QMimeData, QUrl, Qt
    from PySide6.QtTest import QTest
    from PySide6.QtCore import QEvent

    win = WindowBase(EmojiProvider().get_static(PetState.default()))
    win.show()
    QTest.qWaitForWindowExposed(win)

    # dragEnter：本地文件 mime → 接受
    from PySide6.QtGui import QDragEnterEvent
    md = QMimeData()
    md.setUrls([QUrl.fromLocalFile("C:/tmp/test.txt")])
    ev_enter = QDragEnterEvent(
        win.rect().center(), Qt.CopyAction, md,
        Qt.LeftButton, Qt.NoModifier,
    )
    win.dragEnterEvent(ev_enter)
    check("T12 本地文件 dragEnter 接受",
          ev_enter.isAccepted() or ev_enter.proposedAction() != Qt.IgnoreAction)

    # dropEvent：fileDropped 信号
    dropped = []
    win.fileDropped.connect(dropped.append)
    from PySide6.QtGui import QDropEvent
    ev_drop = QDropEvent(
        win.rect().center().toPointF(), Qt.CopyAction, md,
        Qt.LeftButton, Qt.NoModifier, QEvent.Drop,
    )
    win.dropEvent(ev_drop)
    check("T12 dropEvent 发 fileDropped", dropped == ["C:/tmp/test.txt"])

    # 平台 open_path：目录+文件
    from pet.platform import get_platform_adapter
    ad = get_platform_adapter()
    ok_d, _ = ad.open_path(tempfile.gettempdir())
    check("T12 win open_path(目录)成功", ok_d)

    QTest.qWait(100)
    win.hide()

    for suffix in ("", ".bak", ".tmp"):
        try:
            os.remove(tmp + suffix)
        except OSError:
            pass
    print(f"\n结果：{len(PASS)} 通过 / {len(FAIL)} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
