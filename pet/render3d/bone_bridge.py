"""骨骼桥 v1（纯逻辑，零 Qt）——语义角色驱动的程序化姿势，任意骨架可插。

解耦设计（S2.4）：
* 输入 = rig_profile sidecar（make_runtime_asset.py 从任意 skinned GLB 生成）：
  ``{"joints": [...], "roles": {语义角色→骨名}, "rest": {骨名→[px,py,pz,qx,qy,qz,qw]}}``
* 程序化动画只引用**语义角色**（hips/spine/chest/neck/head/arm_*/leg_*/foot_*），
  profile 里缺的角色直接跳过（"有则动、无则静"）——骨名零硬编码。
* 输出 = {骨名: [qx,qy,qz,qw]} **绝对**四元数（delta ⊗ rest 前乘父系），QML 只赋值。

走路侧身（v0.18.27，用户需求：3D 模型走路时侧身展示走姿、停下转回正面）：
WalkYawState 状态机（TURN_SIDE → SIDE_WALK → TURN_BACK → IDLE_YAW）管理
整体 yaw，hips 承载；转身期四肢摆幅按转身进度渐入渐出（连贯无跳变）。
 delta 在父系表达（假设局部 Y≈骨向，逆向骨架/常见人形骨架成立）；
 baked clip（S1.8 Mixamo）就绪后本模块退化为 clip 通道的角色参数化补充。
"""

from __future__ import annotations

import math

from pet.scene_contract import PoseSemantics

# 侧身角语义：契约 view_yaw_deg 在行走时携带目标朝向（右走 +90/左走 −90，
# semantic_source._walk_yaw_deg 生成）；状态机平滑过渡到该目标。
TURN_SIDE_S = 0.55     # 起步转身时长
TURN_BACK_S = 0.70     # 停下回正时长（略慢，"停下喘口气"的节奏感）


class WalkYawState:
    """走路侧身状态机（纯逻辑，可单测）：yaw 平滑逼近**契约目标角**。

    单一目标源 = 契约 view_yaw_deg（semantic_source：行走 ±90=朝向真值，
    idle=0）——本状态机只负责平滑（smoothstep 转身/回正两档节奏），
    绝不自造目标角。曾有双重计入 bug：契约 ±90 + 状态机再转 ±90=135°
    渲染（0.75 系数后）——重构后 yaw 恒等于 state.yaw。

    模式（诊断用）：TURN_SIDE（走向目标，行走节奏）/ TURN_BACK（回 0，
    停下节奏略慢）/ SIDE_WALK / IDLE_YAW（到位保持）。
    """

    def __init__(self) -> None:
        self.mode = "IDLE_YAW"
        self.yaw = 0.0            # 当前角（度）
        self._t = 0.0             # 模式内计时
        self._from = 0.0          # 段起点角
        self._target = 0.0
        self._walking = False

    def update(self, walking: bool, dt: float, target_deg: float = 0.0) -> float:
        self._walking = walking
        self._t += max(0.0, dt)
        # walk_blend 独立连续演化（不依赖模式）：行走渐升 / 停止渐降——
        # 走姿摆幅的淡入淡出系数（v0.18.28 重构后与转向解耦）
        if walking:
            self._blend = min(1.0, getattr(self, "_blend", 0.0) + dt / TURN_SIDE_S)
        else:
            self._blend = max(0.0, getattr(self, "_blend", 0.0) - dt / TURN_BACK_S)
        if abs(target_deg - self._target) > 0.5:
            # 目标变了（起步 0→±90 / 掉头 ±90→∓90 / 停下 ±90→0）：开新段
            self._from = self.yaw
            self._t = 0.0
            self._target = target_deg
        turning = abs(self._target - self.yaw) > 0.5
        if turning:
            dur = TURN_SIDE_S if (walking or self._walking) else TURN_BACK_S
            k = min(1.0, self._t / dur)
            k = k * k * (3 - 2 * k)                   # smoothstep 缓入缓出
            self.yaw = self._from + (self._target - self._from) * k
            self.mode = "TURN_SIDE" if walking else "TURN_BACK"
        else:
            self.yaw = self._target
            self.mode = "SIDE_WALK" if walking else "IDLE_YAW"
        return self.yaw

    @property
    def walk_blend(self) -> float:
        """走姿摆幅系数（0-1）：行走渐升/停止渐降（独立连续值，update 维护）。"""
        return getattr(self, "_blend", 0.0)


# ---------------- 四元数工具（xyzw 存储，w-first 运算） ---------------- #

def _euler_quat(rx: float, ry: float, rz: float) -> tuple[float, float, float, float]:
    """小角欧拉→四元数（xyzw）。用三轴复合实现——展开式曾把恒等算成 (0,0,0,0)
    （w 项在全零角时=0），payload 全零致关节赋值非法、网格近乎不动。"""
    qx = (math.sin(rx / 2), 0.0, 0.0, math.cos(rx / 2))
    qy = (0.0, math.sin(ry / 2), 0.0, math.cos(ry / 2))
    qz = (0.0, 0.0, math.sin(rz / 2), math.cos(rz / 2))
    return _qmul(_qmul(qx, qy), qz)


def _qmul(a: tuple[float, ...], b: tuple[float, ...]) -> tuple[float, ...]:
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return (aw * bx + ax * bw + ay * bz - az * by,
            aw * by - ax * bz + ay * bw + az * bx,
            aw * bz + ax * by - ay * bx + az * bw,
            aw * bw - ax * bx - ay * by - az * bz)


class RigProfile:
    """rig_profile.json 的薄封装；缺字段一律降级为空（动画=只保持 rest）。"""

    def __init__(self, data: dict | None):
        d = data or {}
        self.joints: list[str] = list(d.get("joints") or [])
        self.roles: dict[str, str] = dict(d.get("roles") or {})
        rest = d.get("rest") or {}
        self.rest_q: dict[str, tuple] = {
            j: tuple(rest.get(j, [0, 0, 0, 0, 0, 0, 1])[3:7]) for j in self.joints
        }

    @classmethod
    def from_sidecar(cls, sidecar: dict | None) -> "RigProfile":
        return cls(sidecar)

    def bone(self, role: str) -> str | None:
        return self.roles.get(role)


def build_pose_payload(profile: RigProfile, pose: PoseSemantics,
                       t: float, yaw_state: WalkYawState | None = None,
                       dt: float = 1 / 30) -> dict[str, list[float]]:
    """契约 PoseSemantics + 时钟 t → {骨名: [qx,qy,qz,qw]}（绝对四元数）。

    程序化档位：walk=四肢摆动+起伏+**侧身转体**（yaw_state 给定时）；其余
    （idle 等）=呼吸+轻摆。幅度均为保守小角（度）。
    """
    out: dict[str, list[float]] = {}

    def set_rot(bone: str | None, rx=0.0, ry=0.0, rz=0.0) -> None:
        if not bone or bone not in profile.rest_q:
            return
        # 前乘（父系表达）：q = delta ⊗ rest。后乘 rest ⊗ delta 是骨自身局部系，
        # 逆向骨架手臂 rest≈170° 时局部 X 几乎沿骨轴——绕轴自旋视觉不可见
        # （实测相位差仅 0.13%）。父系对核心链（root 近恒等）= 可预期的世界轴。
        q = _qmul(_euler_quat(rx, ry, rz), profile.rest_q[bone])
        out[bone] = [round(v, 5) for v in q]

    raw_walking = pose.action_id == "walk"
    if yaw_state is not None:
        yaw_state.update(raw_walking, dt, target_deg=pose.view_yaw_deg)
        walking = raw_walking or yaw_state.mode == "TURN_BACK"   # 回正期走姿渐出
        blend = yaw_state.walk_blend
        # 单一目标源（v0.18.28 重构）：yaw=state.yaw——状态机平滑逼近契约
        # view_yaw_deg（±90=朝向真值/idle 0）。历史坑：state 与契约各自
        # 贡献角度会双重计入（180→渲染 135°）；state 又曾强制 0 覆盖契约
        # （"旋转无效"假象）。单一来源后两类 bug 结构性消除
        yaw = yaw_state.yaw
    else:
        walking, blend, yaw = raw_walking, 1.0, pose.view_yaw_deg
    ph = pose.phase * 2.0 * math.pi
    breath = math.sin(t * 2.0 * math.pi / 4.0)          # 呼吸 ~4s 周期
    bob = math.sin(2.0 * ph) * blend if raw_walking else 0.0
    swing_deg = 14.0 * blend if raw_walking else 2.0    # 摆幅随转身进度渐入渐出

    # 躯干：呼吸俯仰 + 行走起伏 + 侧身行走时的前倾（行进感）
    #（hips 的朝向在四肢段统一设置——yaw_render 含 3/4 收窄系数）
    lean = 2.5 * blend if raw_walking else 0.0
    set_rot(profile.bone("spine"), rx=math.radians(1.2 * breath + 1.5 * bob + lean))
    set_rot(profile.bone("chest"), rx=math.radians(0.8 * breath + 1.0 * bob))
    set_rot(profile.bone("neck"), rx=math.radians(-0.5 * breath))
    # 头部：行走时回望观众（侧身走路不甩头——头反向回 35°，桌宠与主人保持
    # 视线接触）；静止轻摆
    if raw_walking:
        set_rot(profile.bone("head"),
                ry=math.radians(-yaw * 0.42 + 1.5 * math.sin(ph)))
    else:
        set_rot(profile.bone("head"),
                ry=math.radians(2.5 * math.sin(t * 2 * math.pi / 7.0)),
                rx=math.radians(1.0 * math.sin(t * 2 * math.pi / 5.0)))
    # 四肢：行走反相摆动 / idle 微摆。侧身行走时渲染角按 3/4 系数收窄
    # （SIDE_FACTOR）：纯 90° 相机看迈步是纯侧棱（深度向），投影不可见
    # =蠕虫感；~68° 既有明显侧身又看得到双腿交替迈步
    SIDE_FACTOR = 0.75
    yaw_render = yaw * SIDE_FACTOR if raw_walking else yaw
    set_rot(profile.bone("hips"), ry=math.radians(yaw_render))
    swing = math.radians(swing_deg)
    ph_l = ph if raw_walking else t * 2 * math.pi / 4.0
    set_rot(profile.bone("arm_upper_l"), rx=+swing * math.sin(ph_l))
    set_rot(profile.bone("arm_upper_r"), rx=-swing * math.sin(ph_l))
    set_rot(profile.bone("arm_lower_l"), rx=+0.4 * swing * math.sin(ph_l))
    set_rot(profile.bone("arm_lower_r"), rx=-0.4 * swing * math.sin(ph_l))
    if walking:
        # 双腿真交替：大腿反相（迈步/蹬地），**膝部屈伸**（摆动腿抬膝弯曲、
        # 支撑腿伸直）——无膝部动作的腿是圆规式挪步（蠕动感的另一半来源）
        set_rot(profile.bone("leg_upper_l"), rx=-swing * math.sin(ph_l))
        set_rot(profile.bone("leg_upper_r"), rx=+swing * math.sin(ph_l))
        knee = math.radians(16.0 * blend)
        set_rot(profile.bone("leg_lower_l"),
                rx=+knee * max(0.0, math.sin(ph_l)))
        set_rot(profile.bone("leg_lower_r"),
                rx=+knee * max(0.0, -math.sin(ph_l)))
        set_rot(profile.bone("foot_l"), rx=0.3 * swing * math.sin(ph_l))
        set_rot(profile.bone("foot_r"), rx=-0.3 * swing * math.sin(ph_l))
    return out


def payload_slice(payload: list[float], index: int) -> tuple[float, ...]:
    """兼容旧扁平 payload 的切片（保留接口；新通道为 dict 形态）。"""
    STRIDE = 7
    base = index * STRIDE
    if base + STRIDE > len(payload):
        return (0.0,) * STRIDE
    return tuple(payload[base:base + STRIDE])
