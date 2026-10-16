"""番茄专注（可选节律器）—— 设计-番茄模块技术方案.md §3。

可选 opt-in 功能：**不开启 = 不存在**，对既有行为零影响（IDLE 惰性接管
不变量，§1）。用户显式 ``start()`` 才进入节律并短暂接管；``stop()`` 立即
还原 IDLE。IDLE 态对吃鼠标/久坐/纠缠/主动关怀零影响——上层门禁统一判
``focus.active``（== state != IDLE），恒 false 时行为与未引入本模块前
逐字节一致。

平台库-free：时钟/气泡/音效/FSM 事件/养成 store/空闲探测全部注入
（``now_fn``/``bubble_fn``/``sound_fn``/``fsm_event_fn``/``store``/
``idle_fn``），**无 import AppKit/WinDLL/ctypes**——平台库-free 的代码
约束 + 开发者本地单测全绿（本仓无 CI，见技术方案 §1/§6 更正）。虚拟时钟
（now_fn 注入）让单测全路径推演，系统睡眠用 epoch deadline
比较（醒来发现过期 → 立即迁移，不补算）。

迁移链：IDLE→FOCUS(work_min)→SHORT(short_min)→FOCUS…每 ``cycles`` 个
番茄→LONG(long_min)；LONG 自然收尾→IDLE（本轮结束），``skip()`` 长休→FOCUS
（新一轮）；任意态 ``stop()``→IDLE。专注段 skip 不计完成直接进休息（诚实
口径）；休息段 skip 不计打断。
"""

from __future__ import annotations

import json
import logging
import os
import random
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum

_log = logging.getLogger("pet")

# ---- 默认参数（进 config.focus 覆盖；schema 校验见 config.py） ----
_DEFAULT_WORK_MIN = 25.0
_DEFAULT_SHORT_MIN = 5.0
_DEFAULT_LONG_MIN = 15.0
_DEFAULT_CYCLES = 4
_DEFAULT_MOOD_GAIN = 3.0

# 休息执法（break_enforce，默认关）计量窗口（§9 拍板项）：
# 休息开始 30s 窗口内，键鼠活动累计每 10s 记一次打断；连续 2 次（累计
# ≥20s）才温和气泡——单次误触不提醒，降低烦人度。
_BREAK_GRACE_S = 30.0
_BREAK_ACTIVITY_THRESHOLD_S = 10.0
_BREAK_ACTIVE_IDLE_S = 2.0   # 系统空闲 < 2s 视为「有键鼠活动」

# 重启重水化：deadline 未大幅过期（5 分钟内）且同日 → 恢复进行中节律；
# 否则只恢复 stats（§3）。上限 1 天防损坏档案的离奇 epoch。
_RESUME_GRACE_S = 300.0
_RESUME_MAX_FORWARD_S = 86400.0


class FocusPhase(Enum):
    IDLE = "idle"
    FOCUS = "focus"
    SHORT = "short"
    LONG = "long"


@dataclass
class FocusStats:
    """当日统计（focus_state.json 载体，§3）。跨日重置键 = ``date``。"""

    date: str = ""          # 本地日期 ISO（对齐 greeted 去重模式）
    completed: int = 0      # 今日完成番茄数
    focus_sec: int = 0      # 今日累计专注秒
    breaks_cut: int = 0     # 今日休息被提前打断次数（break_enforce 计量）
    streak_days: int = 0    # 连续达标天数（有完成的天数，隔日无完成归零）


# ---- 提醒文案池（纯数据，离线零依赖） ----
_FOCUS_START = (
    "开始专注啦🍅 我陪你安静待着～",
    "专注时间到，先把手头的做完吧～",
)
_FOCUS_DONE = (
    "专注完成！休息一下吧，看看远处放松眼睛～",
    "这一轮结束啦，起来活动活动～",
)
_REST_DONE = (
    "休息结束，继续加油🍅～",
    "满血复活，开始下一轮～",
)
_SKIP_FOCUS = (
    "先跳过这轮，歇口气吧～",
)
_SKIP_REST = (
    "好，那我们接着来～",
)
_BREAK_CUT = (
    "休息时间还没结束哦，再歇一小会儿眼睛吧～",
    "先别急，让眼睛再休息一下～",
)


def _today(now: float) -> str:
    try:
        return datetime.fromtimestamp(now).date().isoformat()
    except (OSError, OverflowError, ValueError):
        return ""


def _yesterday(today_iso: str) -> str:
    try:
        return (datetime.fromisoformat(today_iso).date()
                - timedelta(days=1)).isoformat()
    except ValueError:
        return ""


def _num(v, lo: float, hi: float) -> float:
    try:
        return min(hi, max(lo, float(v)))
    except (TypeError, ValueError):
        return lo


def _int(v, lo: int, hi: int) -> int:
    try:
        return min(hi, max(lo, int(v)))
    except (TypeError, ValueError):
        return lo


def _pick(pool) -> str:
    return random.choice(pool)


class FocusSession:
    """可选番茄节律器（对标 EatMouseSession 接口风格，proactive.py:96）。

    构造注入（§3）：now_fn / bubble_fn / fsm_event_fn / mouse_lock /
    sound_fn / store / idle_fn / fullscreen_fn / dnd_fn；全部平台库-free，
    未注入即对应能力静默降级（无气泡/无音效/无养成联动/无休息执法）。
    默认 IDLE = 零接管（§1 不变量）。
    """

    def __init__(self, now_fn=time.time, bubble_fn=None, fsm_event_fn=None,
                 mouse_lock=None, sound_fn=None, store=None, cfg=None,
                 state_path=None, idle_fn=None, fullscreen_fn=None,
                 dnd_fn=None):
        self._now = now_fn
        self._bubble = bubble_fn
        self._fsm = fsm_event_fn
        self._mouse_lock = mouse_lock     # 预留：break_enforce 完整抑制升级用（§4.4 不接）
        self._sound = sound_fn
        self._store = store               # 养成 store（完成番茄 +mood，0.25.5）
        self._idle = idle_fn              # callable() -> float 空闲秒（break_enforce，0.25.4）
        self._fullscreen_fn = fullscreen_fn  # callable() -> bool（全屏只气泡不切姿态，0.25.3）
        self._dnd_fn = dnd_fn

        c = cfg or {}
        self._work_min = _num(c.get("work_min", _DEFAULT_WORK_MIN), 1.0, 180.0)
        self._short_min = _num(c.get("short_min", _DEFAULT_SHORT_MIN), 1.0, 60.0)
        self._long_min = _num(c.get("long_min", _DEFAULT_LONG_MIN), 1.0, 120.0)
        self._cycles = _int(c.get("cycles", _DEFAULT_CYCLES), 1, 12)
        self._break_enforce = bool(c.get("break_enforce", False))
        self._mood_gain = _num(c.get("mood_gain", _DEFAULT_MOOD_GAIN), 0.0, 20.0)

        self._state = FocusPhase.IDLE
        self._deadline: float | None = None
        self._completed_in_cycle = 0     # 本轮已完成番茄数（决定 SHORT/LONG）
        self._stats = FocusStats()
        self._state_path = state_path
        self._dnd = False

        # break_enforce 窗口态（0.25.4 启用）
        self._rest_started_at: float | None = None
        self._rest_activity_s = 0.0
        self._last_break_tick = 0.0
        self._rest_cut_count = 0
        self._break_bubbled = False

        self._load_persisted()

    # ---- 状态读口（0.24 纠缠线 duck-typing 探测 getattr(focus,"state")） ----

    @property
    def state(self) -> FocusPhase:
        return self._state

    @property
    def stats(self) -> FocusStats:
        return self._stats

    @property
    def active(self) -> bool:
        """== (state != IDLE)。上层互斥门禁统一用此（§1 不变量）。"""
        return self._state != FocusPhase.IDLE

    # ---- 用户操作 ----

    def start(self, minutes: float | int | None = None) -> None:
        """IDLE→FOCUS；已运行则改期重启（显式 start = 新一轮，cycle 归零）。"""
        now = self._now()
        self._rollover(now)
        if minutes is not None:
            try:
                m = float(minutes)
                if m >= 1.0:
                    self._work_min = min(180.0, m)
            except (TypeError, ValueError):
                pass
        self._completed_in_cycle = 0
        self._enter(FocusPhase.FOCUS, self._work_min, now)

    def stop(self) -> None:
        """任意态→IDLE，释放全部接管（当前番茄不计完成）。幂等。"""
        if self._state == FocusPhase.IDLE:
            return
        self._enter_idle(self._now())

    def skip(self) -> None:
        """跳过当前段进下一段。专注段 skip 不计完成直接进短休；短休 skip
        进 FOCUS；长休 skip 进 FOCUS 且新一轮（cycle 归零）。休息 skip 不计打断。"""
        if self._state == FocusPhase.IDLE:
            return
        now = self._now()
        phase = self._state
        if phase == FocusPhase.FOCUS:
            self._emit(_SKIP_FOCUS)
            self._enter(FocusPhase.SHORT, self._short_min, now)
        elif phase == FocusPhase.SHORT:
            self._emit(_SKIP_REST)
            self._enter(FocusPhase.FOCUS, self._work_min, now)
        else:  # LONG
            self._emit(_SKIP_REST)
            self._completed_in_cycle = 0
            self._enter(FocusPhase.FOCUS, self._work_min, now)

    def tick(self) -> None:
        """非 IDLE 才有效：deadline 比较 + 迁移 + 提醒（幂等）。IDLE 直接返
        回零副作用（§1 不变量）。"""
        self._refresh_dnd()
        if self._state == FocusPhase.IDLE:
            return
        now = self._now()
        if self._deadline is None or now < self._deadline:
            self._tick_break_enforce(now)
            return
        self._advance(now)

    def on_dnd_active(self) -> bool:
        """DND 生效：只静默提醒（气泡/音效），节律照走（与吃鼠标「DND 立即
        吐出」相反，§9）。返回「是否正在跑」（对齐 EatMouseSession 语义）。"""
        self._dnd = True
        return self.active

    # ---- 内部：迁移 ----

    def _advance(self, now: float) -> None:
        phase = self._state
        if phase == FocusPhase.FOCUS:
            self._on_focus_complete(now)
            self._emit(_FOCUS_DONE)
            self._completed_in_cycle += 1
            if self._completed_in_cycle >= self._cycles:
                self._enter(FocusPhase.LONG, self._long_min, now)
            else:
                self._enter(FocusPhase.SHORT, self._short_min, now)
        elif phase == FocusPhase.SHORT:
            self._emit(_REST_DONE)
            self._enter(FocusPhase.FOCUS, self._work_min, now)
        else:  # LONG 自然收尾 → 本轮结束回 IDLE（§3 FOCUS/IDLE 两分支之 IDLE）
            self._emit(_REST_DONE)
            self._enter_idle(now)

    def _enter(self, phase: FocusPhase, minutes: float, now: float) -> None:
        self._state = phase
        self._deadline = now + float(minutes) * 60.0
        self._reset_break_window()
        if phase == FocusPhase.FOCUS:
            self._fsm_event("focus_rest_on")
            self._emit(_FOCUS_START)
        else:
            self._fsm_event("focus_rest_off")
        self._persist()

    def _enter_idle(self, now: float) -> None:
        self._state = FocusPhase.IDLE
        self._deadline = None
        self._completed_in_cycle = 0
        self._reset_break_window()
        self._fsm_event("focus_rest_off")
        self._persist()

    def _on_focus_complete(self, now: float) -> None:
        self._rollover(now)
        if self._stats.completed == 0:
            # 今日首次完成：streak +1（昨日有完成则续，否则从 1 起算）
            self._stats.streak_days += 1
        self._stats.completed += 1
        self._stats.focus_sec += int(round(self._work_min * 60.0))
        # 养成联动（0.25.5）：完成番茄 → 心情 +mood_gain（store 未注入静默）
        if self._store is not None and self._mood_gain > 0:
            try:
                self._store.update(mood=float(self._mood_gain))
            except Exception:
                _log.warning("[番茄] 心情增益异常", exc_info=True)

    def _refresh_dnd(self) -> None:
        if self._dnd_fn is not None:
            try:
                self._dnd = bool(self._dnd_fn())
            except Exception:
                self._dnd = True   # fail-closed：探测异常按 DND 静默

    def _fsm_event(self, event: str) -> None:
        if self._fsm is None:
            return
        # 全屏中到点：只气泡，不发 FSM 事件切姿态（§4.1 安全边界）
        if self._fullscreen_fn is not None:
            try:
                if self._fullscreen_fn():
                    return
            except Exception:
                pass
        try:
            self._fsm(event)
        except Exception:
            _log.warning("[番茄] fsm 事件 %s 派发异常", event, exc_info=True)

    def _emit(self, msg: str) -> None:
        if self._dnd:
            return   # DND 只静默提醒（气泡+音效），节律照走
        if self._bubble is not None:
            try:
                self._bubble(msg)
            except Exception:
                _log.warning("[番茄] 气泡异常", exc_info=True)
        if self._sound is not None:
            try:
                self._sound()
            except Exception:
                _log.warning("[番茄] 音效异常", exc_info=True)

    # ---- 内部：休息执法（0.25.4 启用，默认关） ----

    def _reset_break_window(self) -> None:
        self._rest_started_at = self._now() if self.active else None
        self._rest_activity_s = 0.0
        self._last_break_tick = self._now()
        self._rest_cut_count = 0
        self._break_bubbled = False

    def _tick_break_enforce(self, now: float) -> None:
        if (not self._break_enforce
                or self._state not in (FocusPhase.SHORT, FocusPhase.LONG)
                or self._idle is None
                or self._rest_started_at is None
                or now - self._rest_started_at > _BREAK_GRACE_S):
            return
        try:
            active = float(self._idle()) < _BREAK_ACTIVE_IDLE_S
        except Exception:
            active = False
        dt = max(0.0, now - self._last_break_tick)
        self._last_break_tick = now
        if active:
            self._rest_activity_s += dt
        # 每累计 10s 记一次打断；连续 2 次（累计 ≥20s）才温和气泡（§9）
        cuts = int(self._rest_activity_s // _BREAK_ACTIVITY_THRESHOLD_S)
        if cuts > self._rest_cut_count:
            self._stats.breaks_cut += cuts - self._rest_cut_count
            self._rest_cut_count = cuts
            self._persist()
            if cuts >= 2 and not self._break_bubbled:
                self._break_bubbled = True
                self._emit(_BREAK_CUT)

    # ---- 内部：统计跨日 ----

    def _rollover(self, now: float) -> None:
        today = _today(now)
        if not today or self._stats.date == today:
            return
        # 跨日/首次：日计数归零；streak 按「昨日有完成」延续
        if self._stats.date == _yesterday(today) and self._stats.completed > 0:
            streak = self._stats.streak_days
        else:
            streak = 0
        self._stats = FocusStats(date=today, completed=0, focus_sec=0,
                                 breaks_cut=0, streak_days=streak)

    # ---- 内部：持久化（focus_state.json 原子写，proactive.py:307 同款） ----

    def _persist(self) -> None:
        if not self._state_path:
            return
        try:
            phase = None if self._state == FocusPhase.IDLE else self._state.value
            data = {
                "version": 1,
                "stats": {
                    "date": self._stats.date,
                    "completed": self._stats.completed,
                    "focus_sec": self._stats.focus_sec,
                    "breaks_cut": self._stats.breaks_cut,
                    "streak_days": self._stats.streak_days,
                },
                "phase": phase,
                "deadline": self._deadline if phase else None,
                "completed_in_cycle": self._completed_in_cycle if phase else 0,
            }
            tmp = self._state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._state_path)
        except (OSError, TypeError, ValueError):
            _log.warning("[番茄] 状态持久化失败", exc_info=True)

    def _load_persisted(self) -> None:
        if not self._state_path or not os.path.isfile(self._state_path):
            return
        try:
            with open(self._state_path, encoding="utf-8") as f:
                raw = json.load(f)
            if not isinstance(raw, dict):
                return
            s = raw.get("stats")
            if isinstance(s, dict):
                self._stats = FocusStats(
                    date=str(s.get("date") or ""),
                    completed=_int(s.get("completed"), 0, 10 ** 6),
                    focus_sec=_int(s.get("focus_sec"), 0, 10 ** 7),
                    breaks_cut=_int(s.get("breaks_cut"), 0, 10 ** 6),
                    streak_days=_int(s.get("streak_days"), 0, 10 ** 4),
                )
            now = self._now()
            self._rollover(now)
            phase = raw.get("phase")
            deadline = raw.get("deadline")
            if (phase in ("focus", "short", "long")
                    and isinstance(deadline, (int, float))
                    and deadline > now - _RESUME_GRACE_S
                    and deadline < now + _RESUME_MAX_FORWARD_S
                    and _same_day(float(deadline), now)):
                self._state = FocusPhase(phase)
                self._deadline = float(deadline)
                self._completed_in_cycle = _int(
                    raw.get("completed_in_cycle"), 0, self._cycles)
                _log.info("[番茄] 恢复进行中节律（%s）", phase)
            else:
                self._state = FocusPhase.IDLE
                self._deadline = None
                self._completed_in_cycle = 0
        except (OSError, ValueError):
            _log.warning("[番茄] 状态档案损坏，忽略", exc_info=True)


def _same_day(deadline: float, now: float) -> bool:
    try:
        return datetime.fromtimestamp(deadline).date().isoformat() == _today(now)
    except (OSError, OverflowError, ValueError):
        return False


# ---- LLM 工具（0.25.2：start_pomodoro/stop_pomodoro，无危险级不打扰） ----

class _StartPomodoroHandler:
    def __init__(self, session) -> None:
        self._session = session

    def execute(self, args: dict, ctx):
        from pet.tools_schema import ToolResult  # 延迟避环 import

        if self._session is None:
            return ToolResult(False, "番茄功能未启用（config focus.enabled）", {})
        minutes = args.get("minutes")
        self._session.start(minutes)
        return ToolResult(True, "已开始番茄钟",
                          {"state": self._session.state.value,
                           "active": self._session.active})


class _StopPomodoroHandler:
    def __init__(self, session) -> None:
        self._session = session

    def execute(self, args: dict, ctx):
        from pet.tools_schema import ToolResult

        if self._session is None:
            return ToolResult(False, "番茄功能未启用（config focus.enabled）", {})
        self._session.stop()
        return ToolResult(True, "已停止番茄钟",
                          {"state": self._session.state.value})


def build_focus_tools(session) -> list:
    """番茄工具清单（app 喂 ToolRegistry.register，与天气/记忆工具并列）。"""
    from pet.tools_schema import ToolSchema

    start_schema = ToolSchema(
        name="start_pomodoro",
        description=(
            "开始一个番茄钟专注段。可选参数 minutes 指定专注时长（分钟，"
            "1–180，默认按配置 25 分钟）；已运行时重新开始计时。"
        ),
        parameters={
            "type": "object",
            "properties": {"minutes": {"type": "number", "minimum": 1,
                                      "maximum": 180}},
            "additionalProperties": False,
        },
    )
    stop_schema = ToolSchema(
        name="stop_pomodoro",
        description="停止/取消当前番茄钟（回到空闲，当前番茄不计完成）。",
        parameters={"type": "object", "properties": {},
                    "additionalProperties": False},
    )
    return [(start_schema, _StartPomodoroHandler(session)),
            (stop_schema, _StopPomodoroHandler(session))]
