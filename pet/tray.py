"""系统托盘 —— 设计思路.md §2.5（退出走 shutdown）/ 平台适配与分工.md §二。

v0.1：``QSystemTrayIcon`` + “退出”菜单。退出回调走 app 层 ``shutdown()``。
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt, QObject
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

# v0.25.2 番茄入口文案（纯函数便于无 Qt 单测；运行态 → 下一动作语义）
_FOCUS_START_LABEL = "🍅 开始番茄钟"
_FOCUS_STOP_LABEL = "🍅 停止番茄钟"

# v0.27.1 番茄自由度扩展「入口层①：运行中控制」（wiki 设计-番茄自由度扩展技术
# 方案 §4.2 B1/B2/B3）：单一 toggle 项升级为「🍅 番茄钟」子菜单（▸ 由 Qt 按
# 子菜单自动绘制，故标题不带箭头字符）——开始/停止（toggle 语义逐字沿用）+
# ⏭ 跳过当前段 + ➕ 延长 5 分钟 + ✅ 提前完成。可用性由纯函数
# ``focus_availability`` 决定（**L1：IDLE 时后三项一律不可用**，从入口层再挡
# 一道，模型侧同类方法本就是 no-op）；``apply_focus_menu`` 只要求对象实现
# setText/setEnabled，故整条可用性逻辑可脱离 QApplication 单测。
_FOCUS_MENU_LABEL = "🍅 番茄钟"
_FOCUS_SKIP_LABEL = "⏭ 跳过当前段"
_FOCUS_EXTEND_LABEL = "➕ 延长 5 分钟"
_FOCUS_COMPLETE_LABEL = "✅ 提前完成"

_PHASE_IDLE = "idle"
_PHASES = ("idle", "focus", "short", "long")


def focus_label(active: bool) -> str:
    """番茄项文案：运行中显示「停止」（点击即 stop），否则显示「开始」。"""
    return _FOCUS_STOP_LABEL if active else _FOCUS_START_LABEL


def focus_phase_name(state) -> str:
    """相位归一化：``FocusPhase`` / 其 ``.value`` / 字符串 / None → 四个相位名。

    tray 刻意**不 import pet.focus**（托盘层不耦合节律器，与既有 duck-typing
    读口同风格）：只读 ``.value`` 或字符串。未知/None 一律按 ``idle`` 处理——
    fail-safe 方向是「更不可用」：宁可多灰一项，也不给 IDLE 下的误点留门。
    """
    v = getattr(state, "value", state)
    v = str(v).strip().lower() if v is not None else _PHASE_IDLE
    return v if v in _PHASES else _PHASE_IDLE


def focus_availability(state) -> dict:
    """相位 → 子菜单四项的文案与可用性（纯函数，§4.2 口径）。

    - ``toggle``：**恒可用**（文案沿用 ``focus_label``：「开始」↔「停止」）
    - ``skip``：非 IDLE 可用（专注/短休/长休；模型侧 IDLE 本就 no-op）
    - ``extend``：非 IDLE 可用（专注段与休息段均可延长，模型侧 IDLE no-op）
    - ``complete``：**仅 FOCUS 可用**——休息段没有「提前完成」语义，若可点会
      凭空 +1 番茄（模型侧已 no-op，此处是入口层双保险）
    """
    phase = focus_phase_name(state)
    active = phase != _PHASE_IDLE
    return {
        "phase": phase,
        "active": active,
        "toggle_label": focus_label(active),
        "skip": active,
        "extend": active,
        "complete": phase == "focus",
    }


def apply_focus_menu(actions: dict, state) -> dict:
    """把 ``focus_availability`` 施加到动作集合，返回该可用性字典。

    ``actions`` 的值只需实现 ``setText(str)`` / ``setEnabled(bool)``（真实
    QAction 或测试替身皆可），故本函数是纯逻辑，可脱离 QApplication 单测。
    缺项（缺键/None）静默跳过，便于兼容旧调用方。
    """
    avail = focus_availability(state)
    actions = actions or {}
    toggle = actions.get("toggle")
    if toggle is not None:
        toggle.setText(avail["toggle_label"])
        toggle.setEnabled(True)          # toggle 是唯一恒可用项
    for key in ("skip", "extend", "complete"):
        obj = actions.get(key)
        if obj is not None:
            obj.setEnabled(avail[key])
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
        self._on_focus_skip = None
        self._on_focus_extend = None
        self._on_focus_complete = None
        self._focus_phase = _PHASE_IDLE
        # 子菜单标题不带「▸」——mac/Qt 会按子菜单自动绘制箭头字符
        self._focus_menu = focus_menu = menu.addMenu(_FOCUS_MENU_LABEL)
        self._act_focus = act_focus = focus_menu.addAction(_FOCUS_START_LABEL)
        act_focus.triggered.connect(self._emit_focus)
        self._act_focus_skip = act_skip = focus_menu.addAction(
            _FOCUS_SKIP_LABEL)
        act_skip.triggered.connect(self._emit_focus_skip)
        self._act_focus_extend = act_ext = focus_menu.addAction(
            _FOCUS_EXTEND_LABEL)
        act_ext.triggered.connect(self._emit_focus_extend)
        self._act_focus_complete = act_cmp = focus_menu.addAction(
            _FOCUS_COMPLETE_LABEL)
        act_cmp.triggered.connect(self._emit_focus_complete)
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

    # ---- v0.25.2 番茄专注入口（v0.27.1 升级为子菜单） ----

    def set_focus_callback(self, cb) -> None:
        """托盘'🍅 开始/停止番茄钟'→ app._toggle_focus（toggle 语义单点）。"""
        self._on_focus = cb

    def _emit_focus(self) -> None:
        if self._on_focus is not None:
            self._on_focus()

    def set_focus_skip_callback(self, cb) -> None:
        """子菜单「⏭ 跳过当前段」→ app._skip_focus（FocusSession.skip 薄转发）。"""
        self._on_focus_skip = cb

    def set_focus_extend_callback(self, cb) -> None:
        """子菜单「➕ 延长 5 分钟」→ app._extend_focus（extend(5) 薄转发）。"""
        self._on_focus_extend = cb

    def set_focus_complete_callback(self, cb) -> None:
        """子菜单「✅ 提前完成」→ app._complete_focus（complete() 薄转发，计入完成）。"""
        self._on_focus_complete = cb

    def _emit_focus_skip(self) -> None:
        if self._on_focus_skip is not None:
            self._on_focus_skip()

    def _emit_focus_extend(self) -> None:
        if self._on_focus_extend is not None:
            self._on_focus_extend()

    def _emit_focus_complete(self) -> None:
        if self._on_focus_complete is not None:
            self._on_focus_complete()

    def _apply_focus_menu(self) -> None:
        """按当前相位刷新子菜单文案/可用性（幂等；相位未变时上层不会调用）。"""
        apply_focus_menu({
            "toggle": getattr(self, "_act_focus", None),
            "skip": getattr(self, "_act_focus_skip", None),
            "extend": getattr(self, "_act_focus_extend", None),
            "complete": getattr(self, "_act_focus_complete", None),
        }, getattr(self, "_focus_phase", _PHASE_IDLE))

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
