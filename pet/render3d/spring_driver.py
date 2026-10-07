"""弹簧骨运行时驱动（纯逻辑，零 Qt）——静止姿态的发丝/衣服/尾巴随风摆。

数据流：sidecar spring_params.json（rig_spring_export.py 从 rig_scene 导出）
→ SpringRig（每链 VerletChain + 骨名映射 + 根锚点）→ step()（风=时变加速度
+重力+回弹）→ 链节点方向变化 → 骨旋转四元数（fromToQuat(rest_dir, cur_dir)
前乘 rest_q，与 bone_bridge 同款父系表达）→ 合并进 posePayload。

风的表达：wind_speed（契约 LightWeatherState，m/s）→ gain=min(1, v/8)；
方向脉动 = 每链独立相位的正弦（阵风感）× 水平主向 + 微垂直分量。
碰撞体未实现；锚点=链根骨静止位置（走路跟随属 S1.8 步骤，当前服务静止姿态）。
"""

from __future__ import annotations

import math

from pet.render3d.spring import VerletChain, chains_from_sidecar


def _norm(v: tuple[float, float, float]) -> tuple[float, float, float]:
    n = math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2])
    if n < 1e-9:
        return (0.0, 0.0, 0.0)
    return (v[0] / n, v[1] / n, v[2] / n)


def from_to_quat(a: tuple[float, float, float],
                 b: tuple[float, float, float]) -> tuple[float, float, float, float]:
    """单位向量 a→b 的旋转四元数（xyzw）。a≈b 恒等；a≈-b 取任意垂直轴 π。"""
    a, b = _norm(a), _norm(b)
    d = a[0] * b[0] + a[1] * b[1] + a[2] * b[2]
    if d > 0.9999999:
        return (0.0, 0.0, 0.0, 1.0)
    if d < -0.99999:
        # 任意与 a 垂直的轴
        ax = _norm((1.0, 0.0, 0.0) if abs(a[0]) < 0.9 else (0.0, 1.0, 0.0))
        axis = _norm((a[1] * ax[2] - a[2] * ax[1],
                      a[2] * ax[0] - a[0] * ax[2],
                      a[0] * ax[1] - a[1] * ax[0]))
        return (axis[0], axis[1], axis[2], 0.0)   # 绕轴 π：w=0
    axis = (a[1] * b[2] - a[2] * b[1],
            a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0])
    s = math.sqrt(max(0.0, (1.0 + d) * 0.5))
    axis_n = math.sqrt(max(1e-18, 1.0 - s * s))
    n = math.sqrt(axis[0] ** 2 + axis[1] ** 2 + axis[2] ** 2) or 1.0
    return (axis[0] / n * axis_n, axis[1] / n * axis_n,
            axis[2] / n * axis_n, s)


def qmul(a: tuple, b: tuple) -> tuple:
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return (aw * bx + ax * bw + ay * bz - az * by,
            aw * by - ax * bz + ay * bw + az * bx,
            aw * bz + ax * by - ay * bx + az * bw,
            aw * bw - ax * bx - ay * by - az * bz)


class SpringRig:
    """全部弹簧链的集合：step 一拍 → {骨名: [qx,qy,qz,qw]}（绝对四元数）。"""

    def __init__(self, sidecar: dict | None, rest_q: dict[str, tuple] | None = None):
        self.chains: list[VerletChain] = []
        self.bone_maps: list[list[str]] = []
        self.roots: list[tuple[float, float, float]] = []
        self.rest_q: dict[str, tuple] = rest_q or {}
        self._phase = 0.0
        chains = (sidecar or {}).get("chains") or []
        for c in chains:
            bones = c.get("bones") or []
            if len(bones) < 2:
                continue
            from pet.render3d.spring import SpringParams
            params = SpringParams(**{k: v for k, v in (c.get("params") or {}).items()
                                     if k in SpringParams.__dataclass_fields__})
            chain = VerletChain(rest=[tuple(p) for p in c["rest"]], params=params)
            self.chains.append(chain)
            self.bone_maps.append(bones)
            rw = c.get("root_world") or [0.0, 0.0, 0.0]
            self.roots.append(tuple(rw))

    def __len__(self) -> int:
        return len(self.chains)

    def step(self, dt: float, wind_speed: float = 0.0) -> dict[str, list[float]]:
        """推进全部链；返回 {骨名: [qx,qy,qz,qw]}（无链/无风→空或全静止）。"""
        self._phase += dt
        gain = min(1.0, max(0.0, wind_speed) / 8.0)
        out: dict[str, list[float]] = {}
        for ci, chain in enumerate(self.chains):
            bones = self.bone_maps[ci]
            # 阵风（视觉增益已调）：每链独立相位错开，主向 +z 水平推，
            # 侧向/升力小分量做拂动感。幅度经实测校准——段 2+ 偏角需
            # 显著超出 from_to 的恒等判定圈且肉眼可见（≥3°）
            gust = gain * (0.75 + 0.35 * math.sin(self._phase * 2.1 + ci * 1.7))
            wind = (0.5 * gain * math.sin(self._phase * 1.3 + ci),
                    0.9 * gain * math.sin(self._phase * 3.7 + ci * 2.3),
                    22.0 * gust)
            chain.step(dt, self.roots[ci], wind)
            pts = chain.points
            for i in range(1, len(bones)):
                rest_dir = (chain.rest[i][0] - chain.rest[i - 1][0],
                            chain.rest[i][1] - chain.rest[i - 1][1],
                            chain.rest[i][2] - chain.rest[i - 1][2])
                cur_dir = (pts[i][0] - pts[i - 1][0],
                           pts[i][1] - pts[i - 1][1],
                           pts[i][2] - pts[i - 1][2])
                q = from_to_quat(rest_dir, cur_dir)
                rq = self.rest_q.get(bones[i], (0.0, 0.0, 0.0, 1.0))
                final = qmul(q, rq)
                out[bones[i]] = [round(v, 5) for v in final]
        return out


def legacy_chains(sidecar: dict | None) -> list[VerletChain]:
    """兼容旧调用（chains_from_sidecar 直通）。"""
    return chains_from_sidecar(sidecar)
