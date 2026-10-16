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

未实装（按 §3 分期留后续）：正计时模式（v0.27.4）、无休息（v0.27.5）、
统计历史（v0.27.6）。

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

v0.27.3「番茄自由度扩展·模型层②（结构性）：暂停/继续」（wiki 设计-番茄自由度
扩展技术方案 §5.1/§5.2，本线最高风险版：结构性 + ``focus_state.json`` schema v2）：

* **剩余量语义**（§5.1）：L2 是"绝对时刻 deadline"，暂停不能靠停表实现，故新增
  ``_paused_at``（暂停时刻，None=未暂停）与 ``_remaining_s``（暂停时冻结的剩余
  秒）。``pause()`` 把 ``_deadline - now`` 冻结进 ``_remaining_s`` 后置
  ``_deadline = None``；``resume()`` 按 ``now + _remaining_s`` 重算绝对时刻，并
  **回填已消耗部分** ``_phase_started_at = now - (相位时长 - 剩余)``——不这样
  resume 后 L3 实耗会把"暂停前已专注的那段"丢掉（§5.2 坑 3）。
* ``tick()`` **最前置早退**（紧跟 ``_refresh_dnd()``、在 IDLE 判断之前）：暂停态
  **睡多久都不迁移、不补算**（L2 初衷在暂停态天然满足）。
* **Q1 姿态语义**：暂停期间 ``active`` **仍为 True**（严格 == state != IDLE，L1
  不被削弱；门禁久坐/吃鼠标/纠缠继续静默——"我还在专注，只是暂时离开"），但
  ``pause()`` 发 ``focus_rest_off``（姿态回 idle，一眼可辨"已暂停"）、``resume()``
  在 FOCUS 段发 ``focus_rest_on``（重新趴下）。**暂停时长不计入 ``focus_sec``**。
* **Q2 隐式继续守卫**：``complete()`` 在暂停态**先** ``resume()`` 再
  ``_finish_focus()``——不 resume 则 ``_deadline=None`` 不封顶，暂停时长会被算进
  ``focus_sec``。``skip()``/``stop()``/自然迁移**不特判**：``_enter()`` 与
  ``_enter_idle()`` 各自清 ``_paused_at``/``_remaining_s``，离开暂停态自动发生。
* **schema v2**（§5.2 三坑）：``_persist()`` 写 ``version: 2`` + ``paused`` +
  ``remaining_s`` + ``phase_started_at``；loader **显式**加暂停分支（loader 不读
  ``version``，不能指望版本号分流——坑 1），在 ``phase`` 判定**之前**处理，且
  **不做 deadline 三重校验**（暂停态 ``_deadline=None`` 必然通不过 → 会静默落回
  IDLE 让暂停的番茄消失——坑 2）。语义：**暂停跨重启仍然暂停、剩余时间不变**
  （app 关多久都算暂停）。非暂停路径**优先**用档案里的 ``phase_started_at``（存在
  且合理时），否则回退旧的反推公式——于是「**延长后重启**」也保真（v1 无该字段，
  只能记回配置时长）。**v1 档案必须仍能加载**：无 ``paused`` 字段 → 视为未暂停，
  走原路径。

v0.27.6「番茄自由度扩展·**统计面**」（wiki 设计-番茄自由度扩展技术方案 §4.5 E
+ 裁决 Q5）——四件统计能力，**纯数据面**，不引入任何配置项：

* **逐日历史**（``_history``，随 ``focus_state.json`` 落盘，schema v2 → v3）：
  ``_rollover()`` 在把当日聚合清零**之前**，先把**刚结束的那一天**推进历史
  （``_archive_day``；同日重复调用**覆盖不追加**，故幂等）。上限
  ``_HISTORY_MAX_DAYS`` = 400 天，超限丢最旧（§4.5 E）。**只记有活动的天**：
  ``date`` 为空、或当日 ``completed``/``focus_sec`` 全为 0 时**不记录**——空转日
  不该占历史额度，也不该让"连续天数"看起来有数据。
* ``stats_range(rng)``：按 ``"today"``/``"week"``（近 7 天含今日）/``"month"``
  （近 30 天）聚合；未知值一律按 ``today``（宽容不抛）。**只读**——不改 phase/
  deadline/``_stats``/``_history``（L1）。今日一律以 ``self._stats`` 为准（历史里
  若已有今日那条也**不许算两遍**），历史按 ``date`` 去重且容忍乱序/重复。
* 工具 ``get_focus_stats(range)``（第 7 个番茄工具，无危险级）：**任何相位（含
  IDLE）都可查、零副作用**，回一句可读摘要（今日/近 7 天/近 30 天）。
* 托盘 tooltip 摘要（``TrayManager.set_focus_stats_summary`` + 纯函数
  ``format_stats_brief``），由 app 在 ``_sync_focus_tray()`` 里与相位/暂停态/模式
  **正交**的一路独立同步。

**QML 面板图表本版不做**（§8 占位登记表里已有它，仍是占位）。
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

# v0.27.6（§4.5 E）：逐日历史上限（天）——超限丢最旧，防档案无限膨胀。
# 400 天 ≈ 一年多的日粒度数据，够"近 7/30 天"聚合与长期回看。
_HISTORY_MAX_DAYS = 400

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


# ---- 统计文案（v0.27.6 §4.5 E：工具回执 + 托盘 tooltip 共用，纯函数） ----

_STATS_RANGE_LABEL = {"today": "今日", "week": "近 7 天", "month": "近 30 天"}


def _stats_label(rng) -> str:
    """范围键 → 中文标签；未知值按 ``today``（与 ``stats_range`` 同宽容口径）。"""
    return _STATS_RANGE_LABEL.get(str(rng or "today"), "今日")


def _minutes_of(sec) -> int:
    """秒 → 分钟（四舍五入、非负；非法值 0）——统计文案只报到分钟。"""
    try:
        return max(0, int(round(float(sec) / 60.0)))
    except (TypeError, ValueError):
        return 0


def format_stats_summary(st: dict) -> str:
    """``stats_range()`` 结果 → 一句可读摘要（v0.27.6，工具回执用）。

    形如 ``今日：完成 3 个番茄 · 专注 75 分钟 · 休息打断 1 次 · 连续 5 天``；
    近 7/30 天再带「共 N 天、日均 X 个」。纯函数、入参缺失/非法一律当 0，
    **绝不抛**（工具回执不能因为一个坏字段变成异常）。
    """
    st = st if isinstance(st, dict) else {}
    key = str(st.get("range") or "today")
    parts = [
        f"{_stats_label(key)}：完成 {_int(st.get('completed'), 0, 10 ** 6)} 个番茄",
        f"专注 {_minutes_of(st.get('focus_sec'))} 分钟",
        f"休息打断 {_int(st.get('breaks_cut'), 0, 10 ** 6)} 次",
        f"连续 {_int(st.get('streak_days'), 0, 10 ** 4)} 天",
    ]
    if key != "today":
        days = max(1, _int(st.get("days"), 1, 400))
        parts.append(f"共 {days} 天、日均 {st.get('avg_completed', 0)} 个")
    return " · ".join(parts)


def format_stats_brief(st: dict) -> str:
    """``stats_range()`` 结果 → 托盘 tooltip 用的**短**摘要（v0.27.6）。

    形如 ``今日 3 个 · 专注 75 分钟 · 连续 5 天``（近 7/30 天只换首个标签）。
    比 ``format_stats_summary`` 少「打断次数」一段——悬停一眼可看，长了会被
    系统托盘截断。同样纯函数、不抛。
    """
    st = st if isinstance(st, dict) else {}
    key = str(st.get("range") or "today")
    return (f"{_stats_label(key)} {_int(st.get('completed'), 0, 10 ** 6)} 个 · "
            f"专注 {_minutes_of(st.get('focus_sec'))} 分钟 · "
            f"连续 {_int(st.get('streak_days'), 0, 10 ** 4)} 天")


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

        # v0.27.4：计时模式（配置派生，见 _apply_cfg）。此处先落默认值，使
        # ``_apply_cfg`` 内部读 ``self._mode`` 之类的派生逻辑不依赖调用顺序。
        # v0.27.5：休息节律（``rest_style``）同理。
        self._mode = "pomodoro"
        self._rest_style = "auto"
        # v0.27.6（§4.5 E）：逐日历史——**运行时状态，不由配置派生**，故必须
        # 在 ``_apply_cfg`` **之前**初始化（``_apply_cfg`` 只写配置派生字段，
        # 不得覆盖它）；真正的数据由 ``_load_persisted()`` 在构造末尾灌入。
        self._history: list[dict] = []

        c = cfg or {}
        self._apply_cfg(c)

        self._state = FocusPhase.IDLE
        self._deadline: float | None = None
        self._phase_started_at: float | None = None   # 本相位起点（L3 实耗记账）
        # v0.27.3 暂停/继续（§5.1 剩余量语义）：
        self._paused_at: float | None = None      # 暂停时刻；None=未暂停
        self._remaining_s: float | None = None    # 暂停时冻结的剩余秒
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
        # v0.27.4 计时模式（§4.3 C）：只认 "stopwatch"（大小写/空白宽容），其余
        # 一律 "pomodoro"。这是**默认零漂移**的守护——老配置无该键、值非法、
        # 大小写怪写法都落在 pomodoro，与 v0.27.3 行为逐值等价（Q4）。
        self._mode = ("stopwatch" if str(c.get("mode", "")).strip().lower()
                      == "stopwatch" else "pomodoro")
        # v0.27.5 休息节律（§4.4 D2）：只认 "none"（大小写/空白宽容），其余一律
        # "auto"——与 ``_mode`` 同一套**默认零漂移**守护（老配置无该键、值非法、
        # 怪写法都落回 "auto"，与 v0.27.4 行为逐值等价，Q4）。
        self._rest_style = ("none" if str(c.get("rest_style", "")).strip().lower()
                            == "none" else "auto")
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
        """== (state != IDLE)。上层互斥门禁统一用此（§1 不变量）。

        v0.27.3（Q1）：**暂停期间仍为 True**——``active`` 严格等于
        ``state != IDLE``，暂停只是"不改 state 的时间冻结"，L1 不被削弱；
        门禁（久坐/吃鼠标/纠缠）继续静默。
        """
        return self._state != FocusPhase.IDLE

    @property
    def paused(self) -> bool:
        """是否处于**暂停态**（只读；供托盘/工具/热键判定，v0.27.3）。

        严格 == ``_paused_at is not None``：暂停态 ``active`` 仍为 True，
        两个属性表达正交的两件事（跑没跑 / 冻没冻）。
        """
        return self._paused_at is not None

    @property
    def mode(self) -> str:
        """计时模式：``"pomodoro"``（默认，与旧行为逐值等价）或 ``"stopwatch"``。

        v0.27.4（§4.3 C）。配置派生（``_apply_cfg`` 解析 ``config.focus.mode``，
        ``reconfigure`` 热重载后即刻生效），供托盘文案/可用性与工具回执分流。
        """
        return self._mode

    @property
    def rest_style(self) -> str:
        """休息节律：``"auto"``（默认，专注→短休/长休）或 ``"none"``（无休息）。

        v0.27.5（§4.4 D2）。配置派生（``_apply_cfg`` 解析
        ``config.focus.rest_style``，``reconfigure`` 热重载后即刻生效），
        只影响**下一次** ``_finish_focus``/``skip`` 的落点，绝不动当前段。
        ``"none"`` 下 SHORT/LONG **永不出现**。
        """
        return self._rest_style

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

    @property
    def history(self) -> list:
        """逐日历史（**只读副本**；v0.27.6 §4.5 E，最旧在前、上限 400 条）。

        每条形如 ``{"date": "2026-01-02", "completed": 3, "focus_sec": 4500,
        "breaks_cut": 1}``；只含**有活动**的天（零活动日不记）。返回深拷贝的
        浅层副本（每条再 dict 一次），调用方改不动内部状态。
        """
        return [dict(r) for r in self._history if isinstance(r, dict)]

    def stats_range(self, rng: str = "today") -> dict:
        """按范围聚合统计（v0.27.6，**只读**，§4.5 E）。

        ``rng``：``"today"`` 当日 / ``"week"`` 近 7 天（含今日）/ ``"month"``
        近 30 天。未知值一律按 ``"today"``（宽容，不抛）。返回
        ``{"range","days","completed","focus_sec","breaks_cut","streak_days",
        "avg_completed"}``：``days`` = 该范围覆盖的**天数**（1/7/30，与有没有
        数据无关），``avg_completed`` = ``completed / days`` 保留 1 位小数。

        今日用 ``self._stats``，历史天用 ``self._history``——**历史里若已有今日
        那条，必须用 ``self._stats`` 覆盖它，不许把今日算两遍**（跨日归档当天
        恰好被归档、或外部塞了同日期记录时都会撞上）。历史按 ``date`` 去重
        （后写覆盖先写，容忍乱序/重复），未来日期与超出范围的日期一律不计。

        **零副作用**（L1）：不调 ``_rollover``、不改 phase/deadline/``_stats``/
        ``_history``（任何相位、含 IDLE 都可安全调用）。
        """
        key = str(rng or "").strip().lower()
        if key not in ("today", "week", "month"):
            key = "today"
        days = {"today": 1, "week": 7, "month": 30}[key]
        completed = _int(self._stats.completed, 0, 10 ** 6)
        focus_sec = _int(self._stats.focus_sec, 0, 10 ** 7)
        breaks_cut = _int(self._stats.breaks_cut, 0, 10 ** 6)
        today = _today(self._now())
        if days > 1 and today:
            # 历史按 date 去重（后写覆盖先写）；今日那条**丢弃**——它由
            # self._stats 提供，否则今日被算两遍。
            seen: dict[str, dict] = {}
            for rec in self._history:
                if not isinstance(rec, dict):
                    continue
                d = str(rec.get("date") or "")
                if d and d != today:
                    seen[d] = rec
            try:
                base = datetime.fromisoformat(today).date()
            except ValueError:
                base = None
            if base is not None:
                for d, rec in seen.items():
                    try:
                        diff = (base - datetime.fromisoformat(d).date()).days
                    except (TypeError, ValueError):
                        continue
                    # 今日 diff=0 已排除；未来天（diff<0）与范围外一律不计
                    if not 0 < diff < days:
                        continue
                    completed += _int(rec.get("completed"), 0, 10 ** 6)
                    focus_sec += _int(rec.get("focus_sec"), 0, 10 ** 7)
                    breaks_cut += _int(rec.get("breaks_cut"), 0, 10 ** 6)
        return {
            "range": key,
            "days": days,
            "completed": completed,
            "focus_sec": focus_sec,
            "breaks_cut": breaks_cut,
            "streak_days": _int(self._stats.streak_days, 0, 10 ** 4),
            "avg_completed": round(completed / days, 1),
        }

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

    def pause(self) -> None:
        """暂停当前段：冻结剩余量（§5.1，v0.27.3）。**IDLE 或已暂停 → no-op**（L1）。

        ``_deadline`` 是绝对 epoch，时间照走——故暂停不能靠"停表"，必须把剩余量
        摘出来存进 ``_remaining_s`` 并把 ``_deadline`` 置 None（``tick()`` 在暂停态
        最前置早退，故 None 不会被误判为"到期"）。

        Q1：``active`` **保持 True**（门禁继续静默），但发 ``focus_rest_off`` 让
        陪伴姿态回 idle——"我还在专注，只是暂时离开"，视觉上一眼可辨。非 IDLE 段
        （专注/短休/长休）都可暂停。立即落盘：不落盘则重启后暂停态丢失。

        v0.27.4：**正计时段没有 ``_deadline``**，旧代码在此 ``return`` 会让正计时
        用户按暂停毫无反应。现改为：有 deadline（pomodoro）冻结剩余量；无 deadline
        （stopwatch）把 ``_remaining_s`` 置 None 表示"无剩余量可冻结"，暂停靠
        ``_paused_at`` 表达（``tick()`` 最前置早退仍然生效，暂停期间不推进）。
        """
        if self._state == FocusPhase.IDLE or self._paused_at is not None:
            return
        now = self._now()
        if self._deadline is not None:            # pomodoro：冻结剩余量
            self._remaining_s = max(0.0, self._deadline - now)
            self._deadline = None
        else:                                     # stopwatch：无剩余量可冻结
            self._remaining_s = None
        self._paused_at = now
        self._fsm_event("focus_rest_off")     # Q1：姿态回 idle，一眼可辨"已暂停"
        self._persist()

    def resume(self) -> None:
        """继续：按冻结的剩余量重算绝对时刻（§5.1）。**未暂停 → no-op**。

        关键在 ``_phase_started_at`` 的**回填已消耗部分**（§5.2 坑 3）：v1 的
        "``_deadline`` − 相位时长"反推在暂停路径下会丢数——resume 后的 ``_deadline``
        是**重算**的，"暂停前已专注的那部分"照抄会被丢掉，L3 实耗随之少算。故写成
        ``now - (相位时长 − 剩余)``，于是收口
        ``min(now, _deadline) - _phase_started_at`` 仍正确，**暂停时长不计入**。
        FOCUS 段恢复时发 ``focus_rest_on``（重新趴下）；休息段不发。

        v0.27.4：正计时段 ``_remaining_s is None``（无剩余量可重算）→ 走
        **把相位起点整体后移暂停时长**这一支，于是
        ``now - _phase_started_at``（实耗）自动扣掉暂停段，而"暂停时长不计入
        正计时累计"这一口径与 pomodoro 逐字一致。
        """
        if self._paused_at is None:
            return
        now = self._now()
        if self._remaining_s is not None:         # pomodoro：重算 + 回填起点
            rem = self._remaining_s
            self._deadline = now + rem
            self._phase_started_at = now - (
                self._phase_minutes(self._state) * 60.0 - rem)
        elif self._phase_started_at is not None:  # stopwatch：起点后移暂停时长
            self._phase_started_at += (now - self._paused_at)
        self._remaining_s = None
        self._paused_at = None
        if self._state == FocusPhase.FOCUS:
            self._fsm_event("focus_rest_on")
        self._persist()

    def complete(self) -> None:
        """提前完成当前专注段：**计入** ``completed``/``streak`` 后进休息。

        与 ``stop()`` 形成明确对照（§4.2 B3）：``complete()`` 走
        ``_on_focus_complete()``——完成数/连续天数/``focus_sec`` 实耗/养成
        mood 联动全走；``stop()`` 一概不计。休息段（SHORT/LONG）与 IDLE 下
        no-op：休息没有"提前完成"语义（提前结束休息用 ``skip()``），且若在
        休息段计完成会凭空 +1 番茄。``focus_sec`` 按实耗（L3）：此处 deadline
        未到，故记 ``now - _phase_started_at`` 的真实秒数（不封顶）。

        v0.27.3（Q2）：**暂停态先隐式 ``resume()`` 再收口**——不 resume 则
        ``_deadline is None`` 会让 ``_focus_elapsed`` 不封顶，**暂停时长会被算进
        专注**（口径是"暂停不算专注"）；回填后只记暂停前真实专注的那一段。
        不额外计打断/完成（§9 Q2）。
        """
        if self._paused_at is not None:
            self.resume()                     # Q2：先隐式继续（不额外计打断）
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

        v0.27.4（§4.3 C）：正计时模式**没有休息节律**，故 skip 等价于"结束本次
        正计时"——直接回 IDLE（不计完成、不进 SHORT/LONG），收尾与 ``stop()``
        一致；仍发专注段跳过文案（用户点了跳过，须有反馈，不静默）。
        """
        if self._state == FocusPhase.IDLE:
            return
        now = self._now()
        phase = self._state
        if self._mode == "stopwatch":
            self._emit(_SKIP_FOCUS)
            self._enter_idle(now)
            return
        if phase == FocusPhase.FOCUS:
            self._emit(_SKIP_FOCUS)
            # v0.27.5 无休息（§4.4 D2）：跳过的下一个落点同样是**专注段**
            # （没有休息可进）。跳过仍不计完成、不记 focus_sec（口径不变）。
            if self._rest_style == "none":
                self._enter(FocusPhase.FOCUS, self._work_minutes(), now)
                return
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
        # v0.27.3（§5.1）：暂停态**最前置早退**——睡多久都不迁移、不补算，也
        # 不进休息执法计量（"时间冻结"）。必须在 IDLE 判断之前。
        if self._paused_at is not None:
            return
        if self._state == FocusPhase.IDLE:
            return
        now = self._now()
        if self._deadline is None:
            # v0.27.4（§4.3 C / §5.2）：正计时专注段**无 deadline** → tick 必须
            # 什么都不做（不迁移、不记账、不进休息执法）。早退在这里是**显式
            # 契约**而非"顺手"：绝不能靠"deadline is None 恰好小于 now"这类巧合，
            # 否则正计时专注段会被误判到期。pomodoro 下非 IDLE 且未暂停时不可能
            # 走到这（`_enter` 必设 deadline），故对旧行为逐值无影响。
            return
        if now < self._deadline:
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
        共用这一条路径，保证两条入口的记账/气泡/cycle 推进逐字一致。

        v0.27.4（§4.3 C）：正计时模式**不参与 SHORT/LONG 节律**——记账
        （``completed``/``streak``/``focus_sec`` 实耗，走 ``_on_focus_complete``）
        与完成后气泡照旧，但随后**直接回 IDLE**，不推进 ``_completed_in_cycle``
        （``_enter_idle`` 本就会归零），也不进短休/长休。
        """
        self._on_focus_complete(now)
        self._emit(_FOCUS_DONE)
        if self._mode == "stopwatch":
            self._enter_idle(now)
            return
        self._completed_in_cycle += 1
        # v0.27.5 无休息（§4.4 D2）：``rest_style="none"`` 时专注段完成 → **直接进
        # 下一个专注段**。``_completed_in_cycle`` 照常累加、到 ``cycles`` 归零
        # （保持计数器有界，不推进 LONG），SHORT/LONG 永不出现。
        if self._rest_style == "none":
            if self._completed_in_cycle >= self._cycle_target():
                self._completed_in_cycle = 0
            self._enter(FocusPhase.FOCUS, self._work_minutes(), now)
            return
        if self._completed_in_cycle >= self._cycle_target():
            self._enter(FocusPhase.LONG, self._long_minutes(), now)
        else:
            self._enter(FocusPhase.SHORT, self._short_minutes(), now)

    def _enter(self, phase: FocusPhase, minutes: float, now: float) -> None:
        self._state = phase
        # v0.27.4（§4.3 C）：正计时专注段**不设 deadline**（只累计实耗，结束靠
        # stop()/complete()/skip()）——故 start()/start_preset() 传进来的时长在
        # stopwatch 下自然被忽略，无需各自特判。休息段永不进（无休息节律）。
        self._deadline = (None if (phase == FocusPhase.FOCUS
                                   and self._mode == "stopwatch")
                          else now + float(minutes) * 60.0)
        self._phase_started_at = now      # L3：实耗记账的相位起点
        # v0.27.3：进入新相位 = 离开暂停态。放在这里，`skip()` 与自然迁移
        # **无需各自特判**（§5.1「_enter/_enter_idle 都清」的关键简化）。
        self._paused_at = None
        self._remaining_s = None
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
        # v0.27.3：回 IDLE = 离开暂停态（`stop()` 无需特判，见 _enter 同注）
        self._paused_at = None
        self._remaining_s = None
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

        重启续算（v0.27.3 schema v2）：档案带 ``phase_started_at`` → 非暂停
        路径**优先**直接采用（合理时），故「**延长后重启**」的实耗也保真；v1
        档案无该字段（或值不合理）→ ``_load_persisted`` 按「``deadline`` − 本
        相位时长」反推，未延长的段与旧口径逐值等价（延长量在 v1 无字段可存，
        只能记回配置时长——进程内照记不误，故非静默失真）。

        v0.27.3 防御分支：暂停态 ``_deadline is None`` **不封顶**，若此时结算
        会把暂停时长算进专注——故显式按「本相位时长 − 冻结剩余」作为已消耗量
        返回。正常路径不会走到（``complete()`` 先隐式 ``resume()``），这是
        兜底：万一有别的路径在暂停态结算，也不丢数/不多算。
        """
        if self._paused_at is not None:
            return max(0.0, self._phase_minutes(self._state) * 60.0
                       - float(self._remaining_s or 0.0))
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
        # v0.27.6（§4.5 E）：跨日前把**刚结束的那一天**推进逐日历史，
        # 否则当日聚合会被直接清零、周/月统计无从谈起。
        # 幂等由上面的早退保证（同日重复调用到不了这里）；首次启动
        # ``self._stats.date`` 为空串 → ``_archive_day`` 内部直接 return，
        # 不会产生一条空记录。
        self._archive_day(self._stats.date, self._stats.completed,
                          self._stats.focus_sec, self._stats.breaks_cut)
        # 跨日/首次：日计数归零；streak 按「昨日有完成」延续
        if self._stats.date == _yesterday(today) and self._stats.completed > 0:
            streak = self._stats.streak_days
        else:
            streak = 0
        self._stats = FocusStats(date=today, completed=0, focus_sec=0,
                                 breaks_cut=0, streak_days=streak)

    def _archive_day(self, date, completed, focus_sec, breaks_cut) -> None:
        """把一天聚合成一条历史记录（幂等：同日重复调用覆盖而非追加）。

        ``date`` 为空或**当日零活动**（completed/focus_sec 都是 0）时不记录——
        空转日不该占历史额度，也不该让"连续天数"看起来有数据。
        （``breaks_cut`` 单独 > 0 但既无完成也无专注的天同样不记：休息执法只在
        专注节律跑起来后才有输入，那种"只打断没专注"的天在语义上是空转。）

        记录形状固定为 ``{"date": str, "completed": int, "focus_sec": int,
        "breaks_cut": int}``。同日覆盖而非追加：``_rollover`` 的早退已保证跨日
        只归档一次，这里是二重保险（例如外部直接调 ``_archive_day``）。
        最后统一 ``[-_HISTORY_MAX_DAYS:]`` 截断——超限丢最旧（§4.5 E）。
        """
        day = str(date or "")
        if not day:
            return
        c = _int(completed, 0, 10 ** 6)
        sec = _int(focus_sec, 0, 10 ** 7)
        cut = _int(breaks_cut, 0, 10 ** 6)
        if c == 0 and sec == 0:
            return
        rec = {"date": day, "completed": c, "focus_sec": sec,
               "breaks_cut": cut}
        if self._history and self._history[-1].get("date") == day:
            self._history[-1] = rec
        else:
            self._history.append(rec)
        self._history = self._history[-_HISTORY_MAX_DAYS:]

    # ---- 内部：持久化（focus_state.json 原子写，proactive.py:307 同款） ----

    def _persist(self) -> None:
        if not self._state_path:
            return
        try:
            phase = None if self._state == FocusPhase.IDLE else self._state.value
            data = {
                # v0.27.6：schema 2 → 3（新增 history 逐日历史）。
                # **loader 依旧不读 version**（§5.2 坑 1），故别指望版本号自动
                # 分流——v1/v2 档案无 `history` 键 → 历史为空列表，会话照常恢复。
                "version": 3,
                "stats": {
                    "date": self._stats.date,
                    "completed": self._stats.completed,
                    "focus_sec": self._stats.focus_sec,
                    "breaks_cut": self._stats.breaks_cut,
                    "streak_days": self._stats.streak_days,
                },
                # v3：逐日历史（最旧在前，上限 _HISTORY_MAX_DAYS 由 _archive_day
                # 保证）。写副本，防 dump 期间被并发改动。
                "history": list(self._history),
                "phase": phase,
                "deadline": self._deadline if phase else None,
                "completed_in_cycle": self._completed_in_cycle if phase else 0,
                # v0.27.4（§4.3 C / §5.2）：档案必须自带**计时模式**——正计时
                # 专注段 `deadline` 恒为 None，loader 只能靠这个键分流，否则
                # 正计时会话重启后会被 deadline 三重校验判为"已过期"而凭空消失
                # （与 v0.27.3 坑 2 同型）。v1/v2 老档案无该键 → 走原路径。
                "mode": self._mode,
                # v2：暂停态 `deadline` 恒为 None，剩余量只能靠 remaining_s 携带
                # （不写则重启后暂停的番茄消失，§5.2 坑 2）
                "paused": self._paused_at is not None if phase else False,
                "remaining_s": self._remaining_s if phase else None,
                # v2 顺带补既有缺口：相位起点落盘，非暂停路径重启时**优先**用它，
                # 于是「延长后重启」也保真（v1 只能记回配置时长，见 _focus_elapsed）
                "phase_started_at": self._phase_started_at if phase else None,
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
            # v0.27.6（§4.5 E）：逐日历史——**容错**读回。非 list 当空；逐条只认
            # dict 且 `date` 为非空 str（数值按 _int 钳制 ≥0），截断到上限。
            # v1/v2 档案无 `history` 键 → 历史为空列表，**绝不因此丢弃整个会话**
            # （version 依旧不参与分流，见 _persist 注）。
            # 顺序要紧：必须在下面的 `_rollover(now)` **之前**灌入，否则跨日时
            # `_rollover` 归档的"昨天"会写进一个还没读回来的空历史里。
            hist = raw.get("history")
            if isinstance(hist, list):
                out = []
                for item in hist:
                    if not isinstance(item, dict):
                        continue
                    d = str(item.get("date") or "")
                    if not d:
                        continue
                    out.append({
                        "date": d,
                        "completed": _int(item.get("completed"), 0, 10 ** 6),
                        "focus_sec": _int(item.get("focus_sec"), 0, 10 ** 7),
                        "breaks_cut": _int(item.get("breaks_cut"), 0, 10 ** 6),
                    })
                self._history = out[-_HISTORY_MAX_DAYS:]
            now = self._now()
            self._rollover(now)
            phase = raw.get("phase")
            deadline = raw.get("deadline")
            # v0.27.3（§5.2 坑 1+2）：**暂停分支必须在 phase 判定之前**，且**不
            # 做 deadline 校验**。loader 不读 version（坑 1）→ 不能指望版本号分流；
            # 暂停态 `deadline` 恒为 None（坑 2）→ 必过不了下面那三重校验 →
            # 静默落回 IDLE，**暂停的番茄凭空消失**。语义（§5.1 拍板）：暂停跨
            # 重启仍然暂停、剩余时间不变（app 关多久都算暂停，故 `_paused_at`
            # 取 now、相位起点按同式回填）。v1 档案无 `paused` 键 → 自然走 else。
            #
            # v0.27.4（§4.3 C / §5.2）：新增 `mode` 键分流**正计时**档案。正计时
            # 专注段 `deadline` 恒为 None → 必过不了下面那三重校验 → 不分流则
            # **正计时会话重启后凭空消失**（与坑 2 同型）。故：(a) 暂停分支放宽，
            # 兼容"正计时暂停态没有 remaining_s"；(b) 其后新增正计时分支；(c) 其余
            # （pomodoro 的 deadline 校验）**逐字不变**——v1/v2 老档案原路径。
            rem = raw.get("remaining_s")
            is_stopwatch = str(raw.get("mode") or "") == "stopwatch"
            if (raw.get("paused") and phase in ("focus", "short", "long")
                    and ((isinstance(rem, (int, float))
                          and not isinstance(rem, bool) and rem > 0)
                         or (is_stopwatch and phase == "focus"))):
                self._state = FocusPhase(phase)
                self._deadline = None
                self._paused_at = now
                self._remaining_s = (
                    float(rem) if isinstance(rem, (int, float))
                    and not isinstance(rem, bool) and rem > 0 else None)
                if self._remaining_s is not None:
                    # 与 resume() 同式回填：`now - (相位时长 − 剩余)`（坑 3）
                    self._phase_started_at = now - (
                        self._phase_minutes(self._state) * 60.0
                        - self._remaining_s)
                else:
                    # 正计时暂停态：无剩余量可回填，直接用档案里的相位起点
                    # （app 关多久都算暂停，故 `_paused_at` 取 now）
                    st = raw.get("phase_started_at")
                    self._phase_started_at = (
                        float(st) if isinstance(st, (int, float)) else now)
                self._completed_in_cycle = _int(
                    raw.get("completed_in_cycle"), 0, self._cycles)
                _log.info("[番茄] 恢复**暂停中**的节律（%s，剩余 %.0fs）",
                          phase, self._remaining_s)
                # Q1：暂停态姿态＝起身回 idle（不发 focus_rest_on）
                if self._state is FocusPhase.FOCUS:
                    self._fsm_event("focus_rest_off")
                return
            if is_stopwatch and phase == "focus":
                # 正计时进行中（无 deadline）：起点合理即恢复 FOCUS；起点缺失/
                # 离谱（未来 / 超一天前，对齐 deadline 校验的界线）→ 落到 else
                # 的 IDLE 兜底，绝不凭空造一个正在跑的会话。
                st = raw.get("phase_started_at")
                if (isinstance(st, (int, float))
                        and not isinstance(st, bool)
                        and float(st) <= now + _RESUME_MAX_FORWARD_S):
                    self._state = FocusPhase.FOCUS
                    self._deadline = None
                    self._phase_started_at = float(st)
                    self._completed_in_cycle = _int(
                        raw.get("completed_in_cycle"), 0, self._cycles)
                    _log.info("[番茄] 恢复进行中的**正计时**（起点 %.0f）",
                              self._phase_started_at)
                    # 重启续算：正计时专注中 → 补发陪伴姿态（同 0.25.5 口径）
                    self._fsm_event("focus_rest_on")
                    return
            if (phase in ("focus", "short", "long")
                    and isinstance(deadline, (int, float))
                    and deadline > now - _RESUME_GRACE_S
                    and deadline < now + _RESUME_MAX_FORWARD_S
                    and _same_day(float(deadline), now)):
                self._state = FocusPhase(phase)
                self._deadline = float(deadline)
                # L3 实耗记账的相位起点：**优先**用 v2 档案里的 phase_started_at
                # （存在且合理时）——这样「延长后重启」的实耗也保真；v1 档案无
                # 该字段/值不合理 → 回退旧的「deadline − 本相位时长」反推（未
                # 延长的段由此与旧口径逐值等价，见 _focus_elapsed）。
                started = raw.get("phase_started_at")
                if (isinstance(started, (int, float))
                        and not isinstance(started, bool)
                        and now - _RESUME_MAX_FORWARD_S <= float(started) <= now
                        and float(started) < self._deadline):
                    self._phase_started_at = float(started)
                else:
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
        # v0.27.4（§4.3 C）：正计时下 `minutes` 被忽略（无 deadline），回执必须
        # **如实**说"已开始正计时"而非"已开始番茄钟"——否则 LLM 会照着回一句
        # "25 分钟后提醒你"，而正计时根本没有到点这回事。
        if getattr(self._session, "mode", "pomodoro") == "stopwatch":
            return ToolResult(True, "已开始正计时（不限时；结束请用停止 / 提前"
                                    "完成 / 跳过，不进退休息）",
                              {"state": self._session.state.value,
                               "active": self._session.active,
                               "mode": "stopwatch"})
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


class _PausePomodoroHandler:
    def __init__(self, session) -> None:
        self._session = session

    def execute(self, args: dict, ctx):
        from pet.tools_schema import ToolResult

        if self._session is None:
            return ToolResult(False, "番茄功能未启用（config focus.enabled）", {})
        # L1：IDLE 下**可读说明**而非静默成功/静默失败（同 skip/extend 口径）
        if not self._session.active:
            return ToolResult(True, "当前未在番茄中（空闲），无需暂停",
                              {"state": self._session.state.value,
                               "active": False, "paused": False})
        if self._session.paused:
            return ToolResult(True, "已处于暂停中，无需重复暂停",
                              {"state": self._session.state.value,
                               "active": True, "paused": True})
        self._session.pause()
        return ToolResult(True, "已暂停番茄钟（剩余时间冻结，继续时接着走）",
                          {"state": self._session.state.value,
                           "active": self._session.active, "paused": True})


class _ResumePomodoroHandler:
    def __init__(self, session) -> None:
        self._session = session

    def execute(self, args: dict, ctx):
        from pet.tools_schema import ToolResult

        if self._session is None:
            return ToolResult(False, "番茄功能未启用（config focus.enabled）", {})
        if not self._session.active:
            return ToolResult(True, "当前未在番茄中（空闲），无需继续",
                              {"state": self._session.state.value,
                               "active": False, "paused": False})
        if not self._session.paused:
            return ToolResult(True, "当前未暂停，无需继续",
                              {"state": self._session.state.value,
                               "active": True, "paused": False})
        self._session.resume()
        return ToolResult(True, "已继续番茄钟（按暂停前的剩余时间接着走）",
                          {"state": self._session.state.value,
                           "active": self._session.active, "paused": False})


class _GetFocusStatsHandler:
    """只读统计查询（v0.27.6 §4.5 E + 裁决 Q5）。

    **任何相位（含 IDLE）都可查、零副作用**：只调 ``stats_range()``（纯读），
    不改 ``_stats``/``_history``/``_deadline``/相位，也不触发 ``_rollover``。
    与 ``get_weather`` 等 ``get_*`` 类查询一致：无危险级、不经 confirm。
    """

    def __init__(self, session) -> None:
        self._session = session

    def execute(self, args: dict, ctx):
        from pet.tools_schema import ToolResult

        if self._session is None:
            return ToolResult(False, "番茄功能未启用（config focus.enabled）", {})
        st = self._session.stats_range((args or {}).get("range") or "today")
        return ToolResult(True, format_stats_summary(st), st)


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
    # v0.27.3 暂停/继续（§5.1）：与 start/stop 同危险级（无危险级不打扰）。
    # 两个工具分开（而非单个 toggle）：LLM 语义更明确，且各自能如实回答
    # "已在暂停中"/"当前未暂停"这类无操作情形。
    pause_schema = ToolSchema(
        name="pause_pomodoro",
        description=(
            "暂停当前番茄段：剩余时间被冻结，暂停期间**不计**专注时长，计时"
            "不再向前走；用 resume_pomodoro 按剩余时间接着走。暂停期间宠物"
            "姿态回空闲，但仍算作专注中（不会打扰你）。空闲时无效果（会如实"
            "说明）；已在暂停中也会如实说明。"
        ),
        parameters={"type": "object", "properties": {},
                    "additionalProperties": False},
    )
    resume_schema = ToolSchema(
        name="resume_pomodoro",
        description=(
            "继续被暂停的番茄段（按暂停前的剩余时间接着走）。空闲时无效果"
            "（会如实说明）；本来就没暂停时也会如实说明。"
        ),
        parameters={"type": "object", "properties": {},
                    "additionalProperties": False},
    )
    # v0.27.6 统计查询（§4.5 E + 裁决 Q5）：**只读**、无必填项、无危险级。
    # 与 get_* 类查询同口径（dangerous=False / dangerous_when=None）。
    stats_schema = ToolSchema(
        name="get_focus_stats",
        description=(
            "查询番茄专注统计（**只读，不改变任何节律状态**；空闲时也可查）。"
            "可选参数 range：\"today\" 今日（默认）/ \"week\" 近 7 天（含今日）/ "
            "\"month\" 近 30 天；其余值按今日处理。返回该范围的完成番茄数、累计"
            "专注时长、休息打断次数、连续天数；近 7/30 天另含覆盖天数与日均"
            "完成数。逐日历史只保留有活动的天（上限 400 天）。"
        ),
        parameters={
            "type": "object",
            "properties": {"range": {"type": "string",
                                     "enum": ["today", "week", "month"],
                                     "default": "today"}},
            "additionalProperties": False,
        },
    )
    return [(start_schema, _StartPomodoroHandler(session)),
            (stop_schema, _StopPomodoroHandler(session)),
            (skip_schema, _SkipPomodoroHandler(session)),
            (extend_schema, _ExtendPomodoroHandler(session)),
            (pause_schema, _PausePomodoroHandler(session)),
            (resume_schema, _ResumePomodoroHandler(session)),
            (stats_schema, _GetFocusStatsHandler(session))]
