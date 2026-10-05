"""3D 呈现桥（S2.9 呈现层半边）——flag 选择 + 降级矩阵，不做翻译。

设计（设计-子模块与接口.md 总原则）：3D 线是呈现选择器后面的一个独立渲染
实现；本桥只决定「用不用 3D」与「何时永久退回 2D」，语义→渲染的翻译在
Render3DAdapter 内。与 2D 线的 pet/engine_bridge.py（Enrichment 增强桥）
同名不同物，互不相扰。

降级矩阵（D04）：3D → 2D rig → frames。本桥只负责第一跳：
  * flag 关 / 资产缺 / 初始化异常 → start()=False（调用方直接走 2D，零成本）；
  * 运行中 apply 异常或看门狗 is_ready=False → 永久降级（mode="2d"），
    on_degrade 回调通知调用方切 2D（只触发一次，不刷屏）。

app.py 接入点（一行，用户区由用户落）::

    bridge = Render3DBridge(cfg, on_degrade=lambda: app.switch_to_2d())
    if bridge.start():
        app.on_scene_state = bridge.apply          # FSM tick 里喂 SceneState
    # else: 2D rig 原样启动（零侵入）

铁律：start/apply/shutdown 均不抛异常给调用方；降级后 apply 变 no-op。
"""

from __future__ import annotations

import logging
from typing import Callable

from pet.scene_contract import SceneState

logger = logging.getLogger("pet.render3d")


class Render3DBridge:
    """3D 呈现的生命周期与降级门面（Qt 依赖经 bootstrap 惰性进入）。"""

    def __init__(self, cfg: dict | None = None,
                 on_degrade: Callable[[], None] | None = None,
                 on_click: Callable[[], None] | None = None,
                 on_drag_start: Callable[[], None] | None = None):
        self._cfg = cfg
        self._on_degrade = on_degrade
        self._on_click = on_click
        self._on_drag = on_drag_start
        self._renderer = None
        self._mode = "2d"          # 3D 成功启动后才变 "3d"
        self._degraded = False     # 3D→2d 的降级只发生一次（含回调）

    # ---- 生命周期 ----

    def start(self) -> bool:
        """尝试装配 3D；False=继续 2D（调用方无需任何补偿动作）。"""
        if self._renderer is not None:
            return self._mode == "3d"
        try:
            from pet.render3d.bootstrap import create_renderer
            self._renderer = create_renderer(self._cfg)
        except Exception:  # noqa: BLE001
            logger.warning("render3d 装配异常，保持 2D", exc_info=True)
            self._renderer = None
        if self._renderer is None:
            return False
        self._mode = "3d"
        # 交互透传（QML 信号 → 调用方；窗口层异常不影响呈现）
        try:
            win = getattr(self._renderer, "_window", None)
            if win is not None:
                win.set_interaction(on_click=self._on_click,
                                    on_drag_start=self._on_drag)
        except Exception:  # noqa: BLE001
            logger.warning("render3d 交互挂接失败（忽略，不影响呈现）")
        logger.info("render3d 呈现已启动")
        return True

    # ---- SceneRenderer 协议转发（永不抛） ----

    def apply(self, state: SceneState) -> None:
        if self._mode != "3d" or self._renderer is None:
            return
        try:
            self._renderer.apply(state)
            if not self._renderer.is_ready():
                self._degrade("watchdog(is_ready=False)")
        except Exception:  # noqa: BLE001
            self._degrade("apply 异常")

    def is_ready(self) -> bool:
        return self._mode == "3d" and self._renderer is not None \
            and self._renderer.is_ready()

    # ---- 诊断/收尾 ----

    @property
    def mode(self) -> str:
        """当前呈现层：'3d' | '2d'（初始即 2d，降级后回 2d）。"""
        return self._mode

    def shutdown(self) -> None:
        r, self._renderer = self._renderer, None
        if r is not None:
            try:
                getattr(r, "_window", None) and r._window.close()
            except Exception:  # noqa: BLE001
                pass
        self._mode = "2d"

    # ---- 内部 ----

    def _degrade(self, why: str) -> None:
        self._mode = "2d"
        try:
            self.shutdown()
        except Exception:  # noqa: BLE001
            pass
        if not self._degraded:
            self._degraded = True
            logger.warning("render3d 降级回 2D（%s）", why)
            if self._on_degrade is not None:
                try:
                    self._on_degrade()
                except Exception:  # noqa: BLE001
                    logger.warning("on_degrade 回调异常（忽略）", exc_info=True)
