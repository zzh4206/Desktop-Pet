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

# 侧身角：+90°=模型左肩朝向观众（展示行进的侧面轮廓）
SIDE_YAW_DEG = 90.0
TURN_SIDE_S = 0.55     # 起步转身时长
TURN_BACK_S = 0.70     # 停下回正时长（略慢，"停下喘口气"的节奏感）


class WalkYawState:
    """走路侧身状态机（纯逻辑，可单测）：yaw 随 walk/idle 平滑过渡。

    TURN_SIDE：起身 0→90°（转身完成前摆幅按进度渐入——"边转边迈步"）
    SIDE_WALK：侧身行走中（yaw 保持 90°）
    TURN_BACK：停止 90°→0°（走姿摆幅按 1-进度渐出）
    IDLE_YAW：正面静止（yaw=0）
    """

    def __init__(self) -> None:
        self.mode = "IDLE_YAW"
        self.yaw = 0.0            # 当前角（度）
        self._t = 0.0             # 模式内计时
        self._from = 0.0          # 转身起点角

    def update(self, walking: bool, dt: float) -> float:
        """喂当拍 walk 状态与时长，返回当前 yaw（度）。"""
        self._t += max(0.0, dt)
        if walking:
            if self.mode in ("IDLE_YAW", "TURN_BACK"):
                # 起步：从当前角转到侧身
                self._from = self.yaw
                self._t = 0.0
                self.mode = "TURN_SIDE"
            if self.mode == "TURN_SIDE":
                k = min(1.0, self._t / TURN_SIDE_S)
                k = k * k * (3 - 2 * k)               # smoothstep 缓入缓出
                self.yaw = self._from + (SIDE_YAW_DEG - self._from) * k
                if k >= 1.0:
                    self.mode = "SIDE_WALK"
        else:
            if self.mode in ("SIDE_WALK", "TURN_SIDE"):
                self._from = self.yaw
                self._t = 0.0
                self.mode = "TURN_BACK"
            if self.mode == "TURN_BACK":
                k = min(1.0, self._t / TURN_BACK_S)
                k = k * k * (3 - 2 * k)
                self.yaw = self._from + (0.0 - self._from) * k
                if k >= 1.0:
                    self.mode = "IDLE_YAW"
        return self.yaw

    @property
    def walk_blend(self) -> float:
        """走姿摆幅系数（0-1）：转身期渐入渐出，保证"连贯无跳变"。"""
        if self.mode == "SIDE_WALK":
            return 1.0
        if self.mode == "TURN_SIDE":
            return min(1.0, self._t / TURN_SIDE_S)
        if self.mode == "TURN_BACK":
            return max(0.0, 1.0 - self._t / TURN_BACK_S)
        return 0.0


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
        yaw_state.update(raw_walking, dt)
        walking = raw_walking or yaw_state.mode in ("TURN_BACK",)  # 回正期走姿渐出
        blend = yaw_state.walk_blend
        # 侧身角叠加在契约视角上——纯 idle（IDLE_YAW）时 state.yaw=0，
        # 回退契约 view_yaw_deg（曾有 bug：state 强制 0 覆盖契约视角，
        # 表现为"任何旋转都无效"——hips 每帧被写回 rest）
        yaw = pose.view_yaw_deg + yaw_state.yaw
    else:
        walking, blend, yaw = raw_walking, 1.0, pose.view_yaw_deg
    ph = pose.phase * 2.0 * math.pi
    breath = math.sin(t * 2.0 * math.pi / 4.0)          # 呼吸 ~4s 周期
    bob = math.sin(2.0 * ph) * blend if raw_walking else 0.0
    swing_deg = 14.0 * blend if raw_walking else 2.0    # 摆幅随转身进度渐入渐出

    # 根：整体朝向——走路侧身（yaw_state 驱动）或契约视角（外部给定）
    set_rot(profile.bone("hips"), ry=math.radians(yaw))
    # 躯干：呼吸俯仰 + 行走起伏 + 侧身行走时的前倾（行进感）
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
    # 四肢：行走反相摆动 / idle 微摆
    swing = math.radians(swing_deg)
    ph_l = ph if raw_walking else t * 2 * math.pi / 4.0
    set_rot(profile.bone("arm_upper_l"), rx=+swing * math.sin(ph_l))
    set_rot(profile.bone("arm_upper_r"), rx=-swing * math.sin(ph_l))
    set_rot(profile.bone("arm_lower_l"), rx=+0.4 * swing * math.sin(ph_l))
    set_rot(profile.bone("arm_lower_r"), rx=-0.4 * swing * math.sin(ph_l))
    if walking:
        set_rot(profile.bone("leg_upper_l"), rx=-swing * math.sin(ph_l))
        set_rot(profile.bone("leg_upper_r"), rx=+swing * math.sin(ph_l))
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
