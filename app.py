"""桌宠入口 —— 设计思路.md §2.5（单实例锁 + shutdown 七步序）。

``APP_VERSION``：当前构建标识（日志首行打印，用于确认运行的是哪一版——
单实例锁会让第二次启动静默退出，肉眼看旧进程容易误判"没修好"）。


**平台库-free**：本文件不 import fcntl/pyobjc/sensor_mac/window_mac，所有平台
特定（单实例锁 / dock 隐藏 / 传感器 / 浮窗创建）经 ``platform.py`` 注入。

v0.2：接 ``PetStateStore``（load 启动 / save debounce+定时+shutdown）+ 1s
衰减 QTimer + 点击交互（window signal ``patRequested``/``feedRequested``/...
→ ``store.update`` + 气泡）+ ``on_change`` 订阅 window（切 emoji）/ behavior
（调制）/ save 各一次。**v0.2 共享交互入口取 win 端 signal 版**，store 接线
（mac 主笔 PetStateStore）嫁接其上（``_interact`` 用 ``store.update`` 而非
静态 ``dataclasses.replace``）。
"""

from __future__ import annotations

import os
import sys
import threading
# 抑制 macOS 系统日志噪音：OS_ACTIVITY_MODE + stderr 过滤管道（TSM/IMK 输入法
# mach port 日志经 stderr NSLog，OS_ACTIVITY_MODE 不覆盖，过滤管道拦截 TSM/IMK 行）
os.environ.setdefault("OS_ACTIVITY_MODE", "disable")
import warnings
warnings.filterwarnings(
    "ignore", message=".*urllib3 v2 only supports OpenSSL.*"
)

# stderr 过滤管道：过滤 macOS TSM/IMK 系统日志行（第一次 TextField 键入触发），
# 保留 Python logging/traceback。dup2 fd2→管道，daemon 线程过滤后写原 stderr。
# v0.6.3：仅 mac 启用（TSM/IMK 是 macOS 专有，win 上无意义却 dup2 重定向 stderr）
if sys.platform == "darwin":
    _orig_stderr_fd = os.dup(2)
    _r, _w = os.pipe()
    os.dup2(_w, 2)
    def _filter_stderr():
        buf = b""
        while True:
            try:
                chunk = os.read(_r, 4096)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                t = line.decode(errors="replace")
                if "TSM AdjustCapsLock" in t or "IMKCFRunLoopWakeUpReliable" in t:
                    continue
                try:
                    os.write(_orig_stderr_fd, line + b"\n")
                except OSError:
                    pass
    threading.Thread(target=_filter_stderr, daemon=True).start()

import argparse
import logging
import signal

from PySide6.QtCore import QTimer
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import QApplication

from pet.asset_provider import EmojiProvider
from pet.behavior import ActionType, BehaviorFSM
from pet.bubble import BubbleType, BubbleWidget
from pet.config import load_config
from pet.logging_setup import setup_logging
from pet.llm import DeepSeekClient
from pet.pet_state import PetStateStore, Stage
from pet.platform import get_platform_adapter
from pet.tools_schema import ToolContext, ToolRegistry
from pet.tray import TrayManager

APP_VERSION = "v0.7.4+win"

_SAVE_DEBOUNCE_MS = 500       # 变更后 500ms 内多次只存一次
_SAVE_PERIODIC_MS = 30_000    # 定时存档
_DECAY_INTERVAL_MS = 1000     # 衰减 1s 一次（wall-clock delta）

# 交互：kind → (数值字段, 气泡文案)
_INTERACT_FIELD = {
    "pet": "mood",
    "feed": "fullness",
    "clean": "cleanliness",
    "poke": "mood",
}
_INTERACT_MSG = {
    "pet": "摸摸头～",
    "feed": "吃饱啦！",
    "clean": "洗得香香的～",
    "poke": "别戳啦…",
}


class PetApp:
    def __init__(self, argv, adapter, verbose: bool):
        self.adapter = adapter
        self.logger = logging.getLogger("pet")

        self.app = QApplication.instance() or QApplication(argv)
        self.app.setQuitOnLastWindowClosed(False)
        adapter.hide_dock_icon()  # mac 特定 / win no-op

        paths = adapter.get_paths()
        self.cfg = load_config(paths["config_path"])
        # config log_level 校准 logger 级别（main 里 setup_logging 用默认 INFO）
        if not verbose and self.cfg.get("log_level"):
            lvl_map = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40}
            self.logger.setLevel(lvl_map.get(
                str(self.cfg["log_level"]).upper(), 20))
        self._state_path = os.path.join(paths["data_dir"], "pet_state.json")

        # 养成 store：启动 load（无存档→default）；重启数值一致靠此
        self.store = PetStateStore.load(self._state_path)
        self.provider = EmojiProvider()
        self._gains = dict(self.cfg.get("interaction_gain", {}))

        self.sensors = adapter.get_sensors()  # 注入式，不直 import sensor_mac
        wa = self.sensors.work_area
        self.fsm = BehaviorFSM(dict(wa), self.cfg.get("behavior", {}))

        self.window = adapter.create_pet_window(  # 注入式，不直 import window_mac
            self.provider.get_static(self.store.get())
        )
        self.window.set_sprite_provider(self.provider)
        # v0.3.12 真实身位高喂 FSM（净空钻行判定；阶段进化变尺寸时更新）
        self.fsm.set_pet_height(self.window.height())
        self.store.on_change(lambda _s: self.fsm.set_pet_height(self.window.height()))

        cx = wa.get("x", 0) + wa.get("width", 0) / 2
        bottom = wa.get("y", 0) + wa.get("height", 0)
        self.window.move_bottom_center(cx, bottom)
        self.window.show()

        # v0.2 交互入口（win signal 版，§2.3 手势消解在共享 WindowBase）
        self.window.patRequested.connect(lambda: self._interact("pet"))
        self.window.feedRequested.connect(lambda: self._interact("feed"))
        self.window.cleanRequested.connect(lambda: self._interact("clean"))
        self.window.pokeRequested.connect(lambda: self._interact("poke"))
        self.window.quitRequested.connect(self.shutdown)
        # v0.3 拖拽（拖动直接挪窗保跟手，FSM 记录位置/速度）+ 跟随开关
        self.window.dragStarted.connect(self._on_drag_started)
        self.window.dragMoved.connect(self._on_drag_moved)
        self.window.dragReleased.connect(self._on_drag_released)
        self.window.followToggleRequested.connect(
            lambda: self._fsm_event("follow_toggle")
        )
        # v0.3 气泡跟随宠物（§2.4 头顶 20px / 靠顶翻下）
        self.window.petMoved.connect(self._on_pet_moved)

        self.bubble = BubbleWidget()
        # 图层探针排除自身（宠物站窗顶时探针点被自己身体覆盖 → 误否决支撑）
        self.adapter.register_own_windows(self.window, self.bubble)
        self.tray = TrayManager(on_quit=self.shutdown, parent=self.app)
        self.tray.set_reset_callback(self._on_reset_requested)

        # v0.4 聊天：key 引导 + DS 客户端 + 工具注册表 + QML 面板
        self._chat_engine = None
        self._chat_bridge = None
        self._chat_window = None
        self._chat_client = None
        self._setup_chat()

        # v0.6 主动关怀（win 主笔）+ v0.7 吃鼠标（mac）：
        # 30s 轮询；气泡锚宠物；idle 用传感器；有 DS key 时链式唤醒走 LLM
        # 隔离决策，否则本地罐头。v0.7 注入平台 mouse_lock + 四门禁检查器 +
        # FSM 事件派发（共享 ProactiveScheduler 零平台库，经 adapter 注入）。
        from pet.proactive import ProactiveScheduler

        proactive_cfg = self.cfg.get("proactive", {})
        self._proactive = ProactiveScheduler(
            store=self.store,
            bubble_fn=lambda t: self.bubble.show(t, anchor=self._pet_anchor()),
            idle_fn=lambda: self.sensors.idle_time,
            client=getattr(self, "_chat_client", None),
            cfg=proactive_cfg,
            mouse_lock=self.adapter.get_mouse_lock(),
            # mac DND v0.7 走 config 手动开关（proactive.dnd）；osascript
            # 专注模式检测留 v0.7.1（T6 config 路径已满足 Must）
            dnd_fn=None,
            active_content_fn=lambda: self.adapter.is_active_content(
                proactive_cfg.get("video_apps")
            ),
            accessibility_fn=self.adapter.is_accessibility_trusted,
            fsm_event_fn=lambda ev: self.fsm.handle_event(ev),
            prompt_accessibility_fn=self.adapter.prompt_accessibility,
        )
        # v0.7 托盘「强制吐出」→ EatMouseSession.force_spit（停 CGEventTap + 回 idle）
        self.tray.set_spit_callback(self._proactive.force_spit)
        self._proactive_timer = QTimer(self.app)
        self._proactive_timer.timeout.connect(
            lambda: self._proactive.poll()
        )
        self._proactive_timer.start(30_000)

        # save：debounce（变更后 500ms）+ 定时 30s + shutdown
        self._save_timer = QTimer(self.app)
        self._save_timer.setSingleShot(True)
        self._save_timer.timeout.connect(self._save_now)
        self._periodic_save_timer = QTimer(self.app)
        self._periodic_save_timer.timeout.connect(self._save_now)
        self._periodic_save_timer.start(_SAVE_PERIODIC_MS)

        # on_change 订阅：window（切 emoji）/ behavior（数值调制）/ save（debounce）
        self.store.on_change(self.window.on_state_change)
        self.store.on_change(self.fsm.on_state_change)
        self.store.on_change(self._on_state_changed_persist)
        # 用当前 state 调制一次（启动即对齐数值，不等首次衰减）
        self.fsm.on_state_change(self.store.get())

        # 气泡骨架自检（证明 BubbleWidget.show(text) 能显示文字，挂宠物头顶）
        QTimer.singleShot(
            1500,
            lambda: self.bubble.show("我醒啦～", anchor=self._pet_anchor()),
        )

        # 传感器慢刷新（2s），FSM 快 tick（50ms），衰减 1s（wall-clock delta），
        # 全屏检测 1s（独立于传感器缓存，缩短可拖/可见窗口期）
        self._sensor_timer = QTimer(self.app)
        self._sensor_timer.timeout.connect(self._refresh_sensors)
        self._sensor_timer.start(2000)

        self._fullscreen_timer = QTimer(self.app)
        self._fullscreen_timer.timeout.connect(self._check_fullscreen)
        self._fullscreen_timer.start(1000)

        self._tick_timer = QTimer(self.app)
        self._tick_timer.timeout.connect(self._tick)
        self._tick_timer.start(50)

        self._decay_timer = QTimer(self.app)
        self._decay_timer.timeout.connect(self._apply_decay)
        self._decay_timer.start(_DECAY_INTERVAL_MS)
        # 启动即补一次离线期间的衰减（load 读回 last_update → now 的 wall-clock）
        self._apply_decay()

        # 让 Python 能响应 SIGINT（开发期 Ctrl-C 干净退出）
        self._sig_timer = QTimer(self.app)
        self._sig_timer.timeout.connect(lambda: None)
        self._sig_timer.start(200)
        signal.signal(signal.SIGINT, lambda *_: self.shutdown())

    def _pet_anchor(self) -> tuple:
        """气泡锚点：宠物当前 bottom_center + 窗口高。"""
        x, y = self.fsm.pos
        return (x, y, self.window.height())

    # ---- v0.4 聊天 ----
    def _setup_chat(self) -> None:
        """DS key 引导 + 客户端 + 工具注册表 + QML 面板。

        无 key → 气泡提示首次引导（QInputDialog）；仍无 key/env → 聊天禁用，
        宠物仍跑（T8 离线/无 key 不阻塞）。ToolRegistry 只注册 open_app（v0.4），
        confirm_fn 经 platform 注入（NSAlert，open_app 不危险不触发）。
        """
        api_key = self._ensure_ds_key()
        if not api_key:
            self.logger.warning("DS key 未设置，聊天禁用（宠物仍跑）")
            QTimer.singleShot(
                1500,
                lambda: self.bubble.show(
                    "还没设置 DS key，聊天暂时不可用～",
                    anchor=self._pet_anchor(),
                ),
            )
            return

        # 工具注册表：mac open_app（v0.4）；confirm 经 platform 注入（NSAlert）
        registry = ToolRegistry(confirm_fn=self.adapter.confirm_dangerous)
        if sys.platform == "darwin":
            from pet.tools_mac import build_mac_tools

            for schema, handler in build_mac_tools():
                registry.register(schema, handler)
        elif sys.platform == "win32":
            from pet.tools_win import build_win_tools

            for schema, handler in build_win_tools():
                registry.register(schema, handler)

        self._chat_client = DeepSeekClient(api_key, registry)
        self._build_chat_panel(registry)

    def _ensure_ds_key(self) -> str | None:
        """首次启动 key 引导：platform.get_ds_key() 都无 → QInputDialog
        输入 → platform.set_ds_key() 存 Keychain。仍无 → 返 None（聊天禁用）。"""
        key = self.adapter.get_ds_key()
        if key:
            return key
        from PySide6.QtWidgets import QInputDialog

        text, ok = QInputDialog.getText(
            None,
            "设置 DeepSeek API Key",
            "请输入 DeepSeek API Key（存入系统密钥库，不写明文文件）：",
        )
        text = (text or "").strip()
        if ok and text:
            self.adapter.set_ds_key(text)
            return self.adapter.get_ds_key() or text
        return None

    def _build_chat_panel(self, registry) -> None:
        """载入 QML 聊天面板（不可见，托盘/聚焦唤出）。

        v0.6.3：先注册 fallback chat callback（_show_chat 会气泡提示未设 key），
        防 QML 载入抛异常时 tray callback 未注册致托盘聊天点击静默无反应。
        """
        import os

        # 先注册 fallback：即使 QML 载入失败，托盘聊天也有反馈
        self.tray.set_chat_callback(self._show_chat)
        from pet.ui.chat_bridge import ChatBridge, load_chat_panel

        qml_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "pet", "ui", "main.qml"
        )
        self._chat_bridge = ChatBridge(
            self._chat_client, registry, self._make_tool_context
        )
        self._chat_bridge.offlineRequested.connect(self._on_chat_offline)
        try:
            self._chat_engine = load_chat_panel(self._chat_bridge, qml_path)
            if self._chat_engine and self._chat_engine.rootObjects():
                self._chat_window = self._chat_engine.rootObjects()[0]
        except Exception as exc:
            self.logger.warning("QML 聊天面板载入失败，托盘聊天走气泡兜底: %s", exc)
        # v0.6 follow-up：用户消息含"去吃饭"等 → 30min 后回访（启发式）
        if self._chat_bridge is not None:
            self._chat_bridge.on_user_message = self._maybe_followup

    def _make_tool_context(self) -> ToolContext:
        """按需取当前 state 作为工具上下文（v0.4 工具不真用 state）。"""
        return ToolContext(
            pet_state=self.store.get(),
            user_name=self.cfg.get("user_name", "主人"),
            config=self.cfg,
            window_info=None,
        )

    _FOLLOWUP_RULES = (
        (("去吃饭", "吃午饭", "吃晚饭", "吃饭去"), "饭点到了～吃饱回来了吗？"),
        (("去洗澡",), "洗完舒服多了吧～"),
        (("睡一觉", "去睡觉", "去午睡"), "睡醒了吗？精神好点没～"),
    )

    def _maybe_followup(self, text: str) -> None:
        """v0.6：聊天消息启发式排 follow-up（30min 后回访气泡）。"""
        for keys, msg in self._FOLLOWUP_RULES:
            if any(k in text for k in keys):
                import time as _t

                self._proactive.follow_up(msg, _t.time() + 30 * 60)
                break

    def _show_chat(self) -> None:
        """托盘'聊天'唤出面板（v0.11 真全局热键占位）。

        全屏 app 在前台时，聊天面板移到桌面 Space（非全屏 Space）展示——
        像微信全屏下开聊天切到桌面。mac 用 NSWindow 的 collectionBehavior
        加入桌面 Space（canJoinAllSpaces + moveToCurrentSpace 降级到桌面）。"""
        if self._chat_window is None:
            self.bubble.show(
                "还没设置 DS key，聊天暂不可用～", anchor=self._pet_anchor()
            )
            return
        self._chat_bridge.reset_offline()
        # 全屏时聊天面板移到桌面 Space（mac 专属；win 无 Space 概念直接 raise）
        if getattr(self, "_fullscreen", False) and sys.platform == "darwin":
            self._move_chat_to_desktop_space()
        self._chat_window.show()
        self._chat_window.raise_()  # 点后跳最高层（不常置顶，失焦正常降层，像微信）
        self._chat_window.requestActivate()

    def _move_chat_to_desktop_space(self) -> None:
        """全屏时把聊天面板移到桌面 Space（非全屏 Space）。

        mac 全屏 app 独占一个 Space；聊天面板默认跟 app 走（在全屏 Space），
        会被全屏 app 盖住。改 NSWindow collectionBehavior 让它加入所有 Space
        但不跟全屏——用户切到桌面 Space 即可看到。"""
        try:
            from ctypes import c_void_p

            from objc import objc_object

            wid = int(self._chat_window.winId())
            view = objc_object(c_void_p=wid)
            nswin = view.window() if view is not None else None
            if nswin is None:
                return
            from AppKit import (
                NSWindowCollectionBehaviorCanJoinAllSpaces,
                NSWindowCollectionBehaviorStationary,
            )

            nswin.setCollectionBehavior_(
                NSWindowCollectionBehaviorCanJoinAllSpaces
                | NSWindowCollectionBehaviorStationary
            )
            self.logger.info("聊天面板移到桌面 Space（全屏模式下可见）")
        except Exception as exc:
            self.logger.warning("聊天面板移 Space 失败: %s", exc)

    def _on_chat_offline(self) -> None:
        """断网/无 key：气泡提示，宠物仍 WANDER/交互/长大（T8）。"""
        self.bubble.show(
            "当前离线，聊天暂不可用～",
            kind=BubbleType.WARNING,
            anchor=self._pet_anchor(),
        )

    def _on_pet_moved(self, x: float, y: float, h: int) -> None:
        self.bubble.follow((x, y, h))

    def _interact(self, kind: str) -> None:
        """v0.2 养成交互：window signal 触发 → store.update + 气泡。

        emoji 切换由 on_change 订阅（window.on_state_change）自动处理，不需
        主动 _refresh_sprite。衰减导致的 mood 变化也经 on_change 切 emoji。
        """
        field = _INTERACT_FIELD.get(kind)
        if field is None:
            return
        delta = float(self._gains.get(kind, 0))
        self.store.update(**{field: delta})
        msg = _INTERACT_MSG.get(kind)
        if msg:
            self.bubble.show(msg, anchor=self._pet_anchor())

    # ---- 衰减 / 持久化 ----
    def _apply_decay(self) -> None:
        # M2：离线补衰减前快照养护分——apply_decay 一次推过多个 age 阈值时，
        # check_evolve 循环补齐多阶，但每阶用离线前的快照分判分支（非衰减后
        # 的瞬时底值，否则离线多日回来连续进化全判 NEGLECTED 失真）。
        score_cfg = self.cfg.get("score", {})
        pre_state = self.store.get()
        pre_score = (
            float(score_cfg.get("mood_weight", 0.4)) * pre_state.mood
            + float(score_cfg.get("fullness_weight", 0.4)) * pre_state.fullness
            + float(score_cfg.get("cleanliness_weight", 0.2)) * pre_state.cleanliness
        )
        self.store.apply_decay(
            self.cfg.get("decay_per_hour", {}),
            age_speed_multiplier=self.cfg.get("age_speed_multiplier", 1.0),
        )
        # 循环 check_evolve 补齐离线多阶进化（旧版只调一次漏多阶，A6）
        while True:
            event = self.store.check_evolve(
                self.cfg.get("evolve_threshold_days", {}),
                score_cfg,
                avg_score=pre_score,
            )
            if event is None:
                break
            self._on_evolve(event)

    def _on_evolve(self, event: dict) -> None:
        """v0.5 进化可视化：气泡"我长大了"+ 阶段名。

        emoji/尺寸切换由 store.update(stage=, branch=) 触发的 on_change→
        window.on_state_change 自动完成（同 tick 同步）；此处补 FSM 身位高
        对齐新尺寸（on_change 回调顺序里 height lambda 先于 window 切换，
        故这里显式刷一次防净空钻行误判）。气泡一次，不每 tick 刷屏
        （check_evolve 跨阈值后下一 tick stage 已进阶即返 None）。
        """
        names = {"young": "幼年", "adult": "成年", "final": "终形态"}
        to = event.get("to_stage")
        msg = f"我长大了！现在进入{names.get(to, '新阶段')}了～"
        self.fsm.set_pet_height(self.window.height())
        self.bubble.show(msg, kind=BubbleType.INFO, anchor=self._pet_anchor())

    def _on_reset_requested(self) -> None:
        """v0.5 重置：托盘'重新开始'→NSAlert 二次确认→删档→in-process 复位。

        不走 execv 重启进程（单实例锁 fd 跨 exec 仍持有，新实例会判"已有
        实例"自退出）；改 in-process 复位：删 pet_state.json+.bak →
        store.reset() 回 default → on_change 同步切回 YOUNG/HEALTHY/尺寸 64
        + debounce 存回 default。取消确认则不动存档。
        """
        ok = self.adapter.confirm_dangerous(
            "重新开始", "清空存档重启", "当前宠物数据将丢失，不可恢复"
        )
        if not ok:
            return
        for p in (self._state_path, self._state_path + ".bak"):
            try:
                os.remove(p)
            except OSError:
                pass
        self.store.reset()
        self.fsm.set_pet_height(self.window.height())
        self.bubble.show("我重新出生啦～age 归零，从幼年重新开始！",
                         kind=BubbleType.WARNING, anchor=self._pet_anchor())

    def _on_state_changed_persist(self, _state) -> None:
        # debounce：500ms 内多次变更只存一次
        if not self._save_timer.isActive():
            self._save_timer.start(_SAVE_DEBOUNCE_MS)

    def _save_now(self) -> None:
        try:
            self.store.save(self._state_path)
        except Exception:
            self.logger.exception("存档失败")

    def _refresh_sensors(self) -> None:
        self.sensors = self.adapter.get_sensors()

    def _check_fullscreen(self) -> None:
        """v0.3 全屏/演示检测（1s 轮询，双次确认去抖）：
        前台全屏 → 隐藏 + 暂停/收敛 FSM；退出单次确认即恢复。"""
        try:
            fs = self.adapter.is_fullscreen_active()
        except NotImplementedError:
            fs = False  # 平台未实现（mac 待补）不抑制
        if fs:
            self._fs_hits = getattr(self, "_fs_hits", 0) + 1
        else:
            self._fs_hits = 0
        was = getattr(self, "_fullscreen", False)
        # 隐藏需连续 2 次命中（防前台切换瞬间的假全屏闪烁）；
        # 恢复单次否决即触发（宁可快恢复可见）
        if not was and fs and self._fs_hits >= 2:
            self._fullscreen = True
            self.fsm.handle_event("fullscreen_on")
            self.window.hide()
            self.logger.info("全屏检测：隐藏宠物")
        elif was and not fs:
            self._fullscreen = False
            self.fsm.handle_event("fullscreen_off")
            self.window.show()
            self.logger.info("全屏检测：恢复显示")

    # ---- v0.3 拖拽 ----
    def _on_drag_started(self, x: float, y: float) -> None:
        self.bubble.hide()  # 拖动开始关气泡（含 WARNING 永久停留的，防拖动跟随"再现"）
        self.fsm.begin_drag((x, y))
        self.window.move_bottom_center(x, y)

    def _on_drag_moved(self, x: float, y: float) -> None:
        self.fsm.drag_move((x, y))
        self.window.move_bottom_center(x, y)  # 直接挪窗保跟手

    def _on_drag_released(self, x: float, y: float) -> None:
        self.fsm.end_drag()

    def _fsm_event(self, event: str) -> None:
        self.fsm.handle_event(event)

    def _tick(self) -> None:
        # 可见性看门狗：非全屏被隐藏（异常/竞态）→ 立即恢复并留痕
        if not getattr(self, "_fullscreen", False) and not self.window.isVisible():
            self.window.show()
            self.logger.warning("宠物窗口异常隐藏，看门狗已恢复")
        # 实时鼠标：sensors.mouse_pos 走 2s 缓存会致跟随按旧位置走（A→B→C 折返路径感）；
        # 每 50ms tick 实时取 QCursor 塞 sensors.mouse_pos，跟随即跟当前指针
        mp = QCursor.pos()
        self.sensors.mouse_pos = (mp.x(), mp.y())
        action = self.fsm.step(self.store.get(), self.sensors, 0.05)
        if action.type == ActionType.ANIMATE and action.params.get("name"):
            self._play_animate(action.params["name"])
        # v0.7.3 两段式吃鼠标：FSM 奔到光标（EAT_APPROACH→EAT_MOUSE 转换）
        # 才真正启动抑制；eat_mouse_tick 处理追赶超时兜底
        mode = self.fsm.mode
        if mode == "eat_mouse" and getattr(self, "_fsm_last_mode", "")                 != "eat_mouse":
            self._proactive.eat_mouse_arrived()
        self._fsm_last_mode = mode
        try:
            self._proactive.eat_mouse_tick()
        except Exception:
            pass
        # 无条件按 FSM.pos 同步窗口位置：MOVE_TO/FALL 之外还有**静默位移**
        # （骑乘跟随移动窗口发生在 IDLE，step 返回 ANIMATE）——只听 action
        # 会漏掉这类位移，视觉上宠物悬空在旧高度。Qt 同坐标 move 是 no-op，
        # 每 tick 调用无代价。
        self.window.move_bottom_center(*self.fsm.pos)

    # ---- v0.3 动画 ----
    def _play_animate(self, name: str) -> None:
        """随机小动作：get_frames 3 帧循环一轮（emoji 占位，~450ms）后回静帧。"""
        state = self.store.get()
        frames = self.provider.get_frames(state, ActionType.ANIMATE)
        self.window.play_frames(frames)

    def shutdown(self) -> None:
        """七步序（§2.5）；v0.2 起 ④保存 PetState 有实体。

        v0.6.2：幂等（_shutdown_done 标志防二次触发崩）；停全部 QTimer（旧版
        只停 proactive_timer，其余靠 app.quit 后事件循环停止，但二次触发时
        正在飞的回调可能访问已关闭资源）。
        """
        if getattr(self, "_shutdown_done", False):
            return  # 幂等：二次触发直接返回
        self._shutdown_done = True
        # ① 停全部 QTimer（proactive/save/sensor/fullscreen/tick/decay/sig）
        for name in ("_proactive_timer", "_periodic_save_timer", "_save_timer",
                     "_sensor_timer", "_fullscreen_timer", "_tick_timer",
                     "_decay_timer", "_sig_timer"):
            t = getattr(self, name, None)
            if t is not None:
                try:
                    t.stop()
                except Exception:
                    pass
        # ② v0.7 释放 EatMouseSession（停 CGEventTap + 回 idle）——v0.2.5 起占位
        # pass，v0.7 实体化。force_spit 幂等，未在吃也安全。
        if getattr(self, "_proactive", None) is not None:
            try:
                self._proactive.force_spit()
            except Exception:
                self.logger.warning("shutdown 释放 EatMouseSession 异常",
                                    exc_info=True)
        # ③ 全局热键 —— v0.11（v0.7 强制吐出热键由 mouse_lock_mac 键盘
        # listen tap 承载，随 EatMouseSession 释放一并停止）
        # ④ 保存 PetState+Memory
        self._save_now()
        # ⑤ 关 QML engine（v0.4 聊天面板）+ 中断流式 worker（防线程泄漏）
        if self._chat_bridge is not None:
            try:
                self._chat_bridge.cancel()
            except Exception:
                pass
        if self._chat_window is not None:
            try:
                self._chat_window.close()
            except Exception:
                pass
        self._chat_engine = None
        # ⑥ 移除托盘
        self.tray.remove()
        # ⑦ QApplication.quit()
        self.app.quit()

    def run(self) -> int:
        return self.app.exec()


def main() -> int:
    parser = argparse.ArgumentParser(description=f"桌宠 {APP_VERSION}")
    parser.add_argument(
        "--verbose", action="store_true", help="详细日志到 stderr"
    )
    args = parser.parse_args()

    adapter = get_platform_adapter()
    paths = adapter.get_paths()
    logger = setup_logging(args.verbose, paths["log_dir"])
    logger.info("启动桌宠 %s（verbose=%s）", APP_VERSION, args.verbose)

    if not adapter.acquire_single_instance_lock():
        logger.info("已有实例运行，本进程退出。")
        return 0

    pet = PetApp(sys.argv, adapter, verbose=args.verbose)
    return pet.run()


if __name__ == "__main__":
    raise SystemExit(main())
