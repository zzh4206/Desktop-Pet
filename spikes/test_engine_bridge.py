"""test_engine_bridge —— 中间层（接口接入）回归锁。

验证三层结构「原有引擎 ← 中间层 ← 新引擎有效部分」：
* 原有引擎缺省（NullEnricher）恒等、零风险；
* 新引擎有效部分（MotionEnricher / ChannelEnricher）产出正确增量；
* 中间层（EngineBridge）防御性：新引擎抛错 → 永久降级恒等，不阻断不刷屏。

运行：.venv/bin/python spikes/test_engine_bridge.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pet.engine_bridge import (  # noqa: E402
    ChannelEnricher, EngineBridge, Enrichment, MotionEnricher, NullEnricher,
)
from pet.rig.spec import load_rig_spec  # noqa: E402

PASS = 0
FAIL = 0


def _spec():
    d = os.path.join("assets", "rig", "final")
    return load_rig_spec(d, "final") if os.path.isdir(d) else None


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}" + (f"  {detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"  ❌ {name}" + (f"  {detail}" if detail else ""))


# --------------------------------------------------------------------------- #
def test_null_identity() -> None:
    print("[T1] NullEnricher 恒等 + 零风险")
    n = NullEnricher()
    n.set_motion(tilt_deg=9.0, walking=True, airborne=False)
    e = n.tick(33.0)
    check("恒等 body_angle=0", e.body_angle == 0.0)
    check("恒等 body_y=0", e.body_y == 0.0)
    check("恒等 scale=1", e.scale_x == 1.0 and e.scale_y == 1.0)
    check("恒等 blink=False", e.blink_on is False)
    check("恒等 shadow=0", e.shadow_alpha == 0.0)


def test_motion_enrichment() -> None:
    print("[T2] MotionEnricher 产出非恒等增强（呼吸浮动 / 眨眼）")
    m = MotionEnricher(_spec())
    ys = set()
    blink_seen = False
    for _ in range(400):                      # 400 × 33ms ≈ 13.2s
        e = m.tick(33.0)
        ys.add(round(e.body_y, 2))
        blink_seen = blink_seen or e.blink_on
    check("body_y 浮动（呼吸）", len(ys) > 3, f"{len(ys)} 个不同值")
    check("眨眼脉冲出现", blink_seen)


def test_motion_squash_edge() -> None:
    print("[T3] airborne 下降沿 → squash 压缩（scale_y < 1）")
    m = MotionEnricher(_spec())
    m.set_motion(airborne=True)
    for _ in range(10):
        m.tick(33.0)
    m.set_motion(airborne=False)              # 落地下降沿 → trigger_squash
    valleys = []
    for _ in range(20):
        e = m.tick(33.0)
        valleys.append(e.scale_y)
    check("出现过压缩", min(valleys) < 0.98, f"谷={min(valleys):.3f}")
    check("有过冲回弹", max(valleys) > 1.0, f"峰={max(valleys):.3f}")


def test_channel_enricher() -> None:
    print("[T4] ChannelEnricher 出风调制 + 阴影（静态兜底，确定性）")
    # enabled=False → StaticWindSource(1.0) / StaticSunSource(0.35)，无网络无时变
    cfg = {"wind": {"enabled": False},
           "sun": {"enabled": False, "shadow_alpha": 0.35}}
    c = ChannelEnricher(cfg)
    gain, bias = c.wind()
    sh = c.shadow()
    check("风增益数值", gain > 0.0, f"gain={gain:.2f}")
    check("顺风偏置有值", isinstance(bias, float))
    check("阴影有 alpha", sh.shadow_alpha > 0.0, f"alpha={sh.shadow_alpha:.2f}")


def test_bridge_original_fallback() -> None:
    print("[T5] EngineBridge(Null) → 恒等 + active=original")
    b = EngineBridge(None)
    b.set_motion(tilt_deg=5.0, walking=True)
    e = b.tick(33.0)
    check("active=original", b.active == "original")
    check("恒等输出", e.body_angle == 0.0 and e.scale_y == 1.0)
    check("无通道 wind() 回退 (1.0, 0.0)", b.wind() == (1.0, 0.0))


def test_bridge_motion_pass() -> None:
    print("[T6] EngineBridge(Motion) → 非恒等 + active=MotionEnricher")
    b = EngineBridge(MotionEnricher(_spec()))
    b.set_motion(tilt_deg=5.0)
    e = b.tick(33.0)
    check("active=MotionEnricher", b.active == "MotionEnricher")
    check("非恒等输出", e.body_angle != 0.0 or e.body_y != 0.0)


class _BoomEnricher(NullEnricher):
    def set_motion(self, **kw):
        raise RuntimeError("boom")

    def tick(self, dt_ms):
        raise RuntimeError("boom")


def test_bridge_defensive_degrade() -> None:
    print("[T7] EngineBridge 防御性：抛错 → 永久降级恒等，不阻断")
    b = EngineBridge(_BoomEnricher())
    b.set_motion(tilt_deg=1.0)                # 内部抛错，应被吞掉
    e = b.tick(33.0)                          # 内部抛错，应被吞掉
    check("降级标记", b.degraded is True)
    check("active 回 original", b.active == "original")
    check("tick 仍返回恒等", e.body_angle == 0.0)
    check("不阻断（无异常外抛）", True)


def test_bridge_full_shadow() -> None:
    print("[T8] EngineBridge(motion + channels) → 增强 + 阴影叠加")
    cfg = {"sun": {"enabled": False, "shadow_alpha": 0.35}}   # 静态阴影，确定性
    b = EngineBridge(MotionEnricher(_spec()), ChannelEnricher(cfg))
    b.refresh_channels()
    wg, wb = b.wind()
    check("wind() 取值（静态微风）", wg > 0.0 and isinstance(wb, float),
          f"gain={wg:.2f}")
    b.set_motion(tilt_deg=3.0)
    e = b.tick(33.0)
    check("motion 非恒等", e.body_angle != 0.0 or e.body_y != 0.0)
    check("阴影被叠加", e.shadow_alpha > 0.0, f"alpha={e.shadow_alpha:.2f}")


def test_determinism() -> None:
    print("[T9] 确定性：同输入序列 → 同输出序列（reset 后复现）")
    spec = _spec()
    a = MotionEnricher(spec)
    b = MotionEnricher(spec)
    seq_a = [a.tick(33.0).body_y for _ in range(50)]
    seq_b = [b.tick(33.0).body_y for _ in range(50)]
    check("两次同序列 body_y 一致", seq_a == seq_b)


def main() -> int:
    test_null_identity()
    test_motion_enrichment()
    test_motion_squash_edge()
    test_channel_enricher()
    test_bridge_original_fallback()
    test_bridge_motion_pass()
    test_bridge_defensive_degrade()
    test_bridge_full_shadow()
    test_determinism()
    print(f"\nengine_bridge 中间层: {PASS} 通过, {FAIL} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
