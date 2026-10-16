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

v0.27.0「番茄自由度扩展」模型层①（wiki 设计-番茄自由度扩展技术方案 §4.1/
§4.2/§5.2）——四件模型层能力，入口接线归 v0.27.1：

* **L3 记账缺陷修复（正确性，本版前置）**：``focus_sec`` 由"按配置
  ``_work_min`` 记账"改为**按实耗**（``_enter()`` 记 ``_phase_started_at``，
  收口按 ``min(now, _deadline) - _phase_started_at`` 结算，见
  ``_focus_elapsed``）。**pomodoro 未延长时与旧口径逐值相等**、睡眠大跳仍
  不补算（L2），是让"延长/正计时"不失真的前置条件。
* ``start(minutes, *, as_default=False)``（修 L4/§9 Q6）：``minutes``
  **只影响这一次**，不再永久改写 ``_work_min``；改默认须显式
  ``as_default=True``。
* ``extend(minutes=5)``：非 IDLE 推移绝对时刻 ``_deadline``（L2 天然支持），
  延长部分靠实耗记账自动计入（§9 Q3）；IDLE no-op。
* ``complete()``：提前完成——走 ``_on_focus_complete()`` 进休息且**计入**
  ``completed``/``streak``，与 ``stop()``（不计）明确对照；IDLE/休息段 no-op。

未实装（按 §3 分期留后续）：暂停继续（v0.27.3，故本文件**无 pause()**）、
正计时模式（v0.27.4）、无休息（v0.27.5）、统计历史（v0.27.6）。

v0.27.2「入口层② + 配置面」（wiki 设计-番茄自由度扩展技术方案 §4.1 A /
§4.4 D1/D3）——三件配置面能力（入口接线在 pet/tray.py + app.py）：

* ``_apply_cfg(cfg)``：把 ``__init__`` 里的 ``_num/_int`` 解析抽成一个方法，
  ``__init__`` 与 ``reconfigure(cfg)`` 共用（单一来源，杜绝两处漂移）。
* ``reconfigure(cfg)``（D3 热更新）：只改配置派生字段（时长/轮数/执法/心情
  增益/预设/上次时长），**绝不碰当前段**（``_deadline``/``_phase_started_at``/
  ``_state``/``_completed_in_cycle``/``_stats`` 原样）——进行中的段不被打断、
  剩余时间不变，新值只在下一次 ``_enter`` 生效。
* ``start_preset(preset)``（D1 命名预设）：``_run`` 本轮覆盖（work/short/long/
  cycles **只作用于本轮**，``_enter_idle()`` 时清除），**不改** ``_work_min``
  等全局默认；``start(minutes, as_default=)`` 语义零改动。
* ``last_work_min``：只读属性（上次使用的专注时长；无历史 = 当前默认），
  app 侧检测到变化时落 config ``focus.last_work_min``（"记住上次"）。
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


def _parse_minutes(minutes) -> float | None:
    """外部传入的分钟数 → [1,180] 的 float；"没传/非法/越下界" 一律 None。

    沿用旧 ``start(minutes)`` 的容错口径（避免行为漂移）：``< 1``、非数值、
    NaN 当作"没传"（用当前默认值），``> 180`` 钳制而非忽略。``None`` 入参
    同样返回 None（区分"没传"与"传了非法值"对调用方无意义，两者同解）。
    """
    if minutes is None:
        return None
    try:
        m = float(minutes)
    except (TypeError, ValueError):
        return None
    if not (m >= 1.0):        # NaN 走此分支（NaN 与任何数比较均 False）
        return None
    return min(180.0, m)


def _opt_min(v, lo: float, hi: float) -> float | None:
    """**可选**分钟值 → [lo,hi] 的 float；缺省/非法/越下界一律 ``None``。

    与 ``_parse_minutes`` 的区别：``None`` 是"这个键没给 / 给了也不可用"的
    显式信号（调用方回落全局值），而不是"当作没传 → 用默认值"。预设的
    short/long_min 可缺省（§4.4 D1），必须能与"给了 1.0"区分开。
    """
    if v is None:
        return None
    try:
        m = float(v)
    except (TypeError, ValueError):
        return None
    if not (m >= lo):         # NaN 走此分支
        return None
    return min(hi, m)


def _opt_int(v, lo: int, hi: int) -> int | None:
    """**可选**整数值 → [lo,hi] 的 int；缺省/非法/越下界一律 ``None``。

    ``bool`` 显式排除（``True`` 会被 ``int()`` 收成 1，但配置里 ``true`` 显然
    不是"1 轮"的意思，按缺省处理更诚实）。
    """
    if v is None or isinstance(v, bool):
        return None
    try:
        i = int(v)
    except (TypeError, ValueError):
        return None
    if i < lo:
        return None
    return min(hi, i)


def _preset_overrides(preset: dict) -> dict:
    """预设 dict → 本轮覆盖字典（只收录**合法且显式给出**的键）。

    单一来源：``start_preset`` 与 ``_parse_presets`` 共用，杜绝两处钳制口径
    漂移。缺失/非法/越界的键不进字典 → 消费端回落全局值（§4.4 D1 的宽容口径）。
    """
    out: dict = {}
    w = _opt_min(preset.get("work_min"), 1.0, 180.0)
    if w is not None:
        out["work_min"] = w
    s = _opt_min(preset.get("short_min"), 1.0, 60.0)
    if s is not None:
        out["short_min"] = s
    lo = _opt_min(preset.get("long_min"), 1.0, 120.0)
    if lo is not None:
        out["long_min"] = lo
    cyc = _opt_int(preset.get("cycles"), 1, 12)
    if cyc is not None:
        out["cycles"] = cyc
    return out


def _parse_presets(v) -> list:
    """``config.focus.presets`` → 归一化预设清单（宽容口径，坏项跳过不抛）。

    schema 只 required ``name``+``work_min``（short/long/cycles 可缺省，消费端
    回落全局值）；此处再防御一次：非 list/非 dict 项/空名一律丢弃，数值越界按
    ``_preset_overrides`` 钳制——**绝不因一个坏预设让整个功能抛异常或不可用**。
    """
    if not isinstance(v, (list, tuple)):
        return []
    out = []
    for item in v:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        p = {"name": name}
        p.update(_preset_overrides(item))
        out.append(p)
    return out


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
        self._apply_cfg(c)

        self._state = FocusPhase.IDLE
        self._deadline: float | None = None
        self._phase_started_at: float | None = None   # 本相位起点（L3 实耗记账）
        self._completed_in_cycle = 0     # 本轮已完成番茄数（决定 SHORT/LONG）
        self._run: dict | None = None    # v0.27.2 本轮预设覆盖（IDLE 时清除）
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

    # ---- 配置：解析 / 热重载（v0.27.2 §4.4 D3） ----

    def _apply_cfg(self, cfg) -> None:
        """``config.focus`` 段 → 节律参数（``__init__`` 与 ``reconfigure`` 共用）。

        只写**配置派生**字段（时长/轮数/执法开关/心情增益/预设/上次时长），
        **不碰任何运行时状态**（``_state``/``_deadline``/``_phase_started_at``/
        ``_completed_in_cycle``/``_stats``/``_run``）——正因如此，``reconfigure``
        天然满足 §4.4 D3「只影响下一次 ``_enter``，绝不动当前段 ``_deadline``」。
        非 dict 入参按"空配置"处理（全默认），不抛。
        """
        c = cfg if isinstance(cfg, dict) else {}
        self._work_min = _num(c.get("work_min", _DEFAULT_WORK_MIN), 1.0, 180.0)
        self._short_min = _num(c.get("short_min", _DEFAULT_SHORT_MIN), 1.0, 60.0)
        self._long_min = _num(c.get("long_min", _DEFAULT_LONG_MIN), 1.0, 120.0)
        self._cycles = _int(c.get("cycles", _DEFAULT_CYCLES), 1, 12)
        self._break_enforce = bool(c.get("break_enforce", False))
        self._mood_gain = _num(c.get("mood_gain", _DEFAULT_MOOD_GAIN), 0.0, 20.0)
        self._presets = _parse_presets(c.get("presets"))
        # last_work_min：0 = 无历史 → 回落当前默认时长（故属性恒 > 0，托盘文案
        # 总有意义）。本次会话已用过某时长（prev > 0）时以内存值为准：热重载
        # 不该把刚"记住"的时长冲回文件里的 0（D3 重载不丢运行时状态）。
        prev = getattr(self, "_last_work_min", 0.0)
        disk = _num(c.get("last_work_min", 0.0), 0.0, 180.0)
        self._last_work_min = disk if disk > 0 else (
            prev if prev > 0 else self._work_min)

    def reconfigure(self, cfg) -> None:
        """热重载 ``config.focus``（v0.27.2 §4.4 D3）——只影响**下一次** ``_enter``。

        与 ``__init__`` 共用 ``_apply_cfg``：改 ``_work_min``/``_short_min``/
        ``_long_min``/``_cycles``/``_break_enforce``/``_mood_gain``/预设/上次时长。
        **绝不碰当前段**：``_deadline``/``_phase_started_at``/``_state``/
        ``_completed_in_cycle``/``_stats`` 全部原样，进行中的段不被打断、剩余时间
        逐值不变；新值在下一段 ``_enter`` 时自然生效（本轮预设覆盖 ``_run`` 也不清）。

        触发路径＝托盘「🔄 重载番茄配置」（用户显式触发），**不做**全局 config
        watcher（§4.4 D3）。非 dict 入参 no-op（不把整段配置"改成空"）。
        """
        if not isinstance(cfg, dict):
            return
        self._apply_cfg(cfg)

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

    @property
    def last_work_min(self) -> float:
        """上次使用的专注时长（分钟，只读；无历史 = 当前默认，见 ``_apply_cfg``）。

        app 侧据此检测变化并落 config ``focus.last_work_min``（"记住上次"），
        托盘「🕘 上次使用 N 分钟」/「⭐ 把 N 分钟设为默认」也读它。
        """
        return self._last_work_min

    @property
    def presets(self) -> list:
        """命名预设清单（只读副本；config ``focus.presets`` 归一化后）。"""
        return [dict(p) for p in self._presets]

    # ---- 本轮有效时长（预设覆盖优先，否则全局默认） ----

    def _work_minutes(self) -> float:
        if self._run is not None and "work_min" in self._run:
            return float(self._run["work_min"])
        return self._work_min

    def _short_minutes(self) -> float:
        if self._run is not None and "short_min" in self._run:
            return float(self._run["short_min"])
        return self._short_min

    def _long_minutes(self) -> float:
        if self._run is not None and "long_min" in self._run:
            return float(self._run["long_min"])
        return self._long_min

    def _cycle_target(self) -> int:
        if self._run is not None and "cycles" in self._run:
            return int(self._run["cycles"])
        return self._cycles

    # ---- 用户操作 ----

    def start(self, minutes: float | int | None = None, *,
              as_default: bool = False) -> None:
        """IDLE→FOCUS；已运行则改期重启（显式 start = 新一轮，cycle 归零）。

        ``minutes``（1–180 分钟；``<1``/非数值/NaN 一律"当作没传"、``>180``
        钳到 180，沿用旧口径）**只影响这一次**——不改 ``self._work_min``。
        这是修 L4（§9 Q6）：旧版 ``start(minutes)`` 会永久改写默认，用户以为
        "只这一次"其实后续所有轮次都变了。要把本次时长写成后续默认，须显式
        ``as_default=True``（托盘「设为默认」走此路；落 config
        ``focus.last_work_min`` 归 v0.27.2）。

        本次实际时长只用于 ``_enter`` 的 deadline；下一个专注段回到
        ``_work_min``（除非 ``as_default=True`` 改了它）。

        v0.27.2：显式 ``start`` = **新一轮**，故一并清掉本轮预设覆盖 ``_run``
        （``start_preset`` 装的那种）；同时把本次有效时长记进 ``last_work_min``
        （"记住上次"，app 检测到变化即落 config）。
        """
        now = self._now()
        self._rollover(now)
        one_shot = _parse_minutes(minutes)
        self._run = None
        if as_default and one_shot is not None:
            self._work_min = one_shot
        self._completed_in_cycle = 0
        effective = one_shot if one_shot is not None else self._work_min
        self._last_work_min = effective
        self._enter(FocusPhase.FOCUS, effective, now)

    def start_preset(self, preset) -> None:
        """以**命名预设**启动本轮（v0.27.2 §4.4 D1）：本轮覆盖，不动全局默认。

        与 ``start()`` 的分工：``start()`` 清掉本轮覆盖、回到全局默认（或用
        一次性 ``minutes``）；``start_preset()`` 装上一组**本轮**覆盖
        （``self._run``，仅收录预设里显式给出的键，缺省者回落全局值），到
        ``_enter_idle()``（``stop()`` / 长休自然收尾 / 提前完成进休息后本轮结束）
        自动清除。**绝不改** ``_work_min``/``_short_min``/``_long_min``/
        ``_cycles`` 这些全局默认——这正是"预设只作用于本轮"的口径。

        非法预设（非 dict / ``name`` 为空）→ no-op（不启动：点了没反应好过乱开
        一轮）；合法但 ``work_min`` 缺省/非法 → 本轮按全局默认时长启动。
        """
        if not isinstance(preset, dict):
            return
        if not str(preset.get("name") or "").strip():
            return
        now = self._now()
        self._rollover(now)
        self._run = _preset_overrides(preset)
        self._completed_in_cycle = 0
        effective = self._work_minutes()
        self._last_work_min = effective
        self._enter(FocusPhase.FOCUS, effective, now)

    def stop(self) -> None:
        """任意态→IDLE，释放全部接管（当前番茄不计完成）。幂等。"""
        if self._state == FocusPhase.IDLE:
            return
        self._enter_idle(self._now())

    def extend(self, minutes: float | int | None = 5) -> None:
        """延长当前段（默认 +5 分钟）。**IDLE 时 no-op**（L1 零副作用）。

        只推移绝对时刻 ``_deadline``（L2 epoch-deadline 天然支持；延长期间
        "系统睡眠不补算"的初衷不变），不改 ``_work_min``/``_completed_in_cycle``
        /state。延长部分靠 L3 实耗记账自动计入 ``focus_sec``——无需新字段
        （§9 Q3：延长后记实耗含延长部分）。``minutes`` 单次调用钳到 [1,180]，
        非法/非正/NaN 一律 no-op（**不做缩短**；要提前结束用 ``complete()``/
        ``stop()``）。延长后立即落盘：``deadline`` 是 v1 档案字段，不落盘则
        重启续算会丢延长。
        """
        if self._state == FocusPhase.IDLE or self._deadline is None:
            return
        m = _parse_minutes(minutes)
        if m is None:
            return
        self._deadline += m * 60.0
        self._persist()

    def complete(self) -> None:
        """提前完成当前专注段：**计入** ``completed``/``streak`` 后进休息。

        与 ``stop()`` 形成明确对照（§4.2 B3）：``complete()`` 走
        ``_on_focus_complete()``——完成数/连续天数/``focus_sec`` 实耗/养成
        mood 联动全走；``stop()`` 一概不计。休息段（SHORT/LONG）与 IDLE 下
        no-op：休息没有"提前完成"语义（提前结束休息用 ``skip()``），且若在
        休息段计完成会凭空 +1 番茄。``focus_sec`` 按实耗（L3）：此处 deadline
        未到，故记 ``now - _phase_started_at`` 的真实秒数（不封顶）。
        """
        if self._state != FocusPhase.FOCUS:
            return
        self._finish_focus(self._now())

    def skip(self) -> None:
        """跳过当前段进下一段。专注段 skip 不计完成直接进短休；短休 skip
        进 FOCUS；长休 skip 进 FOCUS 且新一轮（cycle 归零）。休息 skip 不计打断。

        **IDLE 时 no-op**（L1）。专注段 skip 既然不计完成，就**不记
        ``focus_sec``**——实耗记账只发生在 ``_finish_focus``（自然到期 /
        ``complete()``）这一处收口，skip 不经过它（诚实口径：跳过的那段不算
        专注）。
        """
        if self._state == FocusPhase.IDLE:
            return
        now = self._now()
        phase = self._state
        if phase == FocusPhase.FOCUS:
            self._emit(_SKIP_FOCUS)
            self._enter(FocusPhase.SHORT, self._short_minutes(), now)
        elif phase == FocusPhase.SHORT:
            self._emit(_SKIP_REST)
            self._enter(FocusPhase.FOCUS, self._work_minutes(), now)
        else:  # LONG
            self._emit(_SKIP_REST)
            self._completed_in_cycle = 0
            self._enter(FocusPhase.FOCUS, self._work_minutes(), now)

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
            self._finish_focus(now)
        elif phase == FocusPhase.SHORT:
            self._emit(_REST_DONE)
            self._enter(FocusPhase.FOCUS, self._work_minutes(), now)
        else:  # LONG 自然收尾 → 本轮结束回 IDLE（§3 FOCUS/IDLE 两分支之 IDLE）
            self._emit(_REST_DONE)
            self._enter_idle(now)

    def _finish_focus(self, now: float) -> None:
        """专注段收口——自然到期（``_advance``）与 ``complete()`` 提前完成
        共用这一条路径，保证两条入口的记账/气泡/cycle 推进逐字一致。"""
        self._on_focus_complete(now)
        self._emit(_FOCUS_DONE)
        self._completed_in_cycle += 1
        if self._completed_in_cycle >= self._cycle_target():
            self._enter(FocusPhase.LONG, self._long_minutes(), now)
        else:
            self._enter(FocusPhase.SHORT, self._short_minutes(), now)

    def _enter(self, phase: FocusPhase, minutes: float, now: float) -> None:
        self._state = phase
        self._deadline = now + float(minutes) * 60.0
        self._phase_started_at = now      # L3：实耗记账的相位起点
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
        self._phase_started_at = None
        self._completed_in_cycle = 0
        self._run = None          # v0.27.2：本轮预设覆盖到 IDLE 即清除（D1）
        self._reset_break_window()
        self._fsm_event("focus_rest_off")
        self._persist()

    def _on_focus_complete(self, now: float) -> None:
        self._rollover(now)
        if self._stats.completed == 0:
            # 今日首次完成：streak +1（昨日有完成则续，否则从 1 起算）
            self._stats.streak_days += 1
        self._stats.completed += 1
        self._stats.focus_sec += int(round(self._focus_elapsed(now)))
        # 养成联动（0.25.5）：完成番茄 → 心情 +mood_gain（store 未注入静默）
        if self._store is not None and self._mood_gain > 0:
            try:
                self._store.update(mood=float(self._mood_gain))
            except Exception:
                _log.warning("[番茄] 心情增益异常", exc_info=True)

    def _focus_elapsed(self, now: float) -> float:
        """本专注段实耗秒（L3：按实耗记账，不再按 ``_work_min`` 配置值）。

        起点 ``_phase_started_at`` 由 ``_enter()`` 在进入该相位那一刻写入。
        到期收口时**以 ``_deadline`` 封顶**，这不是可选优化：
        ① L2 的初衷是"系统睡眠过期即迁移、**不补算**"——不封顶则睡 2 小时
           醒来的第一个 tick 会把 2 小时记进 ``focus_sec``（旧口径恒记 25min）；
        ② 「pomodoro 未延长时与旧口径逐值相等」也只有封顶才成立：tick 有拍长，
           实际 ``now`` 通常略晚于 ``deadline``（既有单测故意 +1s，真实 app 每拍
           数十毫秒），不封顶会记成 1501/1500+ε。
        延长过的段 ``_deadline`` 已后移，故延长部分照记进实耗（§9 Q3）。

        重启续算：v1 档案只有 ``deadline``（无相位起点字段），
        ``_load_persisted`` 按「``deadline`` − 本相位时长」反推
        ``_phase_started_at``——未延长的段重启后口径仍逐值等价；**"延长后
        重启"** 的段只能记回配置时长（延长量在 v1 无字段可存，待 v0.27.3
        schema v2 一并携带；进程内照记不误，故非静默失真）。
        """
        started = self._phase_started_at
        if started is None:
            # 兜底（正常路径不会到这）：沿用旧口径，绝不因缺起点而丢记账
            return max(0.0, self._work_minutes() * 60.0)
        elapsed = now - started
        if self._deadline is not None:
            elapsed = min(elapsed, self._deadline - started)
        return max(0.0, elapsed)

    def _phase_minutes(self, phase: FocusPhase) -> float:
        """该相位的**本轮有效**时长（分钟）——仅供重启反推相位起点用。

        v0.27.2：走 ``_work_minutes/_short_minutes/_long_minutes``，预设本轮
        覆盖同样生效（重启后 ``_run`` 不恢复，见 ``_load_persisted`` 注释）。
        """
        if phase == FocusPhase.SHORT:
            return self._short_minutes()
        if phase == FocusPhase.LONG:
            return self._long_minutes()
        return self._work_minutes()

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

    def _emit(self, msg) -> None:
        """气泡 + 音效；DND 只静默提醒（气泡+音效），节律照走。

        ``msg`` 可为文案池元组（_FOCUS_START/_FOCUS_DONE/_REST_DONE/_
        BREAK_CUT 均为纯数据元组）——0.25.4 修：旧版把整个元组直接交给
        ``bubble_fn``，而 app ``_auto_bubble``/``BubbleManager.show`` 契约是
        ``str``，实际会显示成 repr 或抛异常；此处按池随机取一条保证恒 str。
        """
        if self._dnd:
            return
        if isinstance(msg, (tuple, list)):
            msg = random.choice(tuple(msg)) if msg else ""
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
                # L3 实耗记账：v1 档案不存相位起点，按「deadline − 本相位时长」
                # 反推。未延长的段由此与旧口径逐值等价（见 _focus_elapsed）。
                self._phase_started_at = (
                    self._deadline - self._phase_minutes(self._state) * 60.0)
                self._completed_in_cycle = _int(
                    raw.get("completed_in_cycle"), 0, self._cycles)
                _log.info("[番茄] 恢复进行中节律（%s）", phase)
                # 0.25.5 重启续算：恢复成 FOCUS 中 → 补发陪伴姿态事件。构造期
                # 没有「进入专注段」这一刻，不补发则重启后宠物不趴下（节律/
                # 托盘/门禁都对，只有姿态漏）。_fsm_event 内含全屏守卫与异常
                # 兜底，未注入 fsm_event_fn（纯逻辑测试）时为 no-op。
                if self._state is FocusPhase.FOCUS:
                    self._fsm_event("focus_rest_on")
            else:
                self._state = FocusPhase.IDLE
                self._deadline = None
                self._phase_started_at = None
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


class _SkipPomodoroHandler:
    def __init__(self, session) -> None:
        self._session = session

    def execute(self, args: dict, ctx):
        from pet.tools_schema import ToolResult

        if self._session is None:
            return ToolResult(False, "番茄功能未启用（config focus.enabled）", {})
        # L1：IDLE 下**可读说明**而非静默成功/静默失败——LLM 需要知道"没在
        # 番茄中"才能正确回话；模型侧 skip() 本就 no-op，这里只是把结果讲清楚
        if not self._session.active:
            return ToolResult(True, "当前未在番茄中（空闲），无需跳过",
                              {"state": self._session.state.value,
                               "active": False})
        self._session.skip()
        return ToolResult(True, "已跳过当前段，进入下一段（本次不计完成）",
                          {"state": self._session.state.value,
                           "active": self._session.active})


class _ExtendPomodoroHandler:
    def __init__(self, session) -> None:
        self._session = session

    def execute(self, args: dict, ctx):
        from pet.tools_schema import ToolResult

        if self._session is None:
            return ToolResult(False, "番茄功能未启用（config focus.enabled）", {})
        if not self._session.active:
            return ToolResult(True, "当前未在番茄中（空闲），延长无效果",
                              {"state": self._session.state.value,
                               "active": False})
        # 钳制口径与 start_pomodoro 完全一致（复用 _parse_minutes）：>180 钳到
        # 180，<1/非数值/NaN/缺省 → 按默认 5 分钟（extend() 的默认值）
        m = _parse_minutes(args.get("minutes", 5))
        if m is None:
            m = 5.0
        self._session.extend(m)
        return ToolResult(True, f"已延长当前段 {m:g} 分钟（只延不缩）",
                          {"state": self._session.state.value,
                           "extended_minutes": m})


def build_focus_tools(session) -> list:
    """番茄工具清单（app 喂 ToolRegistry.register，与天气/记忆工具并列）。"""
    from pet.tools_schema import ToolSchema

    start_schema = ToolSchema(
        name="start_pomodoro",
        description=(
            "开始一个番茄钟专注段。可选参数 minutes 指定专注时长（分钟，"
            "1–180）；**只影响这一次**，不改变配置里的默认时长（下一轮回到"
            "默认值）；已运行时重新开始计时。"
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
    # v0.27.1 运行中控制（§4.2 B1/B2）：skip/extend 与 start/stop 同危险级
    # （ToolSchema.dangerous 默认 False = 无危险级不打扰，不经 confirm_fn）
    skip_schema = ToolSchema(
        name="skip_pomodoro",
        description=(
            "跳过当前番茄段、直接进入下一段（专注段跳过**不计完成**：既不记"
            "完成数/连续天数，也不记专注时长）。专注段跳过 → 短休息；短休跳过"
            "→ 专注；长休跳过 → 专注并开始新一轮。空闲时无效果（会如实说明）。"
        ),
        parameters={"type": "object", "properties": {},
                    "additionalProperties": False},
    )
    extend_schema = ToolSchema(
        name="extend_pomodoro",
        description=(
            "延长当前番茄段（**只延不缩**，默认多给 5 分钟）。可选参数 minutes"
            " 指定延长分钟数（1–180，与 start_pomodoro 同口径：>180 钳到 180、"
            "<1/非法当作没传→按 5 分钟）；延长部分计入实际专注时长。空闲时无"
            "效果（会如实说明）；要提前收工请用 stop_pomodoro（不计完成）。"
        ),
        parameters={
            "type": "object",
            "properties": {"minutes": {"type": "number", "minimum": 1,
                                      "maximum": 180}},
            "additionalProperties": False,
        },
    )
    return [(start_schema, _StartPomodoroHandler(session)),
            (stop_schema, _StopPomodoroHandler(session)),
            (skip_schema, _SkipPomodoroHandler(session)),
            (extend_schema, _ExtendPomodoroHandler(session))]
