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

from pet.render3d import bone_bridge, lighting, morph
from pet.render3d.spring_driver import SpringRig
from pet.scene_contract import LightWeatherState, PoseSemantics, SceneState

logger = logging.getLogger("pet.render3d")


class Render3DAdapter:
    def __init__(self, window, level: int = 1, sidecars: dict | None = None):
        self._window = window
        self._level = level
        self._resolver = morph.ExpressionResolver((sidecars or {}).get("expression_map"))
        # 弹簧骨（S1.6）：链定义 sidecar + rig_profile 的 rest_q（骨绝对
        # 四元数表达与 bone_bridge 一致）；无 sidecar 时空转（零影响）
        profile_sc = (sidecars or {}).get("rig_profile") or {}
        rest_q = {j: tuple(v) for j, v in (profile_sc.get("rest") or {}).items()
                  if isinstance(v, (list, tuple)) and len(v) >= 7}
        rest_q = {j: tuple(v[3:7]) for j, v in rest_q.items()}
        self._springs = SpringRig((sidecars or {}).get("spring_params"), rest_q)
        self._profile = bone_bridge.RigProfile.from_sidecar(profile_sc)
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
            now = time.monotonic()
            dt = min(now - self._last_t, 0.1)
            self._last_t = now
            # 姿势通道：语义角色程序化 + 弹簧骨（发丝/衣服/尾巴随风摆——
            # 风从契约 light.wind_speed 取，静止姿态的活物感来源）
            payload = bone_bridge.build_pose_payload(self._profile, state.pose, now)
            if len(self._springs):
                try:
                    payload.update(self._springs.step(dt, state.light.wind_speed))
                except Exception:  # noqa: BLE001
                    logger.warning("弹簧骨步进异常（本拍跳过）", exc_info=True)
            if payload:
                self._window.apply_pose(payload)
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
        """调试/验收：各弹簧链当前节点（空=无 sidecar）。"""
        return [list(ch.points) for ch in self._springs.chains]
