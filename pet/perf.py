"""帧率分档治理（v0.19.8）——不同机器不同节奏，成本可控。

用户实测反馈（2026-10-07）：不太流畅、帧数太低。桌面宠物的"帧数"由两个
独立节拍器构成，分档只动它们的间隔，不动任何素材内容时长（帧动画
interval 是内容播放速度，不参与分档）：

- FSM tick（app._tick_timer）：行为步进 + 窗口位移节奏（行走平滑度的
  主要来源；物理吃实测 dt，变速不变速感）；
- rig motion tick（presenter._motion_timer）：骨骼/渲染节奏——其内部
  已有活跃度三档（loco/fast/slow），本模块换的是三档常数本身。

档位：
- low    弱机/省电：全程慢拍，位移 15fps；
- medium 0.19.7 的固定节奏原样保留（回退基线，行为零变化）；
- high   现代 CPU（auto 在 Apple Silicon 命中）：位移 30fps、rig 活跃拍
  20ms（QTimer mac 实测粒度 ~17.7ms，实播 ~35-45fps）。

auto 判档（平台启发式，启动一次）+ 过载降档守卫（auto 档专用：FSM tick
持续超时说明机器撑不住当前档，逐级降到 low 为止）。用户可在右键菜单
「流畅度」改档，选择持久化到 config.performance.frame_tier；手动档不做
自动降档（用户拍板优先）。
"""

from __future__ import annotations

import logging
import os
import platform
import sys
from dataclasses import dataclass

log = logging.getLogger("pet")

TIER_ORDER = ("high", "medium", "low")

TIER_NAME_ZH = {"high": "高", "medium": "中", "low": "低"}


@dataclass(frozen=True)
class PerfTier:
    """单档节拍表（毫秒）。medium = 0.19.7 固定节奏，保持逐值一致。"""

    fsm_tick_ms: int    # app._tick_timer：行为步进 + 窗口位移
    rig_loco_ms: int    # rig 侧身编排拍
    rig_fast_ms: int    # rig 活跃拍（行走/空中/瞬态）
    rig_slow_ms: int    # rig 静止拍（呼吸/慢摆）


TIERS: dict[str, PerfTier] = {
    "low": PerfTier(fsm_tick_ms=66, rig_loco_ms=33, rig_fast_ms=50,
                    rig_slow_ms=100),
    "medium": PerfTier(fsm_tick_ms=50, rig_loco_ms=16, rig_fast_ms=33,
                       rig_slow_ms=66),
    "high": PerfTier(fsm_tick_ms=33, rig_loco_ms=16, rig_fast_ms=20,
                     rig_slow_ms=50),
}


def detect_tier() -> tuple[str, str]:
    """auto 判档：返回 (tier, 依据说明)。平台启发式，启动一次，不引 Qt。"""
    if sys.platform == "darwin":
        machine = platform.machine()
        if machine == "arm64":
            return "high", "Apple Silicon"
        return "medium", "Intel mac"
    try:
        cores = os.cpu_count() or 4
    except Exception:  # noqa: BLE001 — cpu_count 异常按弱机保守处理
        cores = 4
    if cores >= 8:
        return "medium", f"{cores} 核"
    return "low", f"{cores} 核"


# ---- 过载降档守卫参数 ----
_OVERRUN_WINDOW = 90        # 滑动窗口采样数（50ms 档 ≈ 4.5s）
_OVERRUN_RATIO = 0.6        # 窗口内超时占比超过该值判过载
_OVERRUN_FACTOR = 1.5       # 实测间隔 > 目标 × factor 记一次超时


class FramePacer:
    """帧率档位裁决器：auto/手动选择 → 三处节拍间隔 + auto 过载降档。"""

    def __init__(self, choice: str = "auto"):
        self._ring: list[bool] = []
        self._choice = "auto"
        self._tier, self._reason = detect_tier()
        self.set_choice(choice)

    # ---- 查询 ----
    @property
    def choice(self) -> str:
        return self._choice

    @property
    def tier(self) -> str:
        return self._tier

    @property
    def reason(self) -> str:
        return self._reason

    @property
    def preset(self) -> PerfTier:
        return TIERS[self._tier]

    @property
    def fsm_ms(self) -> int:
        return self.preset.fsm_tick_ms

    def rig_intervals(self) -> tuple[int, int, int]:
        """(loco, fast, slow) 毫秒三参，presenter.apply_frame_tier 消费。"""
        p = self.preset
        return p.rig_loco_ms, p.rig_fast_ms, p.rig_slow_ms

    # ---- 变更 ----
    def set_choice(self, choice: str) -> str:
        """切换选择（auto/high/medium/low），返回生效档位名。

        非法值静默回退 auto；auto 即重跑判档（机器环境可能已变）。"""
        if choice not in ("auto", *TIER_ORDER):
            log.warning("frame_tier 非法 %r，回退 auto", choice)
            choice = "auto"
        self._choice = choice
        if choice == "auto":
            self._tier, self._reason = detect_tier()
        else:
            self._tier, self._reason = choice, "手动"
        self._ring.clear()
        log.info("帧率档位：%s（%s：%s）", self._tier, self._choice,
                 self._reason)
        return self._tier

    # ---- 过载守卫（auto 档专用；app._tick 每拍喂原始 dt） ----
    def record_fsm_tick(self, dt_s: float | None) -> None:
        """喂入 FSM tick 实测间隔（秒；None=首拍无基准跳过）。

        手动档只记账不降档；auto 档滑动窗口超时占比超阈 → 降一档并清窗。
        单次偶发超时不动作（GC/前台抢占都会造成孤立尖峰）。"""
        if dt_s is None or dt_s <= 0:
            return
        over = dt_s > self.fsm_ms / 1000.0 * _OVERRUN_FACTOR
        self._ring.append(over)
        if len(self._ring) > _OVERRUN_WINDOW:
            self._ring.pop(0)
        if (self._choice == "auto" and len(self._ring) >= _OVERRUN_WINDOW
                and sum(self._ring) >= _OVERRUN_RATIO * _OVERRUN_WINDOW):
            idx = TIER_ORDER.index(self._tier)
            if idx < len(TIER_ORDER) - 1:
                down = TIER_ORDER[idx + 1]
                log.warning("FSM tick 持续超时（近 %d 拍 %d 次超时），"
                            "降档 %s → %s", _OVERRUN_WINDOW, sum(self._ring),
                            self._tier, down)
                self._tier = down
            self._ring.clear()
