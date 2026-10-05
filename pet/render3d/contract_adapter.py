"""契约适配器——Render3DAdapter 实现 pet.scene_contract.SceneRenderer 协议。

SceneState 四通道 → 各子系统：
  light → lighting.compute(L) → 场景 uniforms
  expression → morph.ExpressionResolver → morph 权重（M1 存档，S1.4 蒙皮后驱动 MeshTarget）
  pose → M1 仅记录（骨骼驱动待 S1.4；bone_bridge 通道已备）
  spring 链：随 apply 步进（根=模型锚点，v0 静态锚点+呼吸微动）

铁律：apply 不抛（任何异常 → 内部降级 is_ready=False，单行日志不刷屏）。
"""

from __future__ import annotations

import logging
import time

from pet.render3d import bone_bridge, lighting, morph, spring
from pet.scene_contract import LightWeatherState, PoseSemantics, SceneState

logger = logging.getLogger("pet.render3d")


class Render3DAdapter:
    def __init__(self, window, level: int = 1, sidecars: dict | None = None):
        self._window = window
        self._level = level
        self._resolver = morph.ExpressionResolver((sidecars or {}).get("expression_map"))
        self._chains = spring.chains_from_sidecar((sidecars or {}).get("spring_params"))
        self._profile = bone_bridge.RigProfile.from_sidecar((sidecars or {}).get("rig_profile"))
        self._last_t = time.monotonic()
        self._degraded = False
        self._logged = False

    # ---- SceneRenderer 协议 ----

    def apply(self, state: SceneState) -> None:
        if self._degraded:
            return
        try:
            u = lighting.compute(state.light, self._level)
            self._window.apply_uniforms(u.as_qml())
            # 表情权重（M1 存档——蒙皮模型就绪后驱动 morph target）
            self._morph_weights = self._resolver.resolve(state.expression)
            # 姿势通道：语义角色程序化（任意骨架——profile 有则动、无则静）
            now = time.monotonic()
            payload = bone_bridge.build_pose_payload(self._profile, state.pose, now)
            if payload:
                self._window.apply_pose(payload)
            # 弹簧链步进（根=脚底锚点上方 1.4m≈头顶位置，v0 演示）
            dt = min(now - self._last_t, 0.1)
            self._last_t = now
            for ch in self._chains:
                ch.step(dt, (0.0, 1.35, 0.0))
        except Exception:
            self._degraded = True
            if not self._logged:
                logger.warning("render3d apply 失败，降级 2D", exc_info=True)
                self._logged = True

    def is_ready(self) -> bool:
        return not self._degraded and not getattr(self._window, "degraded", False)

    # ---- 调试/验收辅助 ----

    @property
    def morph_weights(self) -> dict[str, float]:
        return getattr(self, "_morph_weights", {})

    @property
    def spring_points(self) -> list:
        return [list(ch.points) for ch in self._chains]
