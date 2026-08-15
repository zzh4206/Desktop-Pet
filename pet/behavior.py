"""行为 FSM（差异化①）—— 接口冻结于 设计思路.md §2.2。

v0.1：``ActionType`` + ``Action`` + ``Sensors`` + ``BehaviorFSM`` 骨架，
只有 ``idle`` + ``沿底边随机走`` 两状态。
v0.2：数值调制（``on_state_change``：饱食低觅食 / 心情低发呆）。
v0.3：完整桌面物理——``IDLE/WALK/DRAG/FALL/THROWN`` 五态 + WANDER（全工作
区随机游走，尊重重力：地板/窗口顶面走，跨表面掉落）+ 拖拽抛掷（release 带
初速度走抛物线）+ follow_cursor 模式 + 随机小动作 + 全屏抑制（暂停 WANDER）。

FSM 只消费 ``Sensors``（work_area/windows 来自平台 sensor），不碰平台 API。
``windows`` 是逻辑坐标 {x,y,width,height} 列表（win EnumWindows / mac AX 端
口），只取**顶面**作可行走表面。物理只改自身 ``screen_pos``（bottom_center），
不碰养成 state（接口隔离）。
"""

from __future__ import annotations

import random
import time
from collections import deque
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
_DRAG = "drag"
_FALL = "fall"
_THROWN = "thrown"

# 物理常量（进 config 可后续提取；v0.3 先合理默认）
_GRAVITY = 2000.0          # px/s²
_THROW_V_MAX = 2500.0      # 抛出初速度上限（防一手甩飞穿屏）
_DRAG_V_WINDOW_S = 0.12    # 取最近 ~120ms 位移估算 release 初速度
_THROW_VY_DEAD = 200.0     # 垂直速度死区：轻抬/轻压松手视为"放下"而非上抛/下砸
_ANIM_MIN_S = 15.0         # 随机小动作间隔
_ANIM_MAX_S = 35.0
_ANIM_NAMES = ("stretch", "roll", "blink")


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

        # v0.3 物理/模式
        self._vx = 0.0
        self._vy = 0.0
        self._windows: list[dict] = []
        self._follow = False
        self._suppressed = False   # 全屏：暂停 WANDER（不出新目标）
        self._anim_left = random.uniform(_ANIM_MIN_S, _ANIM_MAX_S)
        self._drag_hist: deque = deque()  # (t, x, y) release 初速度估算

        # v0.2 数值调制因子（on_state_change 更新；默认 1.0 即不调制）
        self._hunger_factor = 1.0   # 饱食低 → 缩短 idle（觅食感），>1 更频繁走动
        self._mood_factor = 1.0     # 心情低 → 拉长 idle（发呆），>1 更呆

    # ---- v0.2 数值调制（mac 主笔，保留不动） ----

    def on_state_change(self, state: PetState) -> None:
        """饱食<20 → 觅食（idle 缩短）；心情<30 → 发呆（idle 拉长）；
        v0.3：心情>60 → WANDER 更频繁（idle 再缩短）。"""
        if state.fullness < 20:
            self._hunger_factor = 2.0
        else:
            self._hunger_factor = 1.0
        if state.mood < 30:
            self._mood_factor = 2.0
        elif state.mood > 60:
            self._mood_factor = 0.7
        else:
            self._mood_factor = 1.0

    # ---- 几何 / 表面 ----

    def _bottom(self) -> float:
        return self._work_area["y"] + self._work_area["height"]

    def _left(self) -> float:
        return self._work_area["x"]

    def _right(self) -> float:
        return self._work_area["x"] + self._work_area["width"]

    def _surface_y(self, x: float) -> float:
        """x 处的站立面：窗口顶面取最高（最小 y），否则工作区底边。

        站立面过滤（防"落到屏幕外消失"）：
        - 顶部高于工作区顶的窗口不算面（最大化窗口 Win32 矩形带 -8px
          隐形 resize 边框，y 为负 → 落上去即屏幕外）；
        - 顶部贴工作区顶（≤顶+8px，最大化/上贴屏窗口）也不算面——
          站在整屏窗口的上边缘等价挂屏幕顶，不是有效平台；
        - 顶面不高于地板（贴底/半贴屏下半）无支撑意义。
        多窗重叠取最上面的面；结果再钳制不低于工作区顶（双保险）。
        """
        floor = self._bottom()
        top = self._work_area["y"]
        best = floor
        for w in self._windows:
            wy = w.get("y", floor)
            if wy >= floor or wy <= top + 8:
                continue
            if w["x"] <= x <= w["x"] + w["width"]:
                best = min(best, wy)
        return max(best, top)

    def _clamp_x(self, x: float) -> float:
        """不穿屏：x 限制在工作区横向范围内。"""
        return max(self._left() + 1.0, min(self._right() - 1.0, x))

    # ---- WANDER ----

    def _new_target(self) -> tuple[float, float]:
        wa = self._work_area
        x = random.uniform(
            wa["x"] + self._margin, wa["x"] + wa["width"] - self._margin
        )
        return (x, self._surface_y(x))

    def _new_idle(self) -> float:
        if self._first:
            self._first = False
            return 3.0
        # v0.2 数值调制：饱食低 → 缩短（÷hunger_factor），心情低 → 拉长（×mood_factor）
        base = random.uniform(self._idle_min, self._idle_max)
        scaled = base * self._mood_factor / self._hunger_factor
        return max(0.5, scaled)

    # ---- 拖拽 API（window 拖拽 signal → app 调用） ----

    def begin_drag(self, cursor: tuple) -> None:
        """按住宠物：进入 DRAG，位置=光标（bottom_center 钉在光标）。"""
        self._mode = _DRAG
        self._vx = self._vy = 0.0
        self._drag_hist.clear()
        self._drag_hist.append((time.monotonic(), cursor[0], cursor[1]))

    def drag_move(self, cursor: tuple) -> None:
        self._pos = (self._clamp_x(cursor[0]), max(cursor[1], 1.0))
        self._drag_hist.append((time.monotonic(), cursor[0], cursor[1]))

    def end_drag(self) -> None:
        """松手：最近 ~120ms 位移/时间 → 初速度；太小则自然下落。"""
        now = time.monotonic()
        while len(self._drag_hist) > 2 and now - self._drag_hist[0][0] > _DRAG_V_WINDOW_S:
            self._drag_hist.popleft()
        if len(self._drag_hist) >= 2:
            t0, x0, y0 = self._drag_hist[0]
            t1, x1, y1 = self._drag_hist[-1]
            dt = max(t1 - t0, 1e-3)
            self._vx = (x1 - x0) / dt
            # 屏幕 y 向下为正，上抛 vy 取负
            self._vy = (y1 - y0) / dt
        v = (self._vx**2 + self._vy**2) ** 0.5
        if v > _THROW_V_MAX:
            k = _THROW_V_MAX / v
            self._vx *= k
            self._vy *= k
        self._drag_hist.clear()
        # 垂直死区：|vy|<200（轻抬/轻压的手部微颤）不判抛 → 垂直放下
        if abs(self._vy) < _THROW_VY_DEAD:
            self._vy = 0.0
        # 垂直速度≈0 且水平速度很小 → 直接落地（拎起来原地放下）
        if abs(self._vy) < 50 and abs(self._vx) < 100:
            self._mode = _FALL
            self._vx = 0.0
            self._vy = 0.0
        else:
            self._mode = _THROWN

    # ---- 事件 ----

    def handle_event(self, event: str) -> None:
        """v0.3：fullscreen_on/off（暂停/恢复 WANDER）、follow_toggle。"""
        if event == "fullscreen_on":
            self._suppressed = True
        elif event == "fullscreen_off":
            self._suppressed = False
        elif event == "follow_toggle":
            self._follow = not self._follow
        elif event == "fullscreen_check_off":
            self._suppressed = False

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def pos(self) -> tuple:
        return self._pos

    # ---- 主循环 ----

    def step(self, state: PetState, sensors: Sensors, dt: float) -> Action:
        # 跟随工作区变化（Dock 显隐 / 多屏）；窗口列表每 tick 换引用零成本
        if sensors.work_area:
            self._work_area = sensors.work_area
        self._windows = sensors.windows or []

        if self._mode == _DRAG:
            return Action(ActionType.MOVE_TO, {"pos": self._pos})

        if self._mode in (_FALL, _THROWN):
            return self._step_air(dt)

        if self._mode == _WALK:
            return self._step_walk(dt)

        # IDLE
        action = Action(ActionType.ANIMATE)
        # 随机小动作（v0.3 Must：1min 内至少一次；全屏抑制时不出）
        self._anim_left -= dt
        if self._anim_left <= 0 and not self._suppressed:
            self._anim_left = random.uniform(_ANIM_MIN_S, _ANIM_MAX_S)
            action = Action(
                ActionType.ANIMATE,
                {"name": random.choice(_ANIM_NAMES)},
            )
        self._idle_left -= dt
        # follow 模式：光标即目标（全屏抑制时不追）
        if self._follow and not self._suppressed and sensors.mouse_pos:
            tx = self._clamp_x(sensors.mouse_pos[0])
            ty = self._surface_y(tx)
            if abs(tx - self._pos[0]) > 4:
                self._target = (tx, ty)
                self._mode = _WALK
                return action
        # WANDER 自发目标：follow 模式下不出（光标即目标，防到点后被游走带离）
        if self._idle_left <= 0 and not self._suppressed and not self._follow:
            self._target = self._new_target()
            self._mode = _WALK
        return action

    def _step_walk(self, dt: float) -> Action:
        x, cur_y = self._pos
        tx, _ty = self._target
        stride = self._speed * dt
        nx = x + (stride if tx > x else -stride)
        nx = self._clamp_x(nx)
        cur_surface = self._surface_y(x)
        ns_surface = self._surface_y(nx)
        # 跨表面掉落：当前站在窗口顶面（高于地板），下一步脚下变地板/无支撑
        if cur_surface < self._bottom() - 1 and ns_surface > cur_surface + 1 \
                and abs(cur_y - cur_surface) < 2:
            self._mode = _FALL
            self._vx = 0.0
            self._vy = 0.0
            self._pos = (nx, cur_y)
            return Action(ActionType.FALL, {"pos": self._pos})
        # 窗壁：下一步表面高于当前站立面 → v0.3 不攀爬（留后），视为到达停下
        if ns_surface < cur_surface - 1:
            self._mode = _IDLE
            self._idle_left = self._new_idle()
            return Action(ActionType.MOVE_TO, {"pos": self._pos})
        if abs(tx - nx) <= stride or (tx > x) != (tx > nx):
            # 到达目标
            self._pos = (tx, ns_surface)
            self._mode = _IDLE
            self._idle_left = self._new_idle()
        else:
            self._pos = (nx, ns_surface)
        return Action(ActionType.MOVE_TO, {"pos": self._pos})

    def _step_air(self, dt: float) -> Action:
        """FALL/THROWN 共用：重力积分 + 落地判定 + 边界钳制。"""
        self._vy += _GRAVITY * dt
        x = self._clamp_x(self._pos[0] + self._vx * dt)
        y = self._pos[1] + self._vy * dt
        # 不飞出：顶部钳制（撞工作区顶即贴住，vy 清零）
        if y < self._work_area["y"]:
            y = float(self._work_area["y"])
            self._vy = max(self._vy, 0.0)
        sy = self._surface_y(x)
        if y >= sy:
            # 落地：触面停止（spec：落到底边/表面停止，不弹跳）
            self._pos = (x, sy)
            self._vx = 0.0
            self._vy = 0.0
            self._mode = _IDLE
            self._idle_left = self._new_idle()
            return Action(ActionType.MOVE_TO, {"pos": self._pos})
        self._pos = (x, y)
        return Action(
            ActionType.FALL if self._mode == _FALL else ActionType.MOVE_TO,
            {"pos": self._pos, "vx": self._vx, "vy": self._vy},
        )
