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

    # T4 深夜不打扰（23:00-08:00 静默；唤醒顺延）
    clock = FakeClock("2026-08-17 23:30")
    s, bubbles = make(clock, idle_s=999 * 60)  # 深夜即使久坐也不发
    s.poll()
    check("T4 深夜久坐静默", len(bubbles) == 0)
    s.schedule_wake(1, {})
    clock.advance(2)
    s.poll()
    check("T4 深夜唤醒顺延不发", len(bubbles) == 0)
    # follow-up 例外：深夜照发（用户自己约的）
    s.follow_up("吃晚饭回来了吗？", clock() - 1)
    s.poll()
    check("T4 深夜 follow-up 照发", bubbles == ["吃晚饭回来了吗？"])

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
    class FakeClient:
        def __init__(self, text):
            self.text = text

        def chat_once(self, history, ctx, on_delta=None):
            return self.text, []

    clock = FakeClock("2026-08-17 10:00")
    s, _ = make(clock)
    s._client = FakeClient('{"message": "记得喝水哦", "next_min": 55}')
    d, m = s._decide({"hour": "10:00"})
    check("T8 LLM 决策采纳", d == 55 and m == "记得喝水哦")
    s._client = FakeClient("不是JSON")
    d, m = s._decide({})
    check("T8 坏输出退本地罐头", 30 <= d <= 120 and len(m) > 0)

    print(f"\n结果：{len(PASS)} 通过 / {len(FAIL)} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
