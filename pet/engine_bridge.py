"""中间层（接口接入）—— 原有引擎 ← 本模块 ← 新引擎的有效部分。

命名约定（v0.15.1 起）：
* **原有引擎** = frames 帧动画后端（成熟、防御性编程成熟，永不失败）。
* **新引擎**   = 正在开发的 motion / wind / sun / rig 那套。
* **有效部分** = 新引擎里已经过单测回归锁的部分（motion 32 / wind 18 / sun 27），
  未达标部分（paperdoll 部件步态、2 帧走路）**不**纳入本接口。

三层结构
--------
::

    原有引擎(frames) + app        （消费 ``Enrichment``，恒等=零侵入）
        ▲  中间层接口：set_motion(...) / tick(dt) → Enrichment
    中间层 EngineBridge           （防御性门面，任一环失败即降级）
        ▲  try/except 包裹的调用
    新引擎有效部分                 （motion.MotionEngine / wind / sun）

铁律
----
* 原有引擎永远是兜底；新引擎有效部分只做「可选叠加」。
* 新引擎任一部分抛错 → 该部分**永久**降级为恒等/静态，绝不阻断启动或运行，
  且只记一次日志（不刷屏）。
* ``Enrichment`` 全字段有安全默认值：消费者（frames 或 QML）缺省即零侵入。
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from .rig.motion import MotionEngine, MotionFrame, MotionInputs
from .rig.spec import RigSpec

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# 接口输出契约
# --------------------------------------------------------------------------- #
@dataclass
class Enrichment:
    """新引擎有效部分贡献给渲染的整身增量（全默认 = 恒等，零侵入）。"""

    body_angle: float = 0.0      # 整体旋转：倾斜 + 超低频漂移（度）
    body_y: float = 0.0          # 纵向浮动：呼吸（px）
    scale_x: float = 1.0         # 体积守恒（=1/scale_y）
    scale_y: float = 1.0         # squash 压缩（<1 压扁 / >1 过冲拉伸）
    blink_on: bool = False       # 眨眼脉冲（闭眼覆盖件显隐）
    part_angles: dict = field(default_factory=dict)   # part_id→角（仅 QML 可用）
    # 光影通道（慢变量，随太阳走，不随身体摇晃）
    shadow_alpha: float = 0.0
    shadow_offset_x: float = 0.0
    shadow_scale_x: float = 1.0
    shadow_scale_y: float = 0.08


# --------------------------------------------------------------------------- #
# 接口契约（ABC）
# --------------------------------------------------------------------------- #
class Enricher(ABC):
    """新引擎有效部分的接口契约：喂 FSM 实况 → 逐帧产出增强量。"""

    @abstractmethod
    def set_motion(self, tilt_deg: float = 0.0, walking: bool = False,
                   walk_hz: float = 0.0, airborne: bool = False,
                   wind_gain: float = 1.0,
                   wind_bias_deg: float = 0.0) -> None:
        """喂入一拍的 FSM 实况（含风调制）；落地沿在此触发 squash。"""

    @abstractmethod
    def tick(self, dt_ms: float) -> Enrichment:
        """推进一帧，返回增强量。"""


# --------------------------------------------------------------------------- #
# 原有引擎的缺省实现：恒等（零风险）
# --------------------------------------------------------------------------- #
class NullEnricher(Enricher):
    """原有引擎缺省实现——恒等增强。任何环境、任何输入都不失败。"""

    def set_motion(self, tilt_deg: float = 0.0, walking: bool = False,
                   walk_hz: float = 0.0, airborne: bool = False,
                   wind_gain: float = 1.0,
                   wind_bias_deg: float = 0.0) -> None:
        return None

    def tick(self, dt_ms: float) -> Enrichment:
        return Enrichment()


# --------------------------------------------------------------------------- #
# 新引擎有效部分①：运动引擎（呼吸/眨眼/倾斜/squash/部件角）
# --------------------------------------------------------------------------- #
class MotionEnricher(Enricher):
    """把 ``motion.MotionEngine`` 包装成接口实现。"""

    def __init__(self, spec: RigSpec | None):
        self._engine = MotionEngine(spec)
        self._inputs = MotionInputs()
        self._air_prev = False

    def set_motion(self, tilt_deg: float = 0.0, walking: bool = False,
                   walk_hz: float = 0.0, airborne: bool = False,
                   wind_gain: float = 1.0,
                   wind_bias_deg: float = 0.0) -> None:
        self._inputs.tilt_deg = float(tilt_deg)
        self._inputs.walking = bool(walking)
        self._inputs.walk_hz = float(walk_hz)
        self._inputs.wind_gain = float(wind_gain)
        self._inputs.wind_bias_deg = float(wind_bias_deg)
        if (not airborne) and self._air_prev:
            self._engine.trigger_squash()      # 落地下降沿 → squash 冲量
        self._air_prev = bool(airborne)

    def tick(self, dt_ms: float) -> Enrichment:
        f: MotionFrame = self._engine.step(self._inputs, dt_ms)
        return Enrichment(
            body_angle=f.body_angle,
            body_y=f.body_y,
            scale_x=f.body_scale_x,
            scale_y=f.body_scale_y,
            blink_on=f.blink_on,
            part_angles=dict(f.part_angles),
        )


# --------------------------------------------------------------------------- #
# 新引擎有效部分②：风 + 光影通道（喂 motion 的风调制 + 出地面阴影）
# --------------------------------------------------------------------------- #
class ChannelEnricher:
    """把 ``wind`` / ``sun`` 两通道包装成接口实现（各自独立兜底，互不拖累）。"""

    def __init__(self, cfg: dict):
        from .wind import StaticWindSource, build_wind_source
        from .sun import StaticSunSource, build_sun_source

        try:
            self._wind = build_wind_source(cfg)
        except Exception:  # noqa: BLE001
            logger.warning("风通道装配失败，回退静态微风")
            self._wind = StaticWindSource()
        try:
            self._sun = build_sun_source(cfg)
        except Exception:  # noqa: BLE001
            logger.warning("光影通道装配失败，回退静态阴影")
            self._sun = StaticSunSource()

    def refresh(self) -> None:
        """按需刷新两条通道；各自 try/except，一条失败不拖累另一条。"""
        for name, src in (("wind", self._wind), ("sun", self._sun)):
            try:
                src.refresh()
            except Exception:  # noqa: BLE001
                logger.warning("通道刷新失败（走兜底）: %s", name)

    def wind(self) -> tuple[float, float]:
        """→ (gain, bias_deg)，喂 motion 的风调制。"""
        try:
            return self._wind.current()
        except Exception:  # noqa: BLE001
            return (1.0, 0.0)

    def shadow(self) -> Enrichment:
        """→ 阴影增量（alpha/offset/scale）。"""
        try:
            sh = self._sun.current()
            return Enrichment(
                shadow_alpha=float(sh.alpha),
                shadow_offset_x=float(sh.offset_x),
                shadow_scale_x=float(sh.scale_x),
                shadow_scale_y=float(sh.scale_y),
            )
        except Exception:  # noqa: BLE001
            return Enrichment()


# --------------------------------------------------------------------------- #
# 中间层门面：防御性组合
# --------------------------------------------------------------------------- #
class EngineBridge:
    """原有引擎打底 + 新引擎有效部分按接口叠加；任一环失败即永久降级。

    ``set_motion`` / ``refresh_channels`` / ``tick`` 三个入口**均不抛异常**：
    新引擎任何一步出错都会把对应部分替换成 ``NullEnricher`` / 静态兜底，
    记一次日志后继续以原有引擎行为运行。
    """

    def __init__(self, enricher: Enricher | None = None,
                 channels: ChannelEnricher | None = None):
        self._enricher: Enricher = enricher if enricher is not None \
            else NullEnricher()
        self._channels = channels
        self._degraded = False

    # ---- 喂入 FSM 实况（含风调制）----
    def set_motion(self, tilt_deg: float = 0.0, walking: bool = False,
                   walk_hz: float = 0.0, airborne: bool = False,
                   wind_gain: float = 1.0,
                   wind_bias_deg: float = 0.0) -> None:
        try:
            self._enricher.set_motion(
                tilt_deg=tilt_deg, walking=walking, walk_hz=walk_hz,
                airborne=airborne, wind_gain=wind_gain,
                wind_bias_deg=wind_bias_deg)
        except Exception:  # noqa: BLE001
            self._degrade("set_motion")

    # ---- 刷新风/光影通道 ----
    def refresh_channels(self) -> None:
        if self._channels is None:
            return
        try:
            self._channels.refresh()
        except Exception:  # noqa: BLE001
            self._degrade("refresh_channels")

    # ---- 风调制取值（喂 set_motion 用；失败兜底静态微风）----
    def wind(self) -> tuple[float, float]:
        """→ (gain, bias_deg)。无通道或刷新失败时回退 (1.0, 0.0)，不抛错。"""
        if self._channels is None:
            return (1.0, 0.0)
        try:
            return self._channels.wind()
        except Exception:  # noqa: BLE001
            return (1.0, 0.0)

    # ---- 推进一帧，返回增强量（永不抛错）----
    def tick(self, dt_ms: float) -> Enrichment:
        try:
            e = self._enricher.tick(dt_ms)
        except Exception:  # noqa: BLE001
            self._degrade("tick")
            return Enrichment()

        # 叠加光影（风调制已在 set_motion 喂给 motion）
        if self._channels is not None:
            sh = self._channels.shadow()
            e.shadow_alpha = sh.shadow_alpha
            e.shadow_offset_x = sh.shadow_offset_x
            e.shadow_scale_x = sh.shadow_scale_x
            e.shadow_scale_y = sh.shadow_scale_y
        return e

    # ---- 诊断 ----
    @property
    def active(self) -> str:
        """当前生效层：'original'（恒等）| 'motion'（新引擎有效部分）。"""
        return "original" if isinstance(self._enricher, NullEnricher) \
            else type(self._enricher).__name__

    @property
    def degraded(self) -> bool:
        return self._degraded

    def _degrade(self, where: str) -> None:
        if not self._degraded:
            logger.warning("新引擎有效部分在 %s 抛错，降级回原有引擎（恒等）", where)
            self._degraded = True
        self._enricher = NullEnricher()
