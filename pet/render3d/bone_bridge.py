"""骨骼桥 v1（纯逻辑，零 Qt）——语义角色驱动的程序化姿势，任意骨架可插。

解耦设计（S2.4）：
* 输入 = rig_profile sidecar（make_runtime_asset.py 从任意 skinned GLB 生成）：
  ``{"joints": [...], "roles": {语义角色→骨名}, "rest": {骨名→[px,py,pz,qx,qy,qz,qw]}}``
* 程序化动画只引用**语义角色**（hips/spine/chest/neck/head/arm_*/leg_*/foot_*），
  profile 里缺的角色直接跳过（"有则动、无则静"）——骨名零硬编码。
* 输出 = {骨名: [qx,qy,qz,qw]} **绝对**四元数（rest ⊗ delta_local），QML 侧只赋值。

delta 在关节局部系表达（假设局部 Y≈骨向，逆向骨架/常见人形骨架成立）；
 baked clip（S1.8 Mixamo）就绪后本模块退化为 clip 通道的角色参数化补充。
"""

from __future__ import annotations

import math

from pet.scene_contract import PoseSemantics


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
                       t: float) -> dict[str, list[float]]:
    """契约 PoseSemantics + 时钟 t → {骨名: [qx,qy,qz,qw]}（绝对四元数）。

    程序化档位：walk=四肢摆动+起伏；其余（idle 等）=呼吸+轻摆。
    幅度均为保守小角（度），骨架局部系近似下任意人形骨架可用。
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

    walking = pose.action_id == "walk"
    ph = pose.phase * 2.0 * math.pi
    breath = math.sin(t * 2.0 * math.pi / 4.0)          # 呼吸 ~4s 周期
    bob = math.sin(2.0 * ph) if walking else 0.0

    # 根：整体朝向（局部系近似=世界 yaw，root 局部通常近 identity）
    set_rot(profile.bone("hips"), ry=math.radians(pose.view_yaw_deg))
    # 躯干：呼吸俯仰 + 行走起伏
    set_rot(profile.bone("spine"), rx=math.radians(1.2 * breath + 1.5 * bob))
    set_rot(profile.bone("chest"), rx=math.radians(0.8 * breath + 1.0 * bob))
    set_rot(profile.bone("neck"), rx=math.radians(-0.5 * breath))
    # 头部：idle 轻摆 / 行走稳头
    if walking:
        set_rot(profile.bone("head"), ry=math.radians(1.5 * math.sin(ph)))
    else:
        set_rot(profile.bone("head"),
                ry=math.radians(2.5 * math.sin(t * 2 * math.pi / 7.0)),
                rx=math.radians(1.0 * math.sin(t * 2 * math.pi / 5.0)))
    # 四肢：行走反相摆动 / idle 微摆
    swing = math.radians(14.0) if walking else math.radians(2.0)
    ph_l = ph if walking else t * 2 * math.pi / 4.0
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
