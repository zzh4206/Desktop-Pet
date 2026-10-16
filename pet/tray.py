"""系统托盘 —— 设计思路.md §2.5（退出走 shutdown）/ 平台适配与分工.md §二。

v0.1：``QSystemTrayIcon`` + “退出”菜单。退出回调走 app 层 ``shutdown()``。
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt, QObject
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

# v0.25.2 番茄入口文案（纯函数便于无 Qt 单测；运行态 → 下一动作语义）
_FOCUS_START_LABEL = "🍅 开始番茄钟"
_FOCUS_STOP_LABEL = "🍅 停止番茄钟"
# v0.27.4 正计时模式（§4.3 C）：无 deadline，点击 = 开始/结束本次正计时。文案必须
# 与 pomodoro 区分（同一个 toggle 项在两种模式下说的不是一回事），故单独一组常量
# 而非复用「番茄钟」——用户一眼能看出"这次没有倒数"。
_FOCUS_STOPWATCH_START_LABEL = "🍅 开始正计时"
_FOCUS_STOPWATCH_STOP_LABEL = "🍅 停止正计时"

# v0.27.1 番茄自由度扩展「入口层①：运行中控制」（wiki 设计-番茄自由度扩展技术
# 方案 §4.2 B1/B2/B3）：单一 toggle 项升级为「🍅 番茄钟」子菜单（▸ 由 Qt 按
# 子菜单自动绘制，故标题不带箭头字符）——开始/停止（toggle 语义逐字沿用）+
# ⏭ 跳过当前段 + ➕ 延长 5 分钟 + ✅ 提前完成。可用性由纯函数
# ``focus_availability`` 决定（**L1：IDLE 时后三项一律不可用**，从入口层再挡
# 一道，模型侧同类方法本就是 no-op）；``apply_focus_menu`` 只要求对象实现
# setText/setEnabled，故整条可用性逻辑可脱离 QApplication 单测。
_FOCUS_MENU_LABEL = "🍅 番茄钟"
# v0.27.3 暂停/继续（§5.1）：**单项**、文案随 paused 切换（暂停中显示「继续」）。
# 可用性 = `active`（**IDLE 置灰**——IDLE 下暂停无意义，也符合 L1）。
_FOCUS_PAUSE_LABEL = "⏸ 暂停"
_FOCUS_RESUME_LABEL = "▶️ 继续"
_FOCUS_SKIP_LABEL = "⏭ 跳过当前段"
_FOCUS_EXTEND_LABEL = "➕ 延长 5 分钟"
_FOCUS_COMPLETE_LABEL = "✅ 提前完成"

# v0.27.2 番茄自由度扩展「入口层② + 配置面」（wiki 设计-番茄自由度扩展技术方案
# §4.1 A / §4.4 D1/D3）：子菜单再加四类启动项——固定时长（25/45/60）+「🕘 上次
# 使用 N 分钟」+ 命名预设分组（D1，空数组时**不建项、不加分隔符**）+「⭐ 把 N
# 分钟设为默认」（as_default=True 走 §4.1 的"设为默认"路）+「🔄 重载番茄配置」
# （D3 热重载，用户显式触发）。
# **L1 要点**：这些启动项在 IDLE 下**必须可用**——点击 = 用户显式 start（不是
# 后台副作用），与「跳过/延长/提前完成」（IDLE 必须置灰，模型侧 no-op）是两类
# 语义；可用性在 ``focus_availability`` 里用不同键分开表达（start_items vs
# skip/extend/complete），不混为一谈。
_FOCUS_MINUTE_CHOICES = (25, 45, 60)
_FOCUS_MINUTE_LABEL_FMT = "⏱ {m} 分钟"
_FOCUS_LAST_LABEL_FMT = "🕘 上次使用 {m} 分钟"
_FOCUS_SET_DEFAULT_LABEL_FMT = "⭐ 把 {m} 分钟设为默认"
_FOCUS_PRESET_LABEL_FMT = "📌 {name}（{m} 分钟）"
_FOCUS_RELOAD_LABEL = "🔄 重载番茄配置"
# 托盘**只做显示**，不 import pet.focus（保持共享层解耦）；无 last_work_min
# 信息时文案兜底用库默认 25（app 建立会话后立刻用真实值覆盖，见
# TrayManager.set_focus_last_work_min）。
_FOCUS_FALLBACK_MIN = 25

# 「启动型」动作键（恒可用：点击=显式 start/重载，§4.1 A）——分钟项按
# _FOCUS_MINUTE_CHOICES 生成，改档位只需改上面那一处。
_FOCUS_START_ACTION_KEYS = tuple(
    [f"minute_{m}" for m in _FOCUS_MINUTE_CHOICES] + ["last", "set_default"])

_PHASE_IDLE = "idle"
_PHASES = ("idle", "focus", "short", "long")


def _is_stopwatch_mode(mode) -> bool:
    """``mode`` 归一化：只有明确是 ``"stopwatch"`` 才算正计时（其余 → pomodoro）。

    与 ``pet.focus`` 侧 ``_apply_cfg`` 的判据逐字一致（strip + lower），但托盘
    **刻意不 import pet.focus**（保持共享层解耦）；未知/None 一律按 pomodoro 处理
    ——fail-safe 方向是"维持既有行为"，默认模式绝不可能被误判成正计时（Q4）。
    """
    return str(mode or "").strip().lower() == "stopwatch"


def focus_label(active: bool, mode="pomodoro") -> str:
    """番茄项文案：运行中显示「停止」（点击即 stop），否则显示「开始」。

    v0.27.4：``mode="stopwatch"`` → 「🍅 开始正计时」/「🍅 停止正计时」；
    缺省/未知模式 → 沿用 v0.25.2 的番茄钟文案（**向后兼容**，老调用点不破）。
    """
    if _is_stopwatch_mode(mode):
        return (_FOCUS_STOPWATCH_STOP_LABEL if active
                else _FOCUS_STOPWATCH_START_LABEL)
    return _FOCUS_STOP_LABEL if active else _FOCUS_START_LABEL


def focus_pause_label(paused: bool) -> str:
    """暂停项文案（v0.27.3，单项 toggle）：暂停中显示「▶️ 继续」，否则「⏸ 暂停」。"""
    return _FOCUS_RESUME_LABEL if paused else _FOCUS_PAUSE_LABEL


def focus_phase_name(state) -> str:
    """相位归一化：``FocusPhase`` / 其 ``.value`` / 字符串 / None → 四个相位名。

    tray 刻意**不 import pet.focus**（托盘层不耦合节律器，与既有 duck-typing
    读口同风格）：只读 ``.value`` 或字符串。未知/None 一律按 ``idle`` 处理——
    fail-safe 方向是「更不可用」：宁可多灰一项，也不给 IDLE 下的误点留门。
    """
    v = getattr(state, "value", state)
    v = str(v).strip().lower() if v is not None else _PHASE_IDLE
    return v if v in _PHASES else _PHASE_IDLE


def _fmt_minutes(m) -> str:
    """分钟数值 → 文案（25 → "25"、7.5 → "7.5"）；非法/≤0 → ""。"""
    try:
        f = float(m)
    except (TypeError, ValueError):
        return ""
    if not (f > 0):        # NaN 走此分支
        return ""
    return str(int(f)) if f == int(f) else f"{f:g}"


def focus_availability(state, last_work_min=None, paused=False,
                       mode="pomodoro") -> dict:
    """相位 → 子菜单各项的文案与可用性（纯函数，§4.1/§4.2/§4.4）。

    「运行中控制」项（v0.27.1 口径逐字不变）：

    - ``toggle``：**恒可用**（文案沿用 ``focus_label``：「开始」↔「停止」）
    - ``skip``：非 IDLE 可用（专注/短休/长休；模型侧 IDLE 本就 no-op）
    - ``extend``：非 IDLE 可用（专注段与休息段均可延长，模型侧 IDLE no-op）
    - ``complete``：**仅 FOCUS 可用**——休息段没有「提前完成」语义，若可点会
      凭空 +1 番茄（模型侧已 no-op，此处是入口层双保险）

    v0.27.3 新增：

    - ``pause``：**非 IDLE 可用**（== ``active``）——IDLE 下暂停无意义（模型侧
      本就 no-op），置灰同时满足 L1「IDLE 零副作用」；暂停中**仍可用**（点击
      = 继续），故不看 ``paused``。
    - ``pause_label``：随 ``paused`` 在「⏸ 暂停」/「▶️ 继续」间切换。

    v0.27.2 新增（L1 要点）：

    - ``start_items``：时长项（25/45/60）／``last``／``set_default`` **恒可用**
      ——IDLE 下正是它们的用武之地（A 维度：启动时选时长）；点击是**用户显式
      start**，不是自动副作用，故不受 L1 的"IDLE 置灰"约束。
    - ``reload``：「🔄 重载番茄配置」恒可用（只重读 config，不动当前段）。
    - ``last_label`` / ``set_default_label``：带 ``last_work_min`` 的文案（未给
      或非法 → 用 ``_FOCUS_FALLBACK_MIN`` 兜底，故文案恒非空、无需调用方判空）。

    v0.27.4 新增 ``mode``（默认 ``"pomodoro"``，**向后兼容**：老调用点不破）：

    - ``toggle_label``：随模式切换——pomodoro「🍅 开始/停止番茄钟」；
      stopwatch「🍅 开始正计时」/「🍅 停止正计时」。
    - ``start_items``：**stopwatch 下恒 False**——正计时没有时长概念，⏱ 固定时长
      /🕘 上次使用/📌 预设/⭐ 设为默认**全部置灰**（点了也无意义：`_enter` 在
      stopwatch 下不设 deadline，时长参数被忽略）。IDLE 下 pomodoro 仍恒 True
      （这正是 v0.27.2「启动时选时长」的入口）。
    """
    phase = focus_phase_name(state)
    active = phase != _PHASE_IDLE
    stopwatch = _is_stopwatch_mode(mode)
    m = _fmt_minutes(last_work_min) or str(_FOCUS_FALLBACK_MIN)
    return {
        "phase": phase,
        "active": active,
        "mode": "stopwatch" if stopwatch else "pomodoro",
        "toggle_label": focus_label(active, mode),
        "pause": active,
        "pause_label": focus_pause_label(paused),
        "skip": active,
        "extend": active,
        "complete": phase == "focus",
        "start_items": not stopwatch,
        "reload": True,
        "last_label": _FOCUS_LAST_LABEL_FMT.format(m=m),
        "set_default_label": _FOCUS_SET_DEFAULT_LABEL_FMT.format(m=m),
    }


def apply_focus_menu(actions: dict, state, last_work_min=None,
                     paused=False, mode="pomodoro") -> dict:
    """把 ``focus_availability`` 施加到动作集合，返回该可用性字典。

    ``actions`` 的值只需实现 ``setText(str)`` / ``setEnabled(bool)``（真实
    QAction 或测试替身皆可），故本函数是纯逻辑，可脱离 QApplication 单测。
    唯一例外是 ``"presets"``：值为**动作列表**（命名预设分组，逐项 setEnabled；
    预设项文案由 ``TrayManager.set_focus_presets`` 建项时写死，与相位无关）。
    缺项（缺键/None）静默跳过，便于兼容旧调用方。``mode`` 缺省 ``"pomodoro"``
    （v0.27.4 追加在参数表末尾，老调用点逐字不变）。
    """
    avail = focus_availability(state, last_work_min, paused, mode)
    actions = actions or {}
    toggle = actions.get("toggle")
    if toggle is not None:
        toggle.setText(avail["toggle_label"])
        toggle.setEnabled(True)          # toggle 是唯一恒可用项
    # v0.27.3 暂停/继续（单项，文案随 paused 切；IDLE 置灰）
    pause = actions.get("pause")
    if pause is not None:
        pause.setText(avail["pause_label"])
        pause.setEnabled(avail["pause"])
    for key in ("skip", "extend", "complete"):
        obj = actions.get(key)
        if obj is not None:
            obj.setEnabled(avail[key])
    # v0.27.2 启动型项：恒可用（点击 = 显式 start，§4.1 A 的入口）
    for key in _FOCUS_START_ACTION_KEYS:
        obj = actions.get(key)
        if obj is not None:
            obj.setEnabled(avail["start_items"])
    last = actions.get("last")
    if last is not None:
        last.setText(avail["last_label"])
    default = actions.get("set_default")
    if default is not None:
        default.setText(avail["set_default_label"])
    for obj in (actions.get("presets") or []):
        if obj is not None:
            obj.setEnabled(avail["start_items"])
    reload_act = actions.get("reload")
    if reload_act is not None:
        reload_act.setEnabled(avail["reload"])
    return avail


def tooltip_text(base: str, focus_count: int = 0,
                 status: str | None = None) -> str:
    """托盘 tooltip 合成（纯函数，便于无 Qt 单测）。

    顺序：基础行（v0.17 热键提示）→ v0.25.6 今日番茄「🍅×N」（0 不显示）
    → v0.19.2 状态行；三段互不覆盖（各自 setter 只改自己那段）。
    """
    parts = [base]
    if focus_count:
        parts.append(f"🍅×{int(focus_count)}")
    if status:
        parts.append(status)
    return "\n".join(parts)


class TrayManager(QObject):
    def __init__(self, on_quit, parent=None):
        super().__init__(parent)
        self._on_quit = on_quit
        self._on_chat = None
        self._on_reset = None
        self._on_spit = None
        self._on_mem = None
        self._on_chat_emotion = None
        self._on_status = None
        self._autostart_state = False
        self._on_autostart = None  # v0.11 自启切换回调（L14 修：注释原误标"强制吐出"）
        # v0.26 语音播报总开关（托盘勾选项 ↔ config.voice.tts.enabled）
        self._voice_state = False
        self._on_voice = None
        # v0.24.5 主动纠缠「安静模式」（勾选 = 安静 = config.attention.pester.enabled
        # 为 false）。托盘侧只说「安静」不做取反，换算留给 app——避免两处语义打架。
        self._quiet_state = False
        self._on_quiet = None
        self._tray = QSystemTrayIcon(self._make_icon(), parent)
        self._tray.setToolTip("桌宠")
        # v0.19.2 F9：tooltip = 基础行（热键提示）+ 状态行（养成数值）合成；
        # 触线警示时图标角落加红点（_make_icon(alert=True)）
        self._base_tooltip = "桌宠"
        self._status_line: str | None = None
        self._focus_count = 0            # v0.25.6 今日完成番茄数（tooltip 🍅×N）

        menu = QMenu()
        act_chat = menu.addAction("聊天")
        act_chat.triggered.connect(self._emit_chat)
        # v0.20 模型管理：快速切换子菜单（set_models 动态重建）+ 管理对话框
        self._models_menu = QMenu("切换模型")
        self._on_model_switch = None
        self._on_model_manager = None
        self.set_models([], "", None)  # 初始占位（app 启动后 set_models 覆盖）
        menu.addMenu(self._models_menu)
        act_model_mgr = menu.addAction("模型管理…")
        act_model_mgr.triggered.connect(self._emit_model_manager)
        act_reset = menu.addAction("重新开始")
        act_mem = menu.addAction("记忆管理")
        act_emotion = menu.addAction("聊天情绪设置")
        act_status = menu.addAction("状态板")
        self._act_auto = act_auto = menu.addAction("开机自启")
        act_auto.setCheckable(True)
        act_auto.setChecked(self._autostart_state)
        act_auto.toggled.connect(self._emit_autostart)
        act_reset.triggered.connect(self._emit_reset)
        act_mem.triggered.connect(self._emit_mem)
        act_emotion.triggered.connect(self._emit_chat_emotion)
        act_status.triggered.connect(self._emit_status)
        # v0.26 语音播报总开关（opt-in，默认关）：勾选即写回 config 并即时生效
        self._act_voice = act_voice = menu.addAction("语音播报")
        act_voice.setCheckable(True)
        act_voice.setChecked(self._voice_state)
        act_voice.toggled.connect(self._emit_voice)
        # v0.24.5 安静模式（默认不勾）：勾上 → 零纠缠行为（只留久坐/天气/喝水等
        # 功能性提醒）；attention 数值照常演化，故取消勾选能接着算而非重置。
        self._act_quiet = act_quiet = menu.addAction("安静模式")
        act_quiet.setCheckable(True)
        act_quiet.setChecked(self._quiet_state)
        act_quiet.toggled.connect(self._emit_quiet)
        # v0.7 强制吐出：吃鼠标期间鼠标被抑制点不到菜单，故此菜单主要服务于
        # 非锁定态的残留释放 + 键盘可达用户（Tab/方向键导航菜单）。主逃生口
        # 仍是热键 Cmd+Option+T + 看门狗。
        act_spit = menu.addAction("强制吐出")
        act_spit.triggered.connect(self._emit_spit)
        # v0.25.2 番茄专注入口（opt-in）；v0.27.1 由单一 toggle 项升级为
        # 「🍅 番茄钟」子菜单（§4.2 B1/B2/B3 运行中控制）——开始/停止 +
        # 跳过长休/短休/专注、延长、提前完成。可用性按当前相位经纯函数
        # apply_focus_menu 施加（初始 IDLE：后三项置灰）。focus.enabled=false
        # 时 app 调 set_focus_visible(False) 隐藏整个子菜单（§1 两段式开关）
        self._on_focus = None
        self._on_focus_pause = None
        self._on_focus_skip = None
        self._on_focus_extend = None
        self._on_focus_complete = None
        # v0.27.2 启动类四个入口 + 命名预设分组状态（§4.1 A / §4.4 D1/D3）
        self._on_focus_minutes = None      # cb(minutes)：「⏱ N 分钟」/「🕘 上次使用」
        self._on_focus_preset = None       # cb(preset dict)：命名预设
        self._on_focus_default = None      # cb()：「⭐ 设为默认」
        self._on_focus_reload = None       # cb()：「🔄 重载番茄配置」
        self._focus_presets: list = []
        self._focus_preset_actions: list = []
        self._focus_preset_sep = None
        self._focus_last_work_min = 0.0    # 0 → 文案用 _FOCUS_FALLBACK_MIN 兜底
        self._focus_phase = _PHASE_IDLE
        self._focus_paused = False         # v0.27.3 暂停态（暂停项文案/可用性）
        # v0.27.4 计时模式（§4.3 C）：只影响 toggle 文案与启动型项可用性，
        # 缺省 pomodoro 保证未接线的调用点/旧测试逐字不变
        self._focus_mode = "pomodoro"
        # 子菜单标题不带「▸」——mac/Qt 会按子菜单自动绘制箭头字符
        self._focus_menu = focus_menu = menu.addMenu(_FOCUS_MENU_LABEL)
        self._act_focus = act_focus = focus_menu.addAction(_FOCUS_START_LABEL)
        act_focus.triggered.connect(self._emit_focus)
        # v0.27.2 启动时长选择（A 维度）：25/45/60 三档 + 「上次使用」——
        # 一次性时长（不改默认，§9 Q6）；「上次使用」与固定档位共用同一回调。
        focus_menu.addSeparator()
        self._focus_minute_actions: dict = {}
        for _m in _FOCUS_MINUTE_CHOICES:
            _act = focus_menu.addAction(_FOCUS_MINUTE_LABEL_FMT.format(m=_m))
            _act.triggered.connect(
                (lambda *_a, _mm=_m: self._emit_focus_minutes(_mm)))
            self._focus_minute_actions[_m] = _act
        self._act_focus_last = act_last = focus_menu.addAction(
            _FOCUS_LAST_LABEL_FMT.format(m=_FOCUS_FALLBACK_MIN))
        act_last.triggered.connect(self._emit_focus_last)
        # 命名预设分组由 set_focus_presets 动态重建（插在「设为默认」之前）；
        # **presets 为空时不建项、不加分隔符**（空菜单里留一条分隔线无意义）
        self._act_focus_default = act_default = focus_menu.addAction(
            _FOCUS_SET_DEFAULT_LABEL_FMT.format(m=_FOCUS_FALLBACK_MIN))
        act_default.triggered.connect(self._emit_focus_default)
        focus_menu.addSeparator()
        # v0.27.3 暂停/继续（单项，文案随 paused 切；可用性=非 IDLE）
        self._act_focus_pause = act_pause = focus_menu.addAction(
            _FOCUS_PAUSE_LABEL)
        act_pause.triggered.connect(self._emit_focus_pause)
        self._act_focus_skip = act_skip = focus_menu.addAction(
            _FOCUS_SKIP_LABEL)
        act_skip.triggered.connect(self._emit_focus_skip)
        self._act_focus_extend = act_ext = focus_menu.addAction(
            _FOCUS_EXTEND_LABEL)
        act_ext.triggered.connect(self._emit_focus_extend)
        self._act_focus_complete = act_cmp = focus_menu.addAction(
            _FOCUS_COMPLETE_LABEL)
        act_cmp.triggered.connect(self._emit_focus_complete)
        focus_menu.addSeparator()
        self._act_focus_reload = act_reload = focus_menu.addAction(
            _FOCUS_RELOAD_LABEL)
        act_reload.triggered.connect(self._emit_focus_reload)
        self._apply_focus_menu()     # 初始 IDLE → 后三项不可用（L1）
        menu.addSeparator()
        act_quit = menu.addAction("退出")
        act_quit.triggered.connect(self._on_quit)
        self._tray.setContextMenu(menu)
        self._menu = menu  # 保留引用，remove() 时 deleteLater 释放
        self._tray.show()

    def _emit_mem(self) -> None:
        """v0.9 记忆管理菜单。"""
        if self._on_mem:
            self._on_mem()

    def set_tooltip(self, text: str) -> None:
        """v0.17.0：动态 tooltip（app 在热键注册成功后调，提示直达热键）。

        v0.19.2 F9 改为存基础行：与 set_status 的状态行合成，互不覆盖。"""
        self._base_tooltip = text
        self._apply_tooltip()

    def set_status(self, status: str | None) -> None:
        """v0.19.2 F9：状态行（app 状态变化时调，如 `饱食28⚠ 心情65`）。

        None/空串清除状态行；不清基础行。"""
        self._status_line = status or None
        self._apply_tooltip()

    def set_alert(self, on: bool) -> None:
        """v0.19.2 F9：任何需求触线时图标加红点（app 状态变化时调）。"""
        if bool(on) == getattr(self, "_alert", False):
            return
        self._alert = bool(on)
        self._tray.setIcon(self._make_icon(alert=self._alert))

    def _apply_tooltip(self) -> None:
        # v0.25.6 番茄线：今日完成数「🍅×N」（0 不显示——没开番茄的托盘与
        # 旧版逐字一致）；合成逻辑在模块级纯函数 tooltip_text
        self._tray.setToolTip(tooltip_text(
            self._base_tooltip, getattr(self, "_focus_count", 0),
            self._status_line))

    def set_focus_count(self, count: int) -> None:
        """v0.25.6：今日完成番茄数进 tooltip（app 完成/跨日/启动时调）。

        仅当数值变化才写 QSystemTrayIcon（幂等，tick 每拍调用零成本）。
        """
        count = max(0, int(count))
        if count == getattr(self, "_focus_count", 0):
            return
        self._focus_count = count
        self._apply_tooltip()

    def set_autostart_state(self, enabled: bool) -> None:
        """v0.11：同步自启菜单勾选态（app 启动时调）。

        M9 修：旧版只写标志不动 QAction——已启用时重启 app 托盘仍显示
        未勾选。blockSignals 防 setChecked 触发 toggled 回环调 setter。
        """
        self._autostart_state = enabled
        act = getattr(self, "_act_auto", None)
        if act is not None:
            act.blockSignals(True)
            act.setChecked(enabled)
            act.blockSignals(False)

    def set_autostart_callback(self, cb) -> None:
        self._on_autostart = cb

    def _emit_autostart(self, checked: bool) -> None:
        if self._on_autostart:
            self._on_autostart(checked)

    def set_mem_callback(self, cb) -> None:
        self._on_mem = cb

    def set_chat_emotion_callback(self, cb) -> None:
        self._on_chat_emotion = cb

    def _emit_chat_emotion(self) -> None:
        if self._on_chat_emotion:
            self._on_chat_emotion()

    def set_status_callback(self, cb) -> None:
        self._on_status = cb

    def _emit_status(self) -> None:
        if self._on_status:
            self._on_status()

    def set_voice_state(self, enabled: bool) -> None:
        """v0.26：同步语音开关勾选态（app 启动/配置变更时调）。

        blockSignals 防 setChecked 触发 toggled 回环回调（同自启 M9 教训）。
        """
        self._voice_state = bool(enabled)
        act = getattr(self, "_act_voice", None)
        if act is not None:
            act.blockSignals(True)
            act.setChecked(bool(enabled))
            act.blockSignals(False)

    def set_voice_callback(self, cb) -> None:
        self._on_voice = cb

    def _emit_voice(self, checked: bool) -> None:
        if self._on_voice:
            self._on_voice(checked)

    def set_quiet_state(self, quiet: bool) -> None:
        """v0.24.5：同步「安静模式」勾选态（app 启动/配置变更时调）。

        ``quiet=True`` 即 attention.pester.enabled=false。blockSignals 防
        setChecked 触发 toggled 回环回调（同自启 M9 教训）。
        """
        self._quiet_state = bool(quiet)
        act = getattr(self, "_act_quiet", None)
        if act is not None:
            act.blockSignals(True)
            act.setChecked(bool(quiet))
            act.blockSignals(False)

    def set_quiet_callback(self, cb) -> None:
        self._on_quiet = cb

    def _emit_quiet(self, checked: bool) -> None:
        if self._on_quiet:
            self._on_quiet(checked)

    def set_chat_callback(self, cb) -> None:
        """v0.4：托盘'聊天'唤出聊天面板（v0.11 真全局热键占位）。"""
        self._on_chat = cb

    def set_reset_callback(self, cb) -> None:
        """v0.5：托盘'重新开始'→app 经 platform.confirm_dangerous 二次确认后清档复位。"""
        self._on_reset = cb

    # ---- v0.20 模型管理 ----

    def set_model_manager_callback(self, cb) -> None:
        """托盘'模型管理…'→app._show_model_manager（增删改 Key/切换/测连接）。"""
        self._on_model_manager = cb

    def _emit_model_manager(self) -> None:
        cb = getattr(self, "_on_model_manager", None)
        if cb:
            cb()
        else:
            logging.getLogger("pet").warning(
                "托盘'模型管理…'点击但未注册 callback")

    def set_models(self, entries, current, cb) -> None:
        """重建'切换模型'子菜单。

        ``entries``: [(name, label)]（app 传入，label 已含模型名/密钥态）；
        ``current``: 当前使用中的 name（勾选态）；``cb(name)``: 选中回调。
        空列表时放一个禁用占位行，子菜单不至于空白不可点。"""
        self._on_model_switch = cb
        self._models_menu.clear()
        if not entries:
            ph = self._models_menu.addAction("（未配置模型）")
            ph.setEnabled(False)
            return
        for name, label in entries:
            act = self._models_menu.addAction(label)
            act.setCheckable(True)
            act.setChecked(name == current)
            # lambda 默认参绑定当前 name（闭包晚绑定陷阱）
            act.triggered.connect(lambda _=False, n=name: self._emit_model_switch(n))

    def _emit_model_switch(self, name: str) -> None:
        if self._on_model_switch:
            self._on_model_switch(name)

    def set_spit_callback(self, cb) -> None:
        """v0.7：托盘'强制吐出'→EatMouseSession.force_spit（停 CGEventTap + 回 idle）。"""
        self._on_spit = cb

    def _emit_chat(self) -> None:
        if self._on_chat is not None:
            self._on_chat()
        else:
            # 未设 chat callback（正常应在 _build_chat_panel 里注册 fallback）；
            # tray 无 bubble 引用，记日志便于排查托盘聊天点击无反应
            logging.getLogger("pet").warning(
                "托盘'聊天'点击但未注册 callback（聊天面板未初始化？）"
            )

    def _emit_reset(self) -> None:
        if self._on_reset is not None:
            self._on_reset()

    def _emit_spit(self) -> None:
        if self._on_spit is not None:
            self._on_spit()

    # ---- v0.25.2 番茄专注入口（v0.27.1 升级为子菜单；v0.27.2 加启动类入口） ----

    def set_focus_callback(self, cb) -> None:
        """托盘'🍅 开始/停止番茄钟'→ app._toggle_focus（toggle 语义单点）。"""
        self._on_focus = cb

    def _emit_focus(self) -> None:
        if self._on_focus is not None:
            self._on_focus()

    def set_focus_pause_callback(self, cb) -> None:
        """子菜单「⏸ 暂停 / ▶️ 继续」→ app._toggle_focus_pause（v0.27.3）。

        **单项 toggle**：app 侧按 ``FocusSession.paused`` 决定调 ``pause()`` 还是
        ``resume()``，托盘只负责"这一项被点了"。
        """
        self._on_focus_pause = cb

    def _emit_focus_pause(self) -> None:
        if self._on_focus_pause is not None:
            self._on_focus_pause()

    def set_focus_skip_callback(self, cb) -> None:
        """子菜单「⏭ 跳过当前段」→ app._skip_focus（FocusSession.skip 薄转发）。"""
        self._on_focus_skip = cb

    def set_focus_extend_callback(self, cb) -> None:
        """子菜单「➕ 延长 5 分钟」→ app._extend_focus（extend(5) 薄转发）。"""
        self._on_focus_extend = cb

    def set_focus_complete_callback(self, cb) -> None:
        """子菜单「✅ 提前完成」→ app._complete_focus（complete() 薄转发，计入完成）。"""
        self._on_focus_complete = cb

    def set_focus_minutes_callback(self, cb) -> None:
        """子菜单「⏱ N 分钟」/「🕘 上次使用 N 分钟」→ app._start_focus_minutes(minutes)。

        两者共用同一回调：都是**一次性**时长启动（不改默认，§9 Q6）。
        """
        self._on_focus_minutes = cb

    def set_focus_preset_callback(self, cb) -> None:
        """命名预设项 → app._start_focus_preset(preset)（本轮覆盖，§4.4 D1）。"""
        self._on_focus_preset = cb

    def set_focus_default_callback(self, cb) -> None:
        """「⭐ 把 N 分钟设为默认」→ app._set_focus_default()（as_default=True，§4.1）。"""
        self._on_focus_default = cb

    def set_focus_reload_callback(self, cb) -> None:
        """「🔄 重载番茄配置」→ app._reload_focus_config()（§4.4 D3 热重载）。"""
        self._on_focus_reload = cb

    def _emit_focus_skip(self) -> None:
        if self._on_focus_skip is not None:
            self._on_focus_skip()

    def _emit_focus_extend(self) -> None:
        if self._on_focus_extend is not None:
            self._on_focus_extend()

    def _emit_focus_complete(self) -> None:
        if self._on_focus_complete is not None:
            self._on_focus_complete()

    def _emit_focus_minutes(self, minutes) -> None:
        if self._on_focus_minutes is not None:
            self._on_focus_minutes(minutes)

    def _emit_focus_last(self) -> None:
        """「🕘 上次使用 N 分钟」：把当前记着的 N 当一次性时长启动。"""
        self._emit_focus_minutes(
            getattr(self, "_focus_last_work_min", 0.0) or _FOCUS_FALLBACK_MIN)

    def _emit_focus_preset(self, preset) -> None:
        if self._on_focus_preset is not None:
            self._on_focus_preset(preset)

    def _emit_focus_default(self) -> None:
        if self._on_focus_default is not None:
            self._on_focus_default()

    def _emit_focus_reload(self) -> None:
        if self._on_focus_reload is not None:
            self._on_focus_reload()

    def set_focus_presets(self, presets) -> None:
        """重建「命名预设」分组（v0.27.2 §4.4 D1；app 装载/重载 config 后调）。

        ``presets`` 为空 → **不建项、不加分隔符**（空分组留在菜单里只会多一条
        无意义的分隔线）；非空 → 在「⭐ 把 N 分钟设为默认」之前插入「分隔符 +
        每个预设一项」，点击即以该预设启动**本轮**（``FocusSession.start_preset``
        的本轮覆盖，不改全局默认）。幂等：重复调用先撤掉旧项再重建。
        """
        menu = getattr(self, "_focus_menu", None)
        if menu is None:
            return
        for act in list(getattr(self, "_focus_preset_actions", [])):
            menu.removeAction(act)
            act.deleteLater()
        self._focus_preset_actions = []
        old_sep = getattr(self, "_focus_preset_sep", None)
        if old_sep is not None:
            menu.removeAction(old_sep)
            old_sep.deleteLater()
            self._focus_preset_sep = None
        items = [dict(p) for p in (presets or []) if isinstance(p, dict)
                 and str(p.get("name") or "").strip()]
        self._focus_presets = items
        if not items:
            self._apply_focus_menu()
            return
        anchor = getattr(self, "_act_focus_default", None)
        sep = QAction(menu)
        sep.setSeparator(True)
        if anchor is not None:
            menu.insertAction(anchor, sep)
        else:
            menu.addAction(sep)
        self._focus_preset_sep = sep
        for p in items:
            name = str(p.get("name") or "").strip()
            m = _fmt_minutes(p.get("work_min"))
            text = (_FOCUS_PRESET_LABEL_FMT.format(name=name, m=m)
                    if m else f"📌 {name}")
            act = QAction(text, menu)
            act.triggered.connect(
                (lambda *_a, _p=dict(p): self._emit_focus_preset(_p)))
            if anchor is not None:
                menu.insertAction(anchor, act)
            else:
                menu.addAction(act)
            self._focus_preset_actions.append(act)
        self._apply_focus_menu()

    def set_focus_last_work_min(self, minutes) -> None:
        """「🕘 上次使用 N 分钟」/「⭐ 把 N 分钟设为默认」文案（v0.27.2，幂等）。

        app 在会话建立后与每次时长变化后调（值未变即早返回，tick 每拍调用零
        成本）。非法/≤0 一律忽略（保留旧值）——托盘只显示，判定归模型层。
        """
        try:
            val = float(minutes)
        except (TypeError, ValueError):
            return
        if not (val > 0):        # NaN 走此分支
            return
        if val == getattr(self, "_focus_last_work_min", None):
            return
        self._focus_last_work_min = val
        self._apply_focus_menu()

    def _apply_focus_menu(self) -> None:
        """按当前相位 + 上次时长 + 暂停态 + 计时模式刷新子菜单文案/可用性（幂等）。"""
        actions = {
            "toggle": getattr(self, "_act_focus", None),
            "pause": getattr(self, "_act_focus_pause", None),
            "skip": getattr(self, "_act_focus_skip", None),
            "extend": getattr(self, "_act_focus_extend", None),
            "complete": getattr(self, "_act_focus_complete", None),
            "last": getattr(self, "_act_focus_last", None),
            "set_default": getattr(self, "_act_focus_default", None),
            "reload": getattr(self, "_act_focus_reload", None),
            "presets": list(getattr(self, "_focus_preset_actions", []) or []),
        }
        for m, act in (getattr(self, "_focus_minute_actions", {}) or {}).items():
            actions[f"minute_{m}"] = act
        apply_focus_menu(
            actions, getattr(self, "_focus_phase", _PHASE_IDLE),
            getattr(self, "_focus_last_work_min", 0.0),
            getattr(self, "_focus_paused", False),
            getattr(self, "_focus_mode", "pomodoro"))

    def set_focus_visible(self, visible: bool) -> None:
        """focus.enabled=false → 彻底隐藏整个「🍅 番茄钟」子菜单（§1 两段式）。

        QMenu 作为菜单项存在时以 ``menuAction()`` 为准（QMenu 自身 setVisible
        在部分 Qt 版本不联动父菜单项）；保留 QAction 分支兼容旧测试替身。
        """
        m = getattr(self, "_focus_menu", None)
        act = getattr(self, "_act_focus", None)
        if m is not None:
            m.menuAction().setVisible(bool(visible))
        elif act is not None:
            act.setVisible(bool(visible))

    def set_focus_phase(self, state) -> None:
        """app 在 start/stop/skip/extend/complete 后与每拍同步时调：按相位刷新
        子菜单四项的文案与可用性（相位未变则不碰 QAction，幂等零成本）。

        v0.27.1 取代 v0.25.2 的 ``set_focus_active(bool)``（后者保留为兼容
        包装）：bool 只能区分「跑没跑」，无法表达「休息段 disable 提前完成」，
        故状态提供者升级为**相位**。
        """
        phase = focus_phase_name(state)
        if phase == getattr(self, "_focus_phase", None):
            return
        self._focus_phase = phase
        self._apply_focus_menu()

    def set_focus_paused(self, paused) -> None:
        """同步**暂停态**（v0.27.3，app 在 pause/resume 后与每拍同步时调）。

        暂停不改相位，故必须与 ``set_focus_phase`` 分开表达——只影响「⏸ 暂停 /
        ▶️ 继续」这一项的文案（可用性只看 ``active``）。幂等：值未变不碰 QAction。
        """
        p = bool(paused)
        if p == getattr(self, "_focus_paused", None):
            return
        self._focus_paused = p
        self._apply_focus_menu()

    def set_focus_mode(self, mode) -> None:
        """同步**计时模式**（v0.27.4，app 在 ``_sync_focus_tray`` 每拍调用）。

        与相位/暂停态一样是**正交**维度：pomodoro → toggle 文案「🍅 开始/停止
        番茄钟」、启动型项（⏱/🕘/📌/⭐）恒可用；stopwatch → 「🍅 开始正计时」/
        「🍅 停止正计时」、启动型项**全部置灰**（无时长概念）。模式变化才重刷，
        值未变不碰 QAction（幂等零成本，tick 每拍调用无妨）；``None``/未知值
        归一化回 ``"pomodoro"``（fail-safe = 维持既有行为）。
        """
        m = "stopwatch" if _is_stopwatch_mode(mode) else "pomodoro"
        if m == getattr(self, "_focus_mode", None):
            return
        self._focus_mode = m
        self._apply_focus_menu()

    def set_focus_active(self, active: bool) -> None:
        """v0.25.2 兼容包装：只区分「跑没跑」（等价于 ``set_focus_phase``）。

        保留既有调用兼容（0.25.2–0.27.0 的 app 调用点）；粗粒度映射下
        「提前完成」在休息段会随 active=True 可用——模型侧 no-op 兜底，
        新调用点一律用 ``set_focus_phase`` 传真实相位。
        """
        self.set_focus_phase("focus" if active else _PHASE_IDLE)

    @staticmethod
    def _make_icon(alert: bool = False) -> QIcon:
        """爪印托盘图标；alert=True 时右上角加红点（v0.19.2 F9 需求警示）。"""
        pix = QPixmap(32, 32)
        pix.fill(QColor(0, 0, 0, 0))
        p = QPainter(pix)
        p.setRenderHint(QPainter.Antialiasing)
        p.setBrush(QColor("#FFB400"))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(3, 3, 26, 26)
        p.setPen(QColor("white"))
        font = QFont()
        font.setPointSize(16)
        p.setFont(font)
        p.drawText(pix.rect(), Qt.AlignmentFlag.AlignCenter, "🐾")
        if alert:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor("#FF3B30"))
            p.drawEllipse(20, 0, 12, 12)
            p.setBrush(QColor("white"))
            p.drawEllipse(22, 2, 3, 3)
        p.end()
        return QIcon(pix)

    def remove(self) -> None:
        # hide + deleteLater 释放托盘与菜单（旧版只 hide，QSystemTrayIcon 留到
        # app.quit 才回收；显式 deleteLater 让退出更干净，防二次 shutdown 重入）
        self._tray.hide()
        self._tray.deleteLater()
        if getattr(self, "_menu", None) is not None:
            self._menu.deleteLater()
