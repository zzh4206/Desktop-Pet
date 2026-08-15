"""行为 FSM（差异化①）—— 接口冻结于 设计思路.md §2.2。

v0.1：``ActionType`` + ``Action`` + ``Sensors`` + ``BehaviorFSM`` 骨架，
只有 ``idle`` + ``沿底边随机走`` 两状态。``idle`` 用随机间隔（默认 5–15s，
首段 3s）触发一次沿底边随机行走，走完回 idle。WANDER / 拖拽 / 抛掷是 v0.3。

FSM 只消费 ``Sensors``（work_area 来自平台 sensor），不碰平台 API。
v0.1 内部持有屏幕坐标用于位移插值；v0.3 物理层接管 screen_pos 时再重构。
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from enum import Enum

from .pet_state import PetState


class ActionType(Enum):
    MOVE_TO = "move_to"
    FALL = "fall"
    ANIMATE = "animate"
    SPEAK = "speak"
    EAT_MOUSE = "eat_mouse"
    FOLLOW_CURSOR = "follow_cursor"

    # WANDER 是 FSM 行为模式（反复发 MOVE_TO 随机目标），非独立 ActionType


@dataclass
class Action:
    type: ActionType
    params: dict = field(default_factory=dict)


@dataclass
class Sensors:
    mouse_pos: tuple = (0, 0)
    work_area: dict = field(default_factory=dict)  # {x,y,width,height} Qt top-left
    windows: list = field(default_factory=list)
    idle_time: float = 0.0


_IDLE = "idle"
_WALK = "walk"


class BehaviorFSM:
    def __init__(self, work_area: dict, cfg: dict | None = None):
        cfg = cfg or {}
        self._work_area = work_area
        self._speed = float(cfg.get("walk_speed", 120))        # px/s
        self._idle_min = float(cfg.get("wander_idle_min_s", 5))
        self._idle_max = float(cfg.get("wander_idle_max_s", 15))
        self._margin = float(cfg.get("edge_margin_px", 40))

        bottom = work_area.get("y", 0) + work_area.get("height", 0)
        cx = work_area.get("x", 0) + work_area.get("width", 0) / 2
        self._pos: tuple[float, float] = (cx, bottom)  # bottom_center
        self._target: tuple[float, float] = self._pos
        self._mode = _IDLE
        self._idle_left = float(cfg.get("first_idle_s", 3))
        self._first = True

    def _bottom(self) -> float:
        return self._work_area["y"] + self._work_area["height"]

    def _new_target(self) -> tuple[float, float]:
        wa = self._work_area
        x = random.uniform(
            wa["x"] + self._margin, wa["x"] + wa["width"] - self._margin
        )
        return (x, self._bottom())

    def _new_idle(self) -> float:
        if self._first:
            self._first = False
            return 3.0
        return random.uniform(self._idle_min, self._idle_max)

    def handle_event(self, event: str) -> None:
        """v0.2/v0.3 填（如 ProactiveScheduler 触发 EAT_MOUSE）。"""
        pass

    def step(self, state: PetState, sensors: Sensors, dt: float) -> Action:
        # 跟随工作区变化（Dock 显隐 / 多屏）
        if sensors.work_area:
            self._work_area = sensors.work_area
            self._pos = (self._pos[0], self._bottom())

        if self._mode == _IDLE:
            self._idle_left -= dt
            if self._idle_left <= 0:
                self._target = self._new_target()
                self._mode = _WALK
            return Action(ActionType.ANIMATE)

        # WALK：沿底边向目标插值
        x, _y = self._pos
        tx, ty = self._target
        dx = tx - x
        stride = self._speed * dt
        if abs(dx) <= stride:
            x = tx
            self._mode = _IDLE
            self._idle_left = self._new_idle()
        else:
            x += stride if dx > 0 else -stride
        self._pos = (x, ty)
        return Action(ActionType.MOVE_TO, {"pos": self._pos})
