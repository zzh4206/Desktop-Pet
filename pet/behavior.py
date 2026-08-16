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
    # v0.3.6/7 图层双检查（可选）：callable(x, y, ref=None) -> bool。
    # (x,y) 处的最顶层实体窗是否就是 ref（几何候选窗 dict；ref=None 时
    # 只验"有实体窗"）。win 端 WindowFromPoint+GA_ROOT 实装并比对候选 hwnd
    # ——被全屏窗盖住的窗口会被否决；mac 端不填 → 纯几何判定。
    # v0.3.10 窗口存活实时检查（可选）：callable(ref) -> bool，ref 为候选窗
    # dict——win 端 IsWindow+IsIconic（O(1)），消除 2s 枚举缓存导致的
    # 最小化/关闭后攀爬掉落延迟；mac 端不填 → 维持几何列表判定。
    # 加字段属 Sensors 增量扩展，已登记 工作表/平台差异表。
    solid_at: object = None
    alive_at: object = None
    # v0.3.12 支撑窗实时矩形（可选）：callable(ref) -> dict|None（逻辑坐标）。
    # win 端 GetWindowRect 新鲜读——宠物站窗顶时每 tick 刷新，窗口上移/下移
    # 即时骑乘跟随、移走即坠落（消除 2s 枚举缓存不敏感）。mac 不填 → 几何兜底。
    rect_at: object = None


_IDLE = "idle"
_WALK = "walk"
_DRAG = "drag"
_FALL = "fall"
_THROWN = "thrown"
_CLIMB = "climb"   # 抛掷撞窗口侧面 → 沿边攀爬到顶（v0.3.5）

# 物理常量（进 config 可后续提取；v0.3 先合理默认）
_GRAVITY = 2000.0          # px/s²
_THROW_V_MAX = 2500.0      # 抛出初速度上限（防一手甩飞穿屏）
_DRAG_V_WINDOW_S = 0.12    # 取最近 ~120ms 位移估算 release 初速度
_THROW_VY_DEAD = 200.0     # 垂直速度死区：轻抬/轻压松手视为"放下"而非上抛/下砸
_BOUNCE_MIN_VY = 1200.0    # 落地反弹阈值：撞击慢于此不弹（轻放/轻落即停）
_BOUNCE_RESTITUTION = 0.35 # 恢复系数（弹起高度 ~12%）
_BOUNCE_VX_DAMP = 0.6      # 反弹时水平衰减
_BOUNCE_MAX = 2            # 最多弹 2 次
_WALL_RESTITUTION = 0.4    # 侧墙（工作区左右界）反弹系数
_CLIMB_MIN_DEPTH = 30.0    # 撞侧攀爬的深度阈值：脚下没入窗顶不足此值视为掠顶(落顶站定)
_PET_HEIGHT = 96.0         # 宠物身位高（ADULT 显示尺寸）：窗底净空≥此值可钻过不爬
_ANIM_MIN_S = 15.0         # 随机小动作间隔
_ANIM_MAX_S = 35.0
_ANIM_NAMES = ("stretch", "roll", "blink")


class BehaviorFSM:
    def __init__(self, work_area: dict, cfg: dict | None = None):
        cfg = cfg or {}
        self._work_area = work_area
        self._speed = float(cfg.get("walk_speed", 120))        # px/s
        self._follow_speed = float(cfg.get("follow_speed", 600))  # px/s（follow 跟手，远快于 walk）
        self._climb_min_depth = float(cfg.get("climb_min_depth_px", _CLIMB_MIN_DEPTH))
        self._pet_height = float(cfg.get("pet_height_px", _PET_HEIGHT))
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
        self._climb_edge_x = 0.0          # 攀爬中的窗边 x（bottom_center 贴边）
        self._climb_top_y = 0.0           # 攀爬目标的窗口顶 y
        self._climb_win: dict | None = None  # 攀爬目标窗（实时存活检查用）
        self._alive_at = None             # 窗口存活实时检查（Sensors.alive_at）
        self._rect_at = None              # 支撑窗实时矩形（Sensors.rect_at）
        self._stand_win: dict | None = None  # 当前脚下的支撑窗（骑乘跟随用）
        self._bounce_count = 0            # 落地反弹计数（重置于新抛掷/静止）
        self._solid_at = None             # 图层双检查（Sensors.solid_at 注入）

        # v0.2 数值调制因子（on_state_change 更新；默认 1.0 即不调制）
        self._hunger_factor = 1.0   # 饱食低 → 缩短 idle（觅食感），>1 更频繁走动
        self._mood_factor = 1.0     # 心情低 → 拉长 idle（发呆），>1 更呆

    def set_pet_height(self, h: float) -> None:
        """真实身位高（app 按 sprite 显示尺寸喂入，随阶段进化更新）——
        净空钻行判定用。"""
        self._pet_height = max(16.0, float(h))

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

    def _valid_windows_raw(self):
        """几何有效窗（不含图层校验；_surface_y 里再做身份校验）。"""
        floor = self._bottom()
        top = self._work_area["y"]
        for w in self._windows:
            wy = w["y"]
            if wy >= floor or wy <= top + 8:
                continue
            yield w

    def _alive(self, w: dict) -> bool:
        """窗口实时存活（未关闭/未最小化）。alive_at 未提供/失败 → True。"""
        if self._alive_at is None or w is None:
            return True
        try:
            return bool(self._alive_at(w))
        except Exception:
            return True

    def _rect_of(self, w: dict) -> dict | None:
        """支撑窗实时矩形（rect_at 未提供/失败 → None 走几何兜底）。"""
        if self._rect_at is None or w is None:
            return None
        try:
            return self._rect_at(w)
        except Exception:
            return None

    def _top_surface(self, x: float, y_min: float = -1e9) -> tuple:
        """图层+存活校验下，顶不低于 y_min 的最高表面 → (表面y, 提供窗|None)。

        窗口过滤（防"落到屏幕外消失"）：
        - 顶部高于工作区顶的窗口不算面（最大化窗口 Win32 矩形带 -8px
          隐形 resize 边框，y 为负 → 落上去即屏幕外）；
        - 顶部贴工作区顶（≤顶+8px，最大化/上贴屏窗口）也不算面；
        - 顶面不高于地板（贴底/半贴屏下半）无支撑意义；
        - **图层身份校验**（v0.3.8）：候选窗被别的窗盖住（如全屏窗在前）
          → 不算面——不能站/不是墙/不能落/不作为游走目标，彻底不存在。
        """
        floor = self._bottom()
        cands = [
            w for w in self._valid_windows_raw()
            if w["x"] <= x <= w["x"] + w["width"] and w["y"] >= y_min
        ]
        cands.sort(key=lambda w: w["y"])  # 自上而下找第一个图层可见的
        for w in cands:
            if w["y"] >= floor:
                break
            if self._alive(w) and self._solid_point(x, w["y"] + 5, w):
                return (max(w["y"], self._work_area["y"]), w)
        return (floor, None)

    def _surface_y(self, x: float) -> float:
        return self._top_surface(x)[0]

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
        self._bounce_count = 0
        self._stand_win = None
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
            self._bounce_count = 0
        # 起抛即在窗体内（拎进窗体松手，两分支都查）→ 弹到窗顶上方 1px：
        # 自然落顶站定或飞越；被盖住的窗（图层否决）不弹，直接穿落地板。
        hit = self._window_spanning(self._pos[0], self._pos[1])
        if hit is not None:
            _edge, wy, w = hit
            wl, wr = w["x"], w["x"] + w["width"]
            if self._solid_point(
                min(max(self._pos[0], wl + 8), wr - 8), wy + 5, w
            ):
                self._pos = (
                    min(max(self._pos[0], wl + 8), wr - 8),
                    wy - 1.0,
                )

    # ---- 事件 ----

    def handle_event(self, event: str) -> None:
        """v0.3：fullscreen_on/off（暂停/恢复 WANDER，空中/攀爬传送回底边中央）、
        follow_toggle。"""
        if event == "fullscreen_on":
            self._suppressed = True
            # 全屏中不可见：空中/攀爬态收敛到地面（维持"不往顶上走"）；
            # DRAG 例外（用户还拎着，松手自会落）
            if self._mode in (_THROWN, _FALL, _CLIMB, _WALK):
                cx = self._left() + (self._right() - self._left()) / 2
                self._pos = (cx, self._bottom())
                self._mode = _IDLE
                self._vx = self._vy = 0.0
                self._idle_left = self._new_idle()
        elif event == "fullscreen_off":
            self._suppressed = False
        elif event == "follow_toggle":
            self._follow = not self._follow

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def pos(self) -> tuple:
        return self._pos

    # ---- 主循环 ----

    def _valid_windows(self):
        """有效平台窗（几何过滤；图层校验在 _surface_y/_solid_point 内做）。"""
        yield from self._valid_windows_raw()

    def _window_spanning(self, x: float, y: float, require_band: bool = True):
        """x 落在某有效窗横向范围内且脚下没入其顶超深度阈值
        → (就近边x, 窗顶y, 窗dict)。

        require_band=True（默认）：还须在窗体竖直带内（低于窗底不算——
        拖到窗口正下方松手不该弹顶）；False：只看顶深（走墙撞边从下方
        沿边攀爬的场景，边可达）。
        深度不足（浅掠/拖到窗口上部松手）不判"在窗体内"，由落顶逻辑站上顶。"""
        best = None
        for w in self._valid_windows():
            wl, wr = w["x"], w["x"] + w["width"]
            if not (wl <= x <= wr) or y <= w["y"] + self._climb_min_depth:
                continue
            if require_band and y >= w["y"] + w["height"]:
                continue  # 在窗体下方（窗外）：既不在体内也不该贴边弹顶
            edge = wl if (x - wl) <= (wr - x) else wr
            cand = (float(edge), float(w["y"]), w)
            if best is None or cand[1] < best[1]:
                best = cand
        return best

    def _solid_point(self, x: float, y: float, w: dict | None = None) -> bool:
        """图层检查：x,y 处最顶层实体窗是否就是候选窗 w（None=只验有窗）。

        候选窗被别的窗（如全屏窗）盖住 → WindowFromPoint 返回别人 → 否决。
        solid_at 未提供/调用失败 → True（退纯几何）。兼容旧 2 参签名。"""
        if self._solid_at is None:
            return True
        try:
            return bool(self._solid_at(x, y, w))
        except TypeError:
            try:
                return bool(self._solid_at(x, y))  # v0.3.6 旧签名
            except Exception:
                return True
        except Exception:
            return True

    def _solid_edge(self, edge_x: float, wy: float, y: float) -> bool:
        """攀爬前图层双检查：边线向窗内 5px 体点的顶层窗是否就是候选窗。

        Sensors.solid_at 未提供（mac）时仅几何判定。"""
        if self._solid_at is None:
            return True
        for w in self._valid_windows():
            wl, wr = w["x"], w["x"] + w["width"]
            if abs(w["y"] - wy) < 2 and (abs(wl - edge_x) < 2 or abs(wr - edge_x) < 2):
                inside = wl + 5 if abs(wl - edge_x) < 2 else wr - 5
                return self._solid_point(inside, y, w)
        return True

    def _enter_climb(self, hit: tuple, w: dict | None = None) -> None:
        self._climb_edge_x, self._climb_top_y = hit
        self._climb_win = w
        self._vx = self._vy = 0.0
        self._mode = _CLIMB
        self._pos = (
            self._clamp_x(self._climb_edge_x),
            max(self._pos[1], self._climb_top_y),
        )

    def step(self, state: PetState, sensors: Sensors, dt: float) -> Action:
        # 跟随工作区变化（Dock 显隐 / 多屏）；窗口列表每 tick 换引用零成本
        if sensors.work_area:
            self._work_area = sensors.work_area
        self._windows = sensors.windows or []
        self._solid_at = sensors.solid_at
        self._alive_at = sensors.alive_at
        self._rect_at = sensors.rect_at

        # 防御钳制：任何状态下都在工作区内（防偶发消失）
        x, y = self._pos
        cx = self._clamp_x(x)
        cy = min(max(y, self._work_area["y"]), self._bottom())
        if (cx, cy) != (x, y):
            self._pos = (cx, cy)

        # 支撑校验（IDLE/WALK）：骑乘跟随 + 关闭/最小化/拖走坠落
        if self._mode in (_IDLE, _WALK):
            x, y = self._pos
            w = self._stand_win
            rect = self._rect_of(w) if w is not None else None
            if w is not None and rect is not None:
                # 实时矩形路径（win）：窗口移动即时响应
                if not self._alive(w) or not (
                    rect["x"] <= x <= rect["x"] + rect["width"]
                ) or rect["y"] >= self._bottom() - 1:
                    # 关闭/最小化/横向移开/贴底失效 → 坠落
                    self._stand_win = None
                    self._mode = _FALL
                    self._vx = self._vy = 0.0
                    return Action(ActionType.FALL, {"pos": self._pos})
                if abs(rect["y"] - y) > 2:
                    # 骑乘：窗口上/下移，脚贴合新窗顶（上移即"被吞没弹顶"）
                    self._pos = (x, rect["y"])
            else:
                # 几何兜底（无 rect_at / 未知支撑窗）
                sy, w2 = self._top_surface(x)
                if sy > y + 2:
                    # 脚下无面（窗关闭/最小化/移走）→ 坠落
                    self._stand_win = None
                    self._mode = _FALL
                    self._vx = self._vy = 0.0
                    return Action(ActionType.FALL, {"pos": self._pos})
                if self._stand_win is not None and sy < y - 2 and w2 is not None:
                    # 原本站在窗上、其上移吞没（2s 缓存兜底）→ 弹到新顶；
                    # 悬浮窗下方行走的 sy < y 不属于此情形（stand_win 为 None）
                    self._pos = (x, sy)
                # 只有脚确实贴着面才记支撑窗（防误关联头顶窗）
                self._stand_win = (
                    w2 if abs(sy - self._pos[1]) <= 2 else None
                )

        if self._mode == _DRAG:
            return Action(ActionType.MOVE_TO, {"pos": self._pos})

        if self._mode == _CLIMB:
            return self._step_climb(dt)

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
        """行走层 = 脚部实际高度（不用几何面回写 y，防悬浮窗下被吸上顶）。

        ns（下一步表面）相对脚部 cur_y 三种情形：
        - ≈ 同层：正常走，y 贴 ns；
        - 高于脚（头顶有窗）：净空判定——窗底距脚 ≥ 身高 → 钻过（保持层），
          否则贴边攀爬；
        - 低于脚（走出边缘）：掉落。
        """
        x, cur_y = self._pos
        tx, _ty = self._target
        stride = (self._follow_speed if self._follow else self._speed) * dt
        nx = x + (stride if tx > x else -stride)
        nx = self._clamp_x(nx)
        ns = self._surface_y(nx)

        if ns < cur_y - 1:
            hit = self._window_spanning(nx, cur_y, require_band=False)
            if hit is not None:
                w = hit[2]
                # 重叠窗：w 横向范围含当前 pos.x → 不爬中间边线，平走保持层
                # （只有走到不重叠的相邻窗边线才爬，防"重叠沿边线爬上去"）
                if w["x"] <= x <= w["x"] + w["width"]:
                    ns = cur_y
                else:
                    gap = cur_y - (w["y"] + w["height"])
                    if gap >= self._pet_height:
                        ns = cur_y  # 窗底净空足够：钻过去，保持当前层
                    else:
                        # 走到边框就往上爬（贴撞入侧边线）
                        self._enter_climb((hit[0], hit[1]), w)
                        return Action(ActionType.MOVE_TO, {"pos": self._pos})
            else:
                ns = cur_y  # 找不到明确窗边（传感器瞬断等）：保守平走

        elif ns > cur_y + 1:
            # 走出边缘：脚下无支撑 → 掉落
            self._mode = _FALL
            self._vx = 0.0
            self._vy = 0.0
            self._pos = (nx, cur_y)
            return Action(ActionType.FALL, {"pos": self._pos})

        if abs(tx - nx) <= stride or (tx > x) != (tx > nx):
            # 到达目标
            self._pos = (tx, ns)
            self._mode = _IDLE
            self._idle_left = self._new_idle()
        else:
            self._pos = (nx, ns)
        return Action(ActionType.MOVE_TO, {"pos": self._pos})

    def _hit_window_side(self, x0: float, x1: float, y: float):
        """扫掠检测：本 tick 位移线段 (x0→x1) 穿过某有效窗的左右边线，
        且脚下没入窗顶超深度阈值（真撞侧面，非掠顶）→ (贴边x, 窗顶y)。
        越过整扇窗（进+出同 tick）也能命中；命中后 _solid_edge 图层双检查
        （候选窗被盖住则否决）。"""
        best = None
        for w in self._valid_windows():
            wy = w["y"]
            if y <= wy + self._climb_min_depth:
                continue  # 高于窗顶+阈值：飞越/浅掠顶，不判撞侧
            if y >= wy + w["height"]:
                continue  # 低于窗底：从窗体下方穿过，不撞侧
            wl, wr = w["x"], w["x"] + w["width"]
            # 线段 (x0→x1) 与边线相交（端点异侧）
            if (x0 - wl) * (x1 - wl) < 0 or (x0 - wr) * (x1 - wr) < 0:
                edge = wl if x0 < wl else wr  # 从哪侧撞入贴哪条边
                cand = (float(edge), float(wy), w)
                if best is None or cand[1] < best[1]:
                    best = cand
        if best is not None and self._solid_edge(best[0], best[1], y):
            return best
        return None

    def _fall_surface(self, x: float, y_prev: float) -> float:
        """下落落点面：只算**不低于起落点(浅带内除外)**的面。

        - 从上方跨越的窗顶可落（正常落顶）；
        - 浅带（顶上方到顶下 climb_min_depth 内）视为"贴着顶"，可吸附上顶；
        - 深于浅带（窗体中下部/窗底下方）→ 该窗顶不可落 → 落向地板，
          消除"窗下松手瞬移上顶"。"""
        floor = self._bottom()
        cands = [
            w for w in self._valid_windows_raw()
            if w["x"] <= x <= w["x"] + w["width"]
            and w["y"] >= y_prev - self._climb_min_depth
        ]
        cands.sort(key=lambda w: w["y"])
        for w in cands:
            if w["y"] >= floor:
                break
            if self._alive(w) and self._solid_point(x, w["y"] + 5, w):
                return max(w["y"], self._work_area["y"])
        return floor

    def _step_air(self, dt: float) -> Action:
        """FALL/THROWN 共用：重力积分 + 窗侧扫掠攀爬 + 落地弹跳 + 边界。"""
        self._vy += _GRAVITY * dt
        x0 = self._pos[0]
        raw_x = x0 + self._vx * dt
        x = self._clamp_x(raw_x)
        y = self._pos[1] + self._vy * dt
        # 不飞出：顶部钳制（撞工作区顶即贴住，vy 清零）
        if y < self._work_area["y"]:
            y = float(self._work_area["y"])
            self._vy = max(self._vy, 0.0)
        # 侧墙（工作区左右界）反弹（仅抛掷态；掉落无水平速度不涉及）
        if self._mode == _THROWN and raw_x != x:
            self._vx = -self._vx * _WALL_RESTITUTION
            x0 = x  # 后续扫掠从墙点起，防穿透检测误报
        # 抛掷横向撞窗侧 → 贴边攀爬（不瞬移上顶）
        if self._mode == _THROWN and self._vx != 0.0:
            hit = self._hit_window_side(x0, x, y)
            if hit is not None:
                self._enter_climb((hit[0], hit[1]), hit[2])
                return Action(ActionType.MOVE_TO, {"pos": self._pos})
        sy = self._fall_surface(x, self._pos[1])  # 从上方跨越才算落顶
        if y >= sy:
            impact = self._vy
            # 落地反弹：撞击够快 + 弹数未尽 → 弹起（否则停稳）
            if (
                self._mode == _THROWN
                and impact > _BOUNCE_MIN_VY
                and self._bounce_count < _BOUNCE_MAX
            ):
                self._bounce_count += 1
                self._vy = -impact * _BOUNCE_RESTITUTION
                self._vx *= _BOUNCE_VX_DAMP
                self._pos = (x, sy)
                return Action(
                    ActionType.MOVE_TO,
                    {"pos": self._pos, "bounced": self._bounce_count},
                )
            # 触面停止（spec：落到底边/表面停止）
            self._pos = (x, sy)
            self._vx = 0.0
            self._vy = 0.0
            self._mode = _IDLE
            self._bounce_count = 0
            self._idle_left = self._new_idle()
            self._stand_win = self._top_surface(x)[1]  # 记住支撑窗（骑乘用）
            return Action(ActionType.MOVE_TO, {"pos": self._pos})
        self._pos = (x, y)
        return Action(
            ActionType.FALL if self._mode == _FALL else ActionType.MOVE_TO,
            {"pos": self._pos, "vx": self._vx, "vy": self._vy},
        )

    def _step_climb(self, dt: float) -> Action:
        """沿窗边以步行速度逐渐爬到顶；窗口消失/最小化则立即坠落。

        优先用 Sensors.alive_at 实时检查（win 端 IsWindow+IsIconic，O(1)），
        消除 2s 枚举缓存延迟；无 alive_at（mac）时退几何列表判定。"""
        if self._climb_win is not None and self._alive_at is not None:
            still = self._alive(self._climb_win)
        else:
            still = any(
                abs(w["y"] - self._climb_top_y) < 2
                and (
                    abs(w["x"] - self._climb_edge_x) < 2
                    or abs(w["x"] + w["width"] - self._climb_edge_x) < 2
                )
                for w in self._windows
            )
        if not still:
            self._mode = _FALL
            self._vx = 0.0
            self._vy = 0.0
            return Action(ActionType.FALL, {"pos": self._pos})
        y = self._pos[1] - self._speed * dt
        if y <= self._climb_top_y:
            # 登顶站定
            self._pos = (self._climb_edge_x, self._climb_top_y)
            self._mode = _IDLE
            self._idle_left = self._new_idle()
        else:
            self._pos = (self._climb_edge_x, y)
        return Action(ActionType.MOVE_TO, {"pos": self._pos})
