"""v0.6 主动关怀自动验证（win 主笔）—— 假时钟驱动，无 Qt/网络。

覆盖版本规划 v0.6 Must：久坐提醒 / 唤醒间隔钳制 10-360 / 深夜不打扰 /
follow-up 定时 / 隔离上下文日志(本地路径无 LLM 也走隔离决策日志)。
运行：python spikes/test_v06_proactive.py
"""

from __future__ import annotations

import sys
from datetime import datetime

sys.path.insert(0, ".")

from pet.proactive import ProactiveScheduler  # noqa: E402
from pet.pet_state import PetState, PetStateStore  # noqa: E402

PASS, FAIL = [], []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("  ✅ " if cond else "  ❌ ") + name)


class FakeClock:
    def __init__(self, iso: str) -> None:
        self.t = datetime.fromisoformat(iso).timestamp()

    def __call__(self) -> float:
        return self.t

    def advance(self, minutes: float) -> None:
        self.t += minutes * 60.0


def make(clock, idle_s=0.0, cfg=None):
    bubbles = []
    s = ProactiveScheduler(
        store=PetStateStore(PetState.default()),
        bubble_fn=bubbles.append,
        idle_fn=lambda: idle_s,
        cfg=cfg,
        now_fn=clock,
    )
    return s, bubbles


def main() -> int:
    # T1 链式唤醒：首次唤醒发气泡 + 再排下一次（本地 30-120min）
    clock = FakeClock("2026-08-17 10:00")
    s, bubbles = make(clock)
    nxt = s._next_wake_at
    check("T1 启动即排首次唤醒", nxt is not None)
    # 快进到首次唤醒（最多 120min）
    clock.advance(121)
    s.poll()
    check("T1 唤醒发关怀气泡", len(bubbles) >= 1)
    check("T1 链式再排下一次", s._next_wake_at is not None
          and s._next_wake_at > clock())

    # T2 唤醒间隔钳制 10-360
    clock = FakeClock("2026-08-17 10:00")
    s, _ = make(clock)
    s.schedule_wake(5, {})       # 过小
    d1 = s._next_wake_at - clock()
    s.schedule_wake(9999, {})    # 过大
    d2 = s._next_wake_at - clock()
    check("T2 下限钳到 10min", abs(d1 - 10 * 60) < 1)
    check("T2 上限钳到 360min", abs(d2 - 360 * 60) < 1)

    # T3 久坐提醒：空闲 ≥45min 触发，冷却 30min 内不重复
    clock = FakeClock("2026-08-17 14:00")
    s, bubbles = make(clock, idle_s=50 * 60)
    s.poll()
    check("T3 久坐触发提醒", len(bubbles) == 1)
    clock.advance(10)  # 冷却内
    s.poll()
    check("T3 冷却内不重复", len(bubbles) == 1)
    clock.advance(25)  # 过冷却
    s.poll()
    check("T3 过冷却再发(话题轮换)", len(bubbles) == 2
          and bubbles[1] != bubbles[0])
    # 空闲恢复 → 段重置（14:35 无早晚安/节日干扰）
    s, bubbles2 = make(clock, idle_s=0)
    s.poll()
    check("T3 活动中不发久坐提醒", len(bubbles2) == 0)

    # T4 深夜不打扰（23:00-08:00 静默；唤醒顺延到 quiet 结束，链不断）
    clock = FakeClock("2026-08-17 23:30")
    s, bubbles = make(clock, idle_s=999 * 60)  # 深夜即使久坐也不发
    s.poll()
    check("T4 深夜久坐静默", len(bubbles) == 0)
    # N3：schedule_wake(1) 钳到 10min → 23:40 触发，仍在深夜 quiet 内
    s.schedule_wake(1, {})
    clock.advance(10)  # 到 23:40，仍在 quiet
    s.poll()
    # 顺延：不发，且重排到 quiet 结束（非丢弃，链不断）
    check("T4 深夜唤醒顺延不发且未丢(_next_wake_at 重排)",
          len(bubbles) == 0 and s._next_wake_at is not None)
    # 顺延到次日 08:00 后补发（链条恢复）
    clock.advance(8 * 60 + 20)  # 23:40 → 次日 08:00
    s.poll()
    check("T4 静默结束后补发唤醒(链条不断)",
          len(bubbles) >= 1 and s._next_wake_at is not None)
    # 清掉 bubbles 给后续 follow-up 测试
    bubbles.clear()
    s._next_wake_at = None
    # follow-up 例外：深夜照发（用户自己约的）——用独立实例避免唤醒干扰
    clock_f = FakeClock("2026-08-17 23:50")
    s_f, bubbles_f = make(clock_f, idle_s=999 * 60)
    s_f.poll()
    s_f.follow_up("吃晚饭回来了吗？", clock_f() - 1)
    s_f.poll()
    check("T4 深夜 follow-up 照发", bubbles_f == ["吃晚饭回来了吗？"])

    # T5 follow-up 定时触发
    clock = FakeClock("2026-08-17 12:00")
    s, bubbles = make(clock)
    s.follow_up("饭点到了，去吃饭吧～", clock() + 30 * 60)
    clock.advance(29)
    s.poll()
    check("T5 未到点不发", len(bubbles) == 0)
    clock.advance(2)
    s.poll()
    check("T5 到点触发", any("吃饭" in b for b in bubbles))

    # T6 早安/晚安（每天一次）
    clock = FakeClock("2026-08-17 09:00")
    s, bubbles = make(clock, idle_s=0)
    s.poll()
    check("T6 早安一次", sum("早上好" in b for b in bubbles) == 1)
    s.poll()
    check("T6 同日不重复", sum("早上好" in b for b in bubbles) == 1)
    clock2 = FakeClock("2026-08-17 22:00")
    s2, b2 = make(clock2, idle_s=0)
    s2.poll()
    check("T6 晚安", any("休息" in b for b in b2))

    # T7 节日（10-01 国庆）
    clock = FakeClock("2026-10-01 10:00")
    s, bubbles = make(clock, idle_s=0)
    s.poll()
    check("T7 节日祝福", any("国庆" in b for b in bubbles))

    # T8 LLM 隔离决策：假客户端返回 JSON → 采纳；坏 JSON → 本地兜底
    # v0.6.2：chat_once 加 system_override/tools_override 参数（决策隔离）
    class FakeClient:
        def __init__(self, text):
            self.text = text
            self.last_system_override = "UNSET"
            self.last_tools_override = "UNSET"

        def chat_once(self, history, ctx, on_delta=None,
                      system_override=None, tools_override=None):
            self.last_system_override = system_override
            self.last_tools_override = tools_override
            return self.text, []

    clock = FakeClock("2026-08-17 10:00")
    s, _ = make(clock)
    s._client = FakeClient('{"message": "记得喝水哦", "next_min": 55}')
    d, m = s._decide({"hour": "10:00"})
    check("T8 LLM 决策采纳", d == 55 and m == "记得喝水哦")
    # N4/N5：决策轮传 system_override=_DECISION_SYSTEM + tools_override=None
    check("T8 决策隔离 system_override 传决策指令",
          s._client.last_system_override is not None
          and "决策器" in s._client.last_system_override)
    check("T8 决策隔离 tools_override=None(不挂工具)",
          s._client.last_tools_override is None)
    s._client = FakeClient("不是JSON")
    d, m = s._decide({})
    check("T8 坏输出退本地罐头", 30 <= d <= 120 and len(m) > 0)

    # T22 N6 JSON 围栏容错：LLM 返 ```json``` 包裹的 JSON 也能解析
    s._client = FakeClient('```json\n{"message": "休息一下", "next_min": 40}\n```')
    d, m = s._decide({"hour": "10:00"})
    check("T22 JSON 围栏容错解析", d == 40 and m == "休息一下")

    # T23 N8 message 非字符串走罐头（防显示 "None"/"123"）
    s._client = FakeClient('{"message": null, "next_min": 30}')
    d, m = s._decide({"hour": "10:00"})
    check("T23 message=null 走罐头不显示None", 30 <= d <= 120 and m != "None")

    # T24 M6 修（REVIEW-2026-08-27）：shutdown 收口在飞决策 worker——
    # cancel 中断 + wait 退出 + 引用清空，不抛不挂（旧版退出时 wake_worker
    # 挂 120s read，QThread 随 GC destroyed-while-running）
    import time as _time

    class SlowClient:
        _resp = None   # cancel() 探测的流式引用（无在飞流）

        def chat_once(self, history, ctx, on_delta=None,
                      system_override=None, tools_override=None):
            _time.sleep(0.3)
            return '{"message": "慢决策", "next_min": 30}', []

    clock = FakeClock("2026-08-17 10:00")
    s, _ = make(clock)
    s._client = SlowClient()
    clock.advance(121)   # 到首次唤醒点 → _fire_wake 起 worker
    s.poll()
    check("T24 决策 worker 在飞", s._wake_worker is not None)
    s.shutdown()
    check("T24 shutdown 收口 worker（引用清空不挂）", s._wake_worker is None)
    s.shutdown()   # 幂等：二次调用 no-op
    check("T24 shutdown 幂等", s._wake_worker is None)

    # ---- T25 批次H/M10（REVIEW-2026-08-31 F32）：异步决策链真跑 ----
    # worker 与 _decide 同路径后的端到端：LLM JSON → done 信号 → 气泡+排程；
    # 垃圾输出 → _decide 内部罐头；_on_wake_failed → 罐头。旧版 T24 无
    # QCoreApplication，done 信号永不送达（生产/测试双轨的实证盲区）
    from PySide6.QtCore import QCoreApplication

    _qapp = QCoreApplication.instance() or QCoreApplication(sys.argv)

    def _pump(ms: int) -> None:
        """跨线程信号投递泵事件循环。实测（批次H）：QTest.qWait 在
        QCoreApplication 下不投递 queued 信号，QApplication 下可——
        显式泵两态通吃，不再依赖该差异"""
        import time as _t
        t0 = _t.monotonic()
        while (_t.monotonic() - t0) * 1000 < ms:
            _qapp.processEvents()
            _t.sleep(0.005)

    class JsonClient:
        _resp = None

        def __init__(self, text):
            self._text = text

        def chat_once(self, history, ctx, on_delta=None,
                      system_override=None, tools_override=None):
            # 决策链契约校验：system_override 隔离 + 不挂工具
            assert system_override and tools_override is None
            return self._text, []

    clock25 = FakeClock("2026-08-17 10:00")
    s25, bubbles25 = make(clock25)
    s25._client = JsonClient('{"message": "记得喝水哦", "next_min": 30}')
    s25._fire_wake(clock25())
    _pump(400)   # worker 完成 + done 信号经事件循环送达
    check("T25a 异步决策链：LLM 气泡送达", bubbles25 == ["记得喝水哦"])
    check("T25b 异步链排定下次唤醒（30min 后）",
          s25._next_wake_at is not None
          and abs(s25._next_wake_at - (clock25() + 1800)) < 1)
    s25.shutdown()

    # 垃圾 JSON → _decide 内部回退罐头（链不断）
    s26, bubbles26 = make(FakeClock("2026-08-17 10:00"))
    s26._client = JsonClient("这不是 JSON")
    s26._fire_wake(s26._now())
    _pump(400)
    check("T25c 垃圾决策输出回退本地罐头（气泡非空+链不断）",
          len(bubbles26) == 1 and s26._next_wake_at is not None)
    s26.shutdown()

    # failed 槽直测（decide_fn 自身崩溃的兜底路径）
    s27, bubbles27 = make(FakeClock("2026-08-17 10:00"))
    s27._on_wake_failed({"hour": "10:00"})
    check("T25d _on_wake_failed 回退罐头 + 链不断",
          len(bubbles27) == 1 and s27._next_wake_at is not None)
    s27.shutdown()

    # ---- 批次B/P2-6（REVIEW-2026-09-05）：重启状态持久化 ----
    import os as _os
    import tempfile as _tmpmod

    _pdir = _tmpmod.mkdtemp(prefix="pet_p26_")
    _ppath = _os.path.join(_pdir, "proactive_state.json")
    clock_p = FakeClock("2026-08-17 10:00")
    sp28 = ProactiveScheduler(
        store=PetStateStore(PetState.default()), bubble_fn=lambda t: None,
        idle_fn=lambda: 0.0, cfg={}, now_fn=clock_p, state_path=_ppath)
    sp28._greeted["morning"] = datetime.fromisoformat("2026-08-17").date()
    sp28.follow_up("饭点到了～", clock_p() + 1800)
    sp28.schedule_wake(45, {"test": True})
    _wk = sp28._next_wake_at
    sp28.shutdown()

    # 重启：新实例从档案恢复（问候/回访/唤醒链不再重排/丢失）
    sp28b = ProactiveScheduler(
        store=PetStateStore(PetState.default()), bubble_fn=lambda t: None,
        idle_fn=lambda: 0.0, cfg={}, now_fn=clock_p, state_path=_ppath)
    check("P2-6a follow-up 跨重启恢复",
          any(m == "饭点到了～" and abs(w - (clock_p() + 1800)) < 1
              for w, m in sp28b._followups))
    check("P2-6b 唤醒链跨重启保留（不重排 boot 唤醒）",
          sp28b._next_wake_at is not None
          and abs(sp28b._next_wake_at - _wk) < 1)
    check("P2-6c 已问候标记跨重启保留",
          sp28b._greeted["morning"]
          == datetime.fromisoformat("2026-08-17").date())
    sp28b.shutdown()

    clock_p.advance(120)  # 2h 后再重启：过期 follow-up 不复活
    sp28c = ProactiveScheduler(
        store=PetStateStore(PetState.default()), bubble_fn=lambda t: None,
        idle_fn=lambda: 0.0, cfg={}, now_fn=clock_p, state_path=_ppath)
    check("P2-6d 过期 follow-up 不复活",
          all(w > clock_p() for w, _m in sp28c._followups))
    sp28c.shutdown()
    import shutil as _sh
    _sh.rmtree(_pdir, ignore_errors=True)

    print(f"\n结果：{len(PASS)} 通过 / {len(FAIL)} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
