"""弹簧骨 verlet 链（纯逻辑，零 Qt）——尾巴/呆毛/发梢的次级动画。

算法对齐 VRM springBone 规范（verlet 积分：位置=当前位置+(当前位置-上一步位置)*drag
+重力·dt²，再向静止位姿回弹 stiffness）——规范把算法与参数写明，参考 three-vrm 实现。
碰撞体（extended_collider）v0 未实现，接口预留。运行时参数来自建模期 sidecar
`spring_params.json`（缺省内置一组保守值）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass
class SpringParams:
    stiffness: float = 0.35     # 回弹强度 0-1（越大越硬）
    drag: float = 0.82          # 速度保留系数 0-1（越大越"水"）
    gravity: float = -9.8       # m/s²（y 向上）
    dt_clamp_s: float = 1 / 30  # 单步 dt 上限（防卡顿爆炸）


@dataclass
class VerletChain:
    """一条弹簧链：根节点 pin（跟随骨骼），其余节点自由。坐标=渲染场景单位。"""

    rest: list[tuple[float, float, float]]          # 静止位姿（相对根，尾向）
    params: SpringParams = field(default_factory=SpringParams)
    points: list[tuple[float, float, float]] = field(default_factory=list)
    prev: list[tuple[float, float, float]] = field(default_factory=list)
    root: tuple[float, float, float] = (0.0, 0.0, 0.0)

    def __post_init__(self) -> None:
        if len(self.rest) < 2:
            raise ValueError("VerletChain 至少需要 2 个节点（根 pin + 1 自由）")
        if not self.points:
            self.points = [(self.root[0] + x, self.root[1] + y, self.root[2] + z)
                           for x, y, z in self.rest]
            self.prev = list(self.points)

    def step(self, dt: float, root_now: tuple[float, float, float],
             wind: tuple[float, float, float] = (0.0, 0.0, 0.0)
             ) -> list[tuple[float, float, float]]:
        """推进一帧；返回当前全部节点位置（[0]=根=root_now）。

        wind：自由节点附加加速度（m/s²，链空间）——调用方算好时变风力
        （幅度×正弦脉动×方向），静止姿态的随风摆动即由此驱动。
        """
        dt = min(max(dt, 0.0), self.params.dt_clamp_s)
        self.root = root_now
        self.points[0] = root_now
        self.prev[0] = root_now
        g = self.params.gravity * dt * dt
        wx, wy, wz = wind[0] * dt * dt, wind[1] * dt * dt, wind[2] * dt * dt
        for i in range(1, len(self.points)):
            cx, cy, cz = self.points[i]
            px, py, pz = self.prev[i]
            vx, vy, vz = (cx - px) * self.params.drag, (cy - py) * self.params.drag, (cz - pz) * self.params.drag
            nx, ny, nz = cx + vx + wx, cy + vy + wy + g, cz + vz + wz
            # 向静止位姿回弹（ stiffness 朝 rest 方向收）
            rx, ry, rz = root_now[0] + self.rest[i][0], root_now[1] + self.rest[i][1], root_now[2] + self.rest[i][2]
            nx += (rx - nx) * self.params.stiffness
            ny += (ry - ny) * self.params.stiffness
            nz += (rz - nz) * self.params.stiffness
            # 骨长保持（VRM springBone 规范核心步骤）：节点钉在上一节点为
            # 球心、半径=rest 段长的球面上——无此约束时风把链整体平移而非
            # 弯曲（实测段方向不变=骨不转），力无法逐段传导成鞭状摆动
            ax_, ay_, az_ = nx - self.points[i - 1][0], ny - self.points[i - 1][1], nz - self.points[i - 1][2]
            seg = math.sqrt(ax_ * ax_ + ay_ * ay_ + az_ * az_)
            want = math.sqrt((self.rest[i][0] - self.rest[i - 1][0]) ** 2
                             + (self.rest[i][1] - self.rest[i - 1][1]) ** 2
                             + (self.rest[i][2] - self.rest[i - 1][2]) ** 2)
            if seg > 1e-9 and want > 1e-9:
                s = want / seg
                nx = self.points[i - 1][0] + ax_ * s
                ny = self.points[i - 1][1] + ay_ * s
                nz = self.points[i - 1][2] + az_ * s
            self.prev[i] = self.points[i]
            self.points[i] = (nx, ny, nz)
        return self.points


def chains_from_sidecar(sidecar: dict | None) -> list[VerletChain]:
    """从 sidecar 建链；格式：{"chains": [{"rest": [[x,y,z],...], "params": {...}}, ...]}。

    缺省内置一条演示链（呆毛状竖直小链），保证无 sidecar 也可运行。
    """
    if not sidecar or not isinstance(sidecar.get("chains"), list) or not sidecar["chains"]:
        rest = [(0.0, 0.02 * i, 0.0) for i in range(5)]
        return [VerletChain(rest=rest)]
    out = []
    for c in sidecar["chains"]:
        params = SpringParams(**{k: v for k, v in (c.get("params") or {}).items()
                                 if k in SpringParams.__dataclass_fields__})
        out.append(VerletChain(rest=[tuple(pt) for pt in c["rest"]], params=params))
    return out
