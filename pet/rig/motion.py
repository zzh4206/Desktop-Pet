"""立绘运动引擎核心 —— v0.15 提案（纯 Python，零 Qt/QML 依赖）。

把 ``rig_scene.qml`` 里散落的运动数学收口成一个确定性 ``step()``：

* **输入**：FSM 实况（倾斜目标/行走/步频/朝向）+ 资产 spec（部件列表）
* **输出**：``MotionFrame``（body 变换 + 每部件角度 + 眨眼脉冲）
* **内部状态**：时钟 t、步态相位/包络、squash 时间戳（P2 起扩展每部件弹簧）

分层约定（详见 docs/立绘动效引擎设计.md）：
* L0 常驻：呼吸 / 眨眼 / 摆件
* L1 反应：速度倾斜 / 落地 squash（squash 沿由 presenter 检测，经
  ``trigger_squash()`` 注入）
* L2 弹道：走路（步态相位累加器 + gaitK 包络）

等价铁律：本模块缺省行为与 ``rig_scene.qml`` 现有公式**逐值一致**。P1 阶段
不接渲染（rig_scene.qml 仍自跑），本模块仅作为可单测的"真相源"独立存在；
接入渲染后 v13/v14 回归必须保持全绿。确定性：同输入序列 → 同输出序列，
供 pytest 断言（落地回弹、相位连续等可直接写成测试）。
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

from .spec import RigSpec


@dataclass
class MotionInputs:
    """每 tick 喂给引擎的 FSM 实况（与 ``presenter.set_motion_params`` 对齐）。

    airborne 不在此列：落地 squash 沿由 presenter 检测，转而调用
    ``trigger_squash()``——引擎只负责"落地冲量如何衰减"，不负责"何时落地"。
    """

    tilt_deg: float = 0.0      # 速度倾斜目标角（度）
    walking: bool = False
    walk_hz: float = 0.0       # 步态频率 Hz；0=用缺省（gaitHz=1.3）
    facing: int = 1            # 1 右 / -1 左（镜像）
    wind_gain: float = 1.0     # v0.16 风通道：sway 幅度倍率（1.0=清单基准）
    wind_bias_deg: float = 0.0  # 顺风偏置（世界空间，度；引擎按 facing 翻局部）


@dataclass
class MotionFrame:
    """step() 的每帧输出（纯数据，渲染层照摆）。"""

    body_angle: float = 0.0    # 整体旋转（L0 drift + L1 tilt + L2 walkRot），度
    body_scale_x: float = 1.0  # 镜像 × 体积守恒（=1/scaleY）
    body_scale_y: float = 1.0  # squash 压缩（呼吸已改纵向浮动 body_y）
    body_y: float = 0.0        # 上下（walkBob + 呼吸浮动），显示像素
    blink_on: bool = False     # 眨眼脉冲（blink 覆盖件显隐）
    part_angles: dict = field(default_factory=dict)  # part_id -> 角度（度）


# ---------- P2 弹簧积木（暂不接入缺省渲染路径，先独立可测） ----------

def spring_from_frequency(freq_hz: float, halflife_s: float) -> tuple:
    """Daniel Holden 参数换算：刚度 s=(2πf)²，阻尼 d=4·ln2/halflife。

    用途：把"手感"参数（频率 + 半衰期）翻译成积分器的 stiffness/damping，
    避免直接调 k/b 无从下手。典型起点：头发 f=2Hz/halflife=0.10s、
    裙摆 f=1Hz/halflife=0.20s、呼吸慢摆 f=0.5Hz/halflife=0.40s。
    """
    s = (2.0 * math.pi * freq_hz) ** 2
    d = 4.0 * math.log(2.0) / halflife_s
    return s, d


def spring_step(angle: float, vel: float, target: float,
                stiffness: float, damping: float, dt_ms: float) -> tuple:
    """二阶弹簧-阻尼一步（semi-implicit Euler，单位质量，目标速度恒 0）。

    阻尼比 ζ = damping/(2√stiffness)：ζ<1 欠阻尼=过冲回弹（软感来源），
    ζ≈0.5~0.8 软但不乱抖。返回 (新 angle, 新 vel)。
    """
    dt = dt_ms / 1000.0
    vel += (stiffness * (target - angle) - damping * vel) * dt
    angle += vel * dt
    return angle, vel


class MotionEngine:
    """确定性运动引擎（纯逻辑，可 headless 单测）。"""

    # ---- L0 眨眼：随机间隔 3–6s（确定性 PRNG）+ 130ms 闭眼脉冲 ----
    _BLINK_CLOSE_MS = 130.0
    _BLINK_MIN_MS = 3000.0
    _BLINK_MAX_MS = 6000.0
    _BLINK_SEED = 20260825       # 确定性伪随机：同输入序列 → 同输出序列
    # ---- L0 分层呼吸：纵向浮动（3.2s）+ 左右漂（15.5s 超低频）----
    # 两周期非整数倍（15.5/3.2≈4.84）去掉「整身同频摆动」的机械感（设计 §4.1）
    _BREATH_FLOAT_PERIOD_MS = 3200.0
    _BREATH_FLOAT_AMP_PX = 2.5   # ±2.5px 纵向浮动（实机 3px 略大回落到 2.5）
    _DRIFT_PERIOD_MS = 15500.0
    _DRIFT_AMP_DEG = 2.0         # ±2° 超低频左右漂
    _GAIT_DEFAULT_HZ = 1.3
    _GAIT_ENV_TAU_MS = 150.0
    _WALK_ROT_AMP_DEG = 1.4
    _WALK_BOB_AMP_PX = 2.2
    # ---- L1 落地 squash：欠阻尼弹簧（ζ=0.4，压缩→过冲回弹→settle）----
    # 峰值压缩 15%（scaleY 谷 0.85，设计 §4.5 的 0.82–0.9）；scaleX=1/scaleY
    # 体积守恒（宽高反比，否则像「缩水」）。频率 2.5Hz 使 d·dt≈0.4 且
    # s·dt²≈0.27——33ms 拍下半隐式欧拉稳定、不「一帧弹回」
    _SQUASH_Y = 0.15
    _SQUASH_STIFFNESS = (2.0 * math.pi * 2.5) ** 2
    _SQUASH_DAMPING = 2.0 * 0.4 * (2.0 * math.pi * 2.5)
    # ---- L1 速度倾斜：近临界弹簧（ζ=0.85，撞墙/落地 vx 突变 → 身体平滑甩）----
    _TILT_STIFFNESS = (2.0 * math.pi * 2.8) ** 2
    _TILT_DAMPING = 2.0 * 0.85 * (2.0 * math.pi * 2.8)

    def __init__(self, spec: RigSpec | None = None):
        self._spec = spec
        self._t_ms = 0.0
        self._gait_phase = 0.0
        self._gait_k = 0.0
        self._squash_at = -1e9
        self._angles: dict[str, float] = {}   # P2 每部件弹簧状态
        self._vels: dict[str, float] = {}
        self._wind_gain = 1.0                 # v0.16 风通道（见 _own_target）
        self._wind_bias = 0.0
        self._tilt_angle = 0.0                # L1 倾斜弹簧状态（度/度·s⁻¹）
        self._tilt_vel = 0.0
        self._squash_s = 0.0                  # L1 squash 弹簧状态（无量纲）
        self._squash_v = 0.0
        self._reset_blink()                   # L0 眨眼调度器（确定性 PRNG）

    @property
    def parts(self):
        return self._spec.parts if self._spec else []

    def reset(self) -> None:
        """清空内部状态（确定性回放 / 换档重建用）。"""
        self._t_ms = 0.0
        self._gait_phase = 0.0
        self._gait_k = 0.0
        self._squash_at = -1e9
        self._angles = {}
        self._vels = {}
        self._wind_gain = 1.0
        self._wind_bias = 0.0
        self._tilt_angle = 0.0
        self._tilt_vel = 0.0
        self._squash_s = 0.0
        self._squash_v = 0.0
        self._reset_blink()

    def _reset_blink(self) -> None:
        """眨眼调度器复位：首拍即闭眼（对齐旧行为），随后随机间隔 3–6s。

        固定 seed → 同输入序列产生同输出序列（四铁则之「确定性」）。
        """
        self._blink_rng = random.Random(self._BLINK_SEED)
        self._blink_open_until = self._BLINK_CLOSE_MS
        self._blink_next_ms = self._blink_open_until + self._blink_rng.uniform(
            self._BLINK_MIN_MS, self._BLINK_MAX_MS)

    def trigger_squash(self) -> None:
        """落地冲量注入（presenter 在 airborne 下降沿调用）。

        撞击瞬间直置压缩量=1（=旧 exp 首拍量级），随后由欠阻尼弹簧回弹
        （过冲拉伸→settle），替代旧的单调指数衰减——落地不再是"戛然而止"。
        """
        self._squash_at = self._t_ms
        self._squash_s = 1.0
        self._squash_v = 0.0

    # ---- 只读快照（接线/测试观察用，与 QML 根属性同名便于对照）----
    @property
    def gait_k(self) -> float:
        return self._gait_k

    @property
    def gait_phase(self) -> float:
        return self._gait_phase

    @property
    def t_ms(self) -> float:
        return self._t_ms

    @property
    def squash_at(self) -> float:
        return self._squash_at

    def step(self, inputs: MotionInputs, dt_ms: float) -> MotionFrame:
        """推进一帧，返回 MotionFrame。顺序刻意对齐 QML onTriggered 后的
        绑定求值次序（先推进 t/相位/包络，再算派生量）。"""
        dt = max(0.0, float(dt_ms))
        self._t_ms += dt
        t = self._t_ms

        # L0 眨眼：随机间隔 3–6s（确定性 PRNG），闭眼 130ms 脉冲
        if t >= self._blink_next_ms:
            self._blink_open_until = t + self._BLINK_CLOSE_MS
            self._blink_next_ms = self._blink_open_until + \
                self._blink_rng.uniform(self._BLINK_MIN_MS, self._BLINK_MAX_MS)
        blink_on = t < self._blink_open_until

        # L2 步态：相位累加器（hz 变化只改斜率，不瞬移——v14 rM1 修）+ gaitK 包络
        gait_hz = inputs.walk_hz if inputs.walk_hz > 0 else self._GAIT_DEFAULT_HZ
        self._gait_phase = (self._gait_phase + gait_hz * dt / 1000.0) % 1.0
        target_k = 1.0 if inputs.walking else 0.0
        self._gait_k += (target_k - self._gait_k) * (dt / self._GAIT_ENV_TAU_MS)
        if abs(self._gait_k - target_k) < 0.01:
            self._gait_k = target_k

        # L0 分层呼吸：纵向浮动（3.2s）+ 左右漂（15.5s 超低频）
        breath_float = self._BREATH_FLOAT_AMP_PX * math.sin(
            2.0 * math.pi * t / self._BREATH_FLOAT_PERIOD_MS)
        drift_deg = self._DRIFT_AMP_DEG * math.sin(
            2.0 * math.pi * t / self._DRIFT_PERIOD_MS)

        # L1 落地 squash：先按触发拍原值出峰值压缩（谷 0.85 当拍可见），
        # 再推欠阻尼弹簧一步（过冲回弹→settle，下帧起恢复）。修复：旧版先
        # 弹簧后取 scaleY，触发拍被弹簧衰减一步，峰值只到 0.89 不到 0.85。
        squash_s = self._squash_s
        self._squash_s, self._squash_v = spring_step(
            self._squash_s, self._squash_v, 0.0,
            self._SQUASH_STIFFNESS, self._SQUASH_DAMPING, dt)

        # L2 走路律动（bob/rot 与步频同源）
        walk_rot = self._gait_k * math.sin(
            2.0 * math.pi * self._gait_phase) * self._WALK_ROT_AMP_DEG
        walk_bob = self._gait_k * -abs(math.sin(
            2.0 * math.pi * self._gait_phase)) * self._WALK_BOB_AMP_PX

        # L1 速度倾斜：弹簧跟随（撞墙/落地 vx 突变 → 身体平滑甩，不瞬移）
        self._tilt_angle, self._tilt_vel = spring_step(
            self._tilt_angle, self._tilt_vel, inputs.tilt_deg,
            self._TILT_STIFFNESS, self._TILT_DAMPING, dt)

        # 合成 body 变换（L0 drift + L1 tilt + L2 walkRot 三层求和互不覆盖）
        body_angle = self._tilt_angle + walk_rot + drift_deg
        scale_y = max(0.5, 1.0 - self._SQUASH_Y * squash_s)
        scale_x = inputs.facing / scale_y   # 体积守恒：scaleX = 1/scaleY
        body_y = walk_bob + breath_float

        # v0.16 风通道：存 sway 幅度倍率 + 顺风偏置（世界→局部按 facing 翻，
        # 因为 bodyScaleX=facing 已把整棵树镜像）
        self._wind_gain = float(inputs.wind_gain)
        self._wind_bias = float(inputs.wind_bias_deg) * inputs.facing

        part_angles = self._compute_part_angles(t, gait_hz, inputs.walk_hz, dt)

        return MotionFrame(
            body_angle=body_angle,
            body_scale_x=scale_x,
            body_scale_y=scale_y,
            body_y=body_y,
            blink_on=blink_on,
            part_angles=part_angles,
        )

    def _compute_part_angles(self, t: float, gait_hz: float,
                             walk_hz: float, dt: float) -> dict:
        """每部件最终角度 = 自身目标（正弦/步态）+ 父级当前角，再经弹簧。

        P2 结构：父级先算、子级后算（递归 + 环守卫）；无 spring/parent 的
        部件退化为"直接等于自身目标"，与旧正弦逐值一致。
        """
        if not self.parts:
            return {}
        by_id = {p.id: p for p in self.parts}
        own = {p.id: self._own_target(p, t, gait_hz, walk_hz)
               for p in self.parts}
        result: dict[str, float] = {}
        resolving: set[str] = set()

        def resolve(pid: str) -> float:
            if pid in result:
                return result[pid]
            if pid in resolving:               # 环守卫：断链按 0 处理
                return 0.0
            resolving.add(pid)
            p = by_id[pid]
            parent_angle = 0.0
            parent_id = getattr(p, "parent", "") or ""
            if parent_id in by_id:
                parent_angle = resolve(parent_id)
            result[pid] = self._apply_spring(
                pid, p, own[pid] + parent_angle, dt)
            resolving.discard(pid)
            return result[pid]

        for p in self.parts:
            resolve(p.id)
        return result

    def _own_target(self, p, t: float, gait_hz: float, walk_hz: float) -> float:
        """部件自身目标角（不含父级、不含弹簧）——即旧版固定正弦/步态。"""
        if p.kind == "blink":
            return 0.0                      # blink 是显隐脉冲，不旋转
        if p.kind == "limb":
            base = p.base_deg
            amp = p.amp_deg
            if walk_hz > 0:
                # 走全局相位累加器（+本件周期偏移）
                return self._gait_k * (base + amp * math.sin(
                    2.0 * math.pi * (self._gait_phase
                                     + p.phase_ms / p.period_ms)))
            # walkHz=0 部件周期缺省分支：hz 恒定，绝对时间式无瞬移风险
            return self._gait_k * (base + amp * math.sin(
                2.0 * math.pi * (t * gait_hz / 1000.0
                                 + p.phase_ms / p.period_ms)))
        # sway：常驻正弦（相位以绝对时间推进）；幅度随风力缩放 + 顺风偏置
        ph = (t + p.phase_ms) % p.period_ms
        return self._wind_gain * p.amp_deg * math.sin(
            2.0 * math.pi * ph / p.period_ms) + self._wind_bias

    def _apply_spring(self, pid: str, p, target: float, dt: float) -> float:
        """有 spring 则弹簧一步（滞后+回弹），无则直出目标（等价）。"""
        f = p.spring_freq_hz
        if f <= 0:
            return target
        stiffness = (2.0 * math.pi * f) ** 2
        damping = 2.0 * p.spring_zeta * (2.0 * math.pi * f)   # = 2ζ√s
        angle, vel = spring_step(self._angles.get(pid, target),
                                 self._vels.get(pid, 0.0),
                                 target, stiffness, damping, dt)
        self._angles[pid] = angle
        self._vels[pid] = vel
        return angle
