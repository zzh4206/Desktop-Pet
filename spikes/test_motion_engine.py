"""motion.py 运动引擎单测（纯 Python，无 Qt/无网络）。

这是 P1「等价迁移」的验收物：证明 MotionEngine 的缺省输出与
rig_scene.qml 现有公式逐值一致（呼吸/眨眼/落地 squash/步态相位与包络/
sway·limb·blink 部件角度），并锁定 P2 弹簧积木的收敛/回弹性质。

覆盖：
  M1  呼吸/眨眼/落地 squash 公式与常数
  M2  步态相位累加器：hz 变化只改斜率不瞬移（对齐 v14 rM1）
  M3  gaitK 包络渐起/渐收 + 收敛钳制
  M4  部件角度：sway 绝对时间正弦 / limb 相位模式 / limb 缺省 hz 模式 / blink=0
  M5  body 变换合成（镜像、squash 压缩、呼吸）
  M6  确定性：同输入序列 → 同输出；reset 归零
  M7  弹簧积木：spring_from_frequency 换算 + spring_step 欠阻尼过冲/过阻尼不过冲
运行：
  python -X utf8 spikes/test_motion_engine.py
"""

from __future__ import annotations

import math
import os
import sys

sys.path.insert(0, ".")

from pet.rig.motion import (   # noqa: E402
    MotionEngine,
    MotionInputs,
    spring_from_frequency,
    spring_step,
)
from pet.rig.spec import RigPart, RigSpec   # noqa: E402

PASS, FAIL = [], []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("  ✅ " if cond else "  ❌ ") + name)


def _part(pid, kind, amp=0.0, period=2600.0, phase=0.0, base=0.0,
          parent="", freq=0.0, zeta=0.7):
    return RigPart(id=pid, path="", source_figure="f",
                   px_rect=(0, 0, 1, 1), pivot=(0, 0), z="under_core",
                   kind=kind, base_deg=base, amp_deg=amp,
                   period_ms=period, phase_ms=phase,
                   parent=parent, spring_freq_hz=freq, spring_zeta=zeta)


# 独立镜像：不 import 引擎内部，靠读 RigPart 字段重推期望值（防"同错同绿"）
def ref_part_angle(p, t, gait_phase, gait_k, gait_hz, walk_hz):
    if p.kind == "blink":
        return 0.0
    if p.kind == "limb":
        if walk_hz > 0:
            return gait_k * (p.base_deg + p.amp_deg * math.sin(
                2 * math.pi * (gait_phase + p.phase_ms / p.period_ms)))
        return gait_k * (p.base_deg + p.amp_deg * math.sin(
            2 * math.pi * (t * gait_hz / 1000.0 + p.phase_ms / p.period_ms)))
    ph = (t + p.phase_ms) % p.period_ms
    return p.amp_deg * math.sin(2 * math.pi * ph / p.period_ms)


def main() -> int:
    # ---- M1 分层呼吸 / 眨眼 / squash（P3）----
    eng = MotionEngine(RigSpec(stage="final", parts=[]))
    f = eng.step(MotionInputs(), 33.0)
    check("M1a 首帧眨眼脉冲开（33ms < 130ms 窗口）", f.blink_on is True)
    # P3 分层呼吸：呼吸改「纵向浮动 body_y（3.2s）」+「左右漂 body_angle（15.5s）」
    # v0.15.1：呼吸浮动幅度 2.0 → 3.0 → 2.5（实机 3px 略大回落），公式不变
    exp_float = 2.5 * math.sin(2 * math.pi * 33.0 / 3200.0)
    exp_drift = 2.0 * math.sin(2 * math.pi * 33.0 / 15500.0)
    check("M1b 呼吸纵向浮动公式对齐（body_y，scaleY 恒 1）",
          abs(f.body_y - exp_float) < 1e-12 and abs(f.body_scale_y - 1.0) < 1e-12)
    check("M1c 静止 = 超低频左右漂（body_angle）+ scaleX 不变化",
          abs(f.body_angle - exp_drift) < 1e-12
          and abs(f.body_scale_x - 1.0) < 1e-12)

    # P3 眨眼：随机间隔 3–6s（确定性 PRNG）——推进 400 拍提取上升沿
    eng.reset()
    edges, prev = [], False
    for _ in range(400):                  # 13.2s
        fr = eng.step(MotionInputs(), 33.0)
        if fr.blink_on and not prev:
            edges.append(eng.t_ms)
        prev = fr.blink_on
    gaps = [edges[i + 1] - edges[i] for i in range(len(edges) - 1)]
    check(f"M1d 眨眼随机间隔落在 [3s,6s]（{len(edges)} 次："
          f"{[round(g) for g in gaps]}）",
          len(edges) >= 3 and all(3000.0 <= g - 130.0 <= 6000.0 for g in gaps))
    # 确定性：reset 后同输入 → 同眨眼上升沿序列
    eng.reset()
    edges2, prev = [], False
    for _ in range(400):
        fr = eng.step(MotionInputs(), 33.0)
        if fr.blink_on and not prev:
            edges2.append(eng.t_ms)
        prev = fr.blink_on
    check("M1e 眨眼序列确定性（reset 后同上升沿）", edges == edges2)

    # P3 squash：欠阻尼弹簧——压缩→过冲拉伸→settle（体积守恒 scaleX=1/scaleY）
    eng.reset()
    eng.step(MotionInputs(), 1000.0)
    eng.trigger_squash()
    sy_min, sy_max, vol_ok = 1.0, 1.0, True
    for _ in range(8):                    # 8 拍≈264ms 覆盖压缩谷 + 过冲峰
        fr = eng.step(MotionInputs(), 33.0)
        sy_min = min(sy_min, fr.body_scale_y)
        sy_max = max(sy_max, fr.body_scale_y)
        if abs(fr.body_scale_x * fr.body_scale_y - 1.0) > 1e-9:
            vol_ok = False
    check(f"M1f squash 落地压缩（scaleY 谷 {sy_min:.4f} ∈ [0.85,0.95)）",
          0.85 <= sy_min < 0.95)
    check("M1g squash 体积守恒（scaleX·scaleY=1）", vol_ok)
    check(f"M1h squash 过冲拉伸（scaleY 峰 {sy_max:.4f} > 1.02）", sy_max > 1.02)
    for _ in range(22):                   # 累计 > 1s，弹簧应 settle
        fr = eng.step(MotionInputs(), 33.0)
    check(f"M1i squash 弹簧 settle 归零（scaleY={fr.body_scale_y:.4f} ≈ 1）",
          abs(fr.body_scale_y - 1.0) < 2e-3)

    # ---- M2/M3 步态相位 + 包络 ----
    eng.reset()
    eng.step(MotionInputs(walking=True, walk_hz=1.3), 400.0)
    ph0 = eng.gait_phase
    eng.step(MotionInputs(walking=True, walk_hz=2.0), 150.0)
    adv = (eng.gait_phase - ph0) % 1.0
    check(f"M2 相位累加器 hz 1.3→2.0 连续推进（150ms 实推 {adv:.3f}）",
          0.28 < adv < 0.32)                # 期望 2.0*0.15=0.30 周期

    eng.reset()
    eng.step(MotionInputs(walking=True), 450.0)
    check("M3a 起步包络渐起（3τ 后 >0.9）", eng.gait_k > 0.9)
    eng.step(MotionInputs(walking=False), 500.0)
    check("M3b 停步包络渐收（>0.4s 后 <0.1）", eng.gait_k < 0.1)
    # 引擎契约：dt 为逻辑固定拍（对齐 QML interval=33），收敛须按 33ms 步进
    for _ in range(60):                     # ~2s 足够收敛并被钳制
        eng.step(MotionInputs(walking=True), 33.0)
    check("M3c 包络收敛钳制到 1", abs(eng.gait_k - 1.0) < 1e-12)

    # ---- M4 部件角度（对照独立镜像，逐步比对）----
    parts = [
        _part("tail", "sway", amp=4.0, period=2600.0, phase=1300.0),
        _part("leg_l", "limb", amp=7.0, period=2600.0, phase=0.0),
        _part("leg_r", "limb", amp=7.0, period=2600.0, phase=1300.0, base=2.0),
        _part("blink", "blink"),
    ]
    eng = MotionEngine(RigSpec(stage="final", parts=parts))

    # 镜像状态推进器
    t = 0.0
    gait_phase = 0.0
    gait_k = 0.0
    ok = True
    seq = [
        (MotionInputs(), 33.0),
        (MotionInputs(walking=True, walk_hz=0.0), 100.0),
        (MotionInputs(walking=True, walk_hz=1.3), 50.0),
        (MotionInputs(walking=False, walk_hz=1.3), 77.0),
        (MotionInputs(walking=True, walk_hz=2.0), 21.0),
    ]
    for (inp, dt) in seq:
        frame = eng.step(inp, dt)
        t += dt
        ghz = inp.walk_hz if inp.walk_hz > 0 else 1.3
        gait_phase = (gait_phase + ghz * dt / 1000.0) % 1.0
        target = 1.0 if inp.walking else 0.0
        gait_k += (target - gait_k) * (dt / 150.0)
        if abs(gait_k - target) < 0.01:
            gait_k = target
        for p in parts:
            exp = ref_part_angle(p, t, gait_phase, gait_k, ghz, inp.walk_hz)
            got = frame.part_angles[p.id]
            if abs(got - exp) > 1e-9:
                ok = False
                print(f"    [mismatch] {p.id}@{t}ms: got {got} exp {exp}")
    check("M4a sway/limb/blink 部件角度逐帧对齐镜像（5 拍混合输入）", ok)
    check("M4b blink 部件角度恒 0（显隐脉冲不旋转）",
          frame.part_angles["blink"] == 0.0)

    # ---- M5 body 变换合成 ----
    eng.reset()
    fr = eng.step(MotionInputs(tilt_deg=3.5, walking=False, facing=-1), 33.0)
    check("M5a 镜像合成（scale_x 带 facing 符号）", fr.body_scale_x < 0)
    check("M5b 倾斜弹簧首拍平滑（0 < angle < 目标）",
          0.0 < fr.body_angle < 3.5)
    for _ in range(20):
        fr = eng.step(MotionInputs(tilt_deg=3.5, walking=False, facing=-1), 33.0)
    drift = 2.0 * math.sin(2 * math.pi * eng.t_ms / 15500.0)
    check(f"M5c 倾斜弹簧收敛到目标（{fr.body_angle - drift:.4f} ≈ 3.5）",
          abs(fr.body_angle - drift - 3.5) < 0.01)

    # ---- M6 确定性 ----
    a = MotionEngine(RigSpec(stage="final", parts=parts))
    b = MotionEngine(RigSpec(stage="final", parts=parts))
    same = True
    for i in range(200):
        inp = MotionInputs(walking=(i % 5 == 0), walk_hz=1.3 + (i % 3) * 0.5,
                           tilt_deg=(i % 7) * 0.5)
        fa = a.step(inp, 33.0)
        fb = b.step(inp, 33.0)
        if (fa.body_angle, fa.body_y, fa.body_scale_x, fa.blink_on,
                fa.part_angles) != (fb.body_angle, fb.body_y, fb.body_scale_x,
                                    fb.blink_on, fb.part_angles):
            same = False
            break
    check("M6a 同输入序列 → 逐帧同输出（确定性）", same)
    a.reset()
    check("M6b reset 归零（t/相位/包络/squash）",
          a.t_ms == 0.0 and a.gait_phase == 0.0 and a.gait_k == 0.0)

    # ---- M7 弹簧积木 ----
    s, d = spring_from_frequency(2.0, 0.10)
    exp_s = (2 * math.pi * 2.0) ** 2
    exp_d = 4 * math.log(2.0) / 0.10
    check("M7a spring_from_frequency 换算",
          abs(s - exp_s) < 1e-9 and abs(d - exp_d) < 1e-9)

    # 欠阻尼过冲：s=100,d=1 → ζ=0.05，从 0 追 10 必过冲
    x, v, mx = 0.0, 0.0, 0.0
    for _ in range(300):
        x, v = spring_step(x, v, 10.0, 100.0, 1.0, 16.0)
        mx = max(mx, x)
    check(f"M7b 欠阻尼过冲（峰值 {mx:.2f} > 10）", mx > 10.0 and abs(x - 10.0) < 0.5)
    # 过阻尼不过冲：s=25,d=20 → ζ=2（d·dt=0.32<1，半隐式欧拉稳定），单调逼近
    x2, v2 = 0.0, 0.0
    mono = True
    for _ in range(800):
        x2, v2 = spring_step(x2, v2, 10.0, 25.0, 20.0, 16.0)
        if x2 > 10.0 + 1e-9:
            mono = False
    check(f"M7c 过阻尼单调不过冲（终值 {x2:.3f}）", mono and abs(x2 - 10.0) < 1e-3)

    # ---- M8 弹簧引入滞后（P2）----
    eng = MotionEngine(RigSpec(stage="final", parts=[
        _part("tail_sine", "sway", amp=10.0, period=1000.0),
        _part("tail_spring", "sway", amp=10.0, period=1000.0,
              freq=0.5, zeta=1.0),           # 慢弹簧（临界阻尼）跟不上 1Hz 正弦
    ]))
    fr = None
    for _ in range(3):
        fr = eng.step(MotionInputs(), 33.0)
    sine_a = fr.part_angles["tail_sine"]
    spring_a = fr.part_angles["tail_spring"]
    check(f"M8a 弹簧滞后（正弦 {sine_a:.2f} / 弹簧 {spring_a:.2f}）",
          abs(spring_a) < abs(sine_a) * 0.6)
    mx = 0.0
    for _ in range(200):
        fr = eng.step(MotionInputs(), 33.0)
        mx = max(mx, abs(fr.part_angles["tail_spring"]))
    check(f"M8b 弹簧有界不发散（峰值 {mx:.2f} ≤ 10.5）", mx <= 10.5)

    # ---- M9 父子链 + 环守卫（P2）----
    eng = MotionEngine(RigSpec(stage="final", parts=[
        _part("head", "sway", amp=5.0, period=2600.0),
        _part("hair", "sway", amp=2.0, period=2600.0, parent="head"),
    ]))
    fr = eng.step(MotionInputs(), 33.0)
    t = eng.t_ms
    head_own = 5.0 * math.sin(2 * math.pi * (t % 2600.0) / 2600.0)
    hair_own = 2.0 * math.sin(2 * math.pi * (t % 2600.0) / 2600.0)
    check("M9a 无弹簧父子链：子 = 自身 + 父（刚性继承）",
          abs(fr.part_angles["head"] - head_own) < 1e-9
          and abs(fr.part_angles["hair"] - (hair_own + head_own)) < 1e-9)
    eng = MotionEngine(RigSpec(stage="final", parts=[
        _part("a", "sway", amp=3.0, period=2600.0, parent="b"),
        _part("b", "sway", amp=4.0, period=2600.0, parent="a"),
    ]))
    fr = eng.step(MotionInputs(), 33.0)
    check("M9b 父子环不挂死（有输出且有限）",
          math.isfinite(fr.part_angles["a"]) and math.isfinite(fr.part_angles["b"]))

    # ---- M10 风通道（v0.16）：幅度倍率 + 顺风偏置 ----
    def _sway(gain, bias, facing, frames=3):
        e = MotionEngine(RigSpec(stage="final", parts=[
            _part("s", "sway", amp=10.0, period=1000.0)]))
        fr = None
        for _ in range(frames):
            fr = e.step(MotionInputs(wind_gain=gain, wind_bias_deg=bias,
                                     facing=facing), 33.0)
        return fr.part_angles["s"]
    a1 = _sway(1.0, 0.0, 1)
    a2 = _sway(2.0, 0.0, 1)
    check(f"M10a 风力倍率线性缩放（{a1:.3f} → {a2:.3f}）",
          abs(a2 - 2.0 * a1) < 1e-9)
    b1 = _sway(1.0, 5.0, 1)
    check(f"M10b 顺风偏置叠加（{b1:.3f} = 正弦 + 5）",
          abs(b1 - (a1 + 5.0)) < 1e-9)
    b2 = _sway(1.0, 5.0, -1)
    check(f"M10c 偏置随朝向翻局部（{b2:.3f} = 正弦 - 5）",
          abs(b2 - (a1 - 5.0)) < 1e-9)
    e = MotionEngine(RigSpec(stage="final", parts=[
        _part("leg", "limb", amp=5.0, period=1000.0)]))
    fr = None
    for _ in range(10):
        fr = e.step(MotionInputs(walking=True, wind_gain=3.0,
                                 wind_bias_deg=7.0), 33.0)
    leg = fr.part_angles["leg"]
    e2 = MotionEngine(RigSpec(stage="final", parts=[
        _part("leg", "limb", amp=5.0, period=1000.0)]))
    fr2 = None
    for _ in range(10):
        fr2 = e2.step(MotionInputs(walking=True), 33.0)
    check(f"M10d limb 不受风（{leg:.4f} == {fr2.part_angles['leg']:.4f}）",
          abs(leg - fr2.part_angles["leg"]) < 1e-9)

    # ---- M11 撞墙倾斜平滑翻转（v0.16：vx 突变不再瞬移身体）----
    eng = MotionEngine(RigSpec(stage="final", parts=[]))
    for _ in range(5):
        eng.step(MotionInputs(tilt_deg=9.0), 33.0)
    a_before = eng.step(MotionInputs(tilt_deg=9.0), 33.0).body_angle
    a_flip = eng.step(MotionInputs(tilt_deg=-9.0), 33.0).body_angle
    check(f"M11 撞墙倾斜平滑翻转（{a_before:.2f} → {a_flip:.2f}，不瞬移）",
          a_before > 0 and -9.0 < a_flip < a_before)

    print(f"\nmotion 引擎: {len(PASS)} 通过, {len(FAIL)} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    rc = main()
    print(f"[exit {rc}]", flush=True)
    sys.exit(rc)
