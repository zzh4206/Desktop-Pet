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
import json
import logging
import signal

from PySide6.QtCore import QTimer
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import QApplication

from pet import __version__ as PKG_VERSION
from pet.asset_provider import AIArtProvider, EmojiProvider
from pet.behavior import ActionType, BehaviorFSM
from pet.bubble import BubbleType, BubbleWidget
from pet.config import load_config
from pet.logging_setup import setup_logging
from pet.llm import create_client  # v0.4.15 工厂（不再硬编码 DeepSeekClient）
from pet.pet_state import Mood, PetStateStore, Stage
from pet.platform import get_platform_adapter
from pet.tools_schema import ToolContext, ToolRegistry
from pet.tray import TrayManager

# 版本单一源 = pet/__init__.__version__（L2 治理：旧版三处硬编码漂移到
# v0.7.4+win / 0.9.3 / 幻影 v0.12.1 注释）。发版只改 pet/__init__.py。
APP_VERSION = f"v{PKG_VERSION}"

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

_CHAT_EMOTION_BUBBLES = {
    "happy": ("太好了，替你开心～", "听起来真棒！", "今天有好消息呀～"),
    "neutral": ("我在这儿陪着你～", "慢慢来就好。", "今天也一起加油～"),
    "sad": ("抱抱你，难过也没关系。", "我会在这里听你说。", "先对自己温柔一点～"),
    "sleepy": ("辛苦啦，早点休息吧～", "慢一点，今晚好好放松。", "困了就和我一起歇会儿～"),
    "hungry": ("别忘了吃点东西呀～", "先补充一点能量吧。", "喝口水、吃点热乎的～"),
}


class PetApp:
    def __init__(self, argv, adapter, verbose: bool):
        self.adapter = adapter
        self.logger = logging.getLogger("pet")

        self.app = QApplication.instance() or QApplication(argv)
        self.app.setQuitOnLastWindowClosed(False)
        adapter.hide_dock_icon()  # mac 特定 / win no-op

        paths = adapter.get_paths()
        self._paths = paths   # v0.9.2(H1 修)：_setup_chat 等方法可引用
        self.cfg = load_config(paths["config_path"])
        # config log_level 校准 logger 级别（main 里 setup_logging 用默认 INFO）
        if not verbose and self.cfg.get("log_level"):
            lvl_map = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40}
            self.logger.setLevel(lvl_map.get(
                str(self.cfg["log_level"]).upper(), 20))
        self._state_path = os.path.join(paths["data_dir"], "pet_state.json")
        # 情绪上下文是独立于养成存档的隐私最小化档案，仅含最近用户消息。
        self._chat_emotion_cfg = dict(self.cfg.get("chat_emotion", {}))
        self._chat_emotion_store = None
        self._chat_emotion_engine = None
        self._chat_emotion_active = None
        if self._chat_emotion_cfg.get("enabled", True):
            from pet.chat_emotion import ChatEmotionEngine, ConversationEmotionStore
            self._chat_emotion_store = ConversationEmotionStore(
                os.path.join(paths["data_dir"], "chat_emotion.json"),
                self._chat_emotion_cfg.get("retention_hours", 48),
            )
            model_root = os.path.join(os.path.dirname(__file__), "pet", "models")
            v2_path = os.path.join(model_root, "chat_emotion_v2")
            model_path = v2_path if os.path.isdir(v2_path) else os.path.join(model_root, "chat_emotion_v1.npz")
            self._chat_emotion_engine = ChatEmotionEngine(
                model_path, self._chat_emotion_cfg.get("confidence_threshold", .55))

        # 养成 store：启动 load（无存档→default）；重启数值一致靠此
        self.store = PetStateStore.load(self._state_path)
        self._gains = dict(self.cfg.get("interaction_gain", {}))

        self.sensors = adapter.get_sensors()  # 注入式，不直 import sensor_mac
        # v0.10 provider 挂 idle_fn：idle 超时 → SLEEPY 立绘（_mood_from_state）
        self.provider = self._make_provider()
        wa = self.sensors.work_area
        self.fsm = BehaviorFSM(dict(wa), self.cfg.get("behavior", {}))

        # v0.13 展示后端选择：presentation=frames（默认，旧行为不变）| rig
        # （分层绑骨：交叉淡化+常驻微动+部件弹簧；资产/环境不满足自动回退，
        # 降级铁律收敛在 pet.rig.presenter.build_rig_window 一处）。
        # v0.14 paperdoll：第三档——在 rig 之上把行走改为部件驱动优先
        # （figure 挂 limb 部件时程序化正面步态，无 limb 自动回退帧路径）。
        # v0.13.3：defer_quick=True —— rig 引擎延至事件循环首拍，保证
        # _setup_chat 的 QML singleton 注册先于全进程首个 QML 引擎
        # （app.py:349 同源约束，否则聊天面板载入失败）。
        sprite0 = self.provider.get_static(self.store.get())
        presentation = self.cfg.get("presentation", "frames")
        if presentation in ("rig", "paperdoll"):
            from pet.rig.presenter import build_rig_window

            self.window = build_rig_window(
                adapter.create_pet_window, sprite0,
                self.store.get().stage.value, defer_quick=True)
        else:
            self.window = adapter.create_pet_window(sprite0)
        self._part_walk = presentation == "paperdoll"
        self.window.set_sprite_provider(self.provider)
        # v0.14.4 行走覆盖：行走期间改显部件步态载体 figure，停步还原
        # mood 立绘——否则行走静默回退 GPT 帧环，帧间烤死的手臂摆动/
        # 尾巴位移/色调差即实机报告的观感问题。
        # v0.14.6 载体优先级：侧身部件立绘（walk_0 像素拷贝+前后腿拆件，
        # 程序化侧身步态）→ 正面 neutral（正面踏步）→ None（帧行走回退）。
        def _walk_refresh(s) -> None:
            fig = None
            if self._part_walk:
                prov = self.provider
                if hasattr(prov, "side_walk_static"):
                    fig = prov.side_walk_static(s) \
                        or prov.neutral_static(s)
            self.window.set_walk_figure(fig)
        _walk_refresh(self.store.get())
        self.store.on_change(_walk_refresh)
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
        # v0.3 拖拽（拖动直接挪窗保跟手）+ 移动模式
        self.window.dragStarted.connect(self._on_drag_started)
        self.window.dragMoved.connect(self._on_drag_moved)
        self.window.dragReleased.connect(self._on_drag_released)
        self.window.motionModeRequested.connect(self._set_motion_mode)
        # v0.8 权限自检页：宠物右键"设置"唤出（win 运行时自检）
        self.window.settingsRequested.connect(self._show_perm)
        # v0.9 拖放文件给它打开（快捷启动器）
        self.window.fileDropped.connect(self._on_file_dropped)
        self._perm_window = None
        self._perm_engine = None
        self._perm_bridge = None
        self._mem_window = None
        self._mem_engine = None
        self._mem_bridge = None
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
        try:
            self._setup_chat()
        except Exception:
            self.logger.exception(
                "聊天初始化失败（key/tool/QML），宠物本体继续运行")

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
            # M5：独立决策客户端（见 _setup_chat 注释），不与聊天共享 _resp
            client=getattr(self, "_proactive_client", None),
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
            # 批次C（REVIEW-2026-08-28 H2）：吃鼠标第五门禁——前台全屏
            # （演示/放映）不抑制；到达点复查同函数
            fullscreen_fn=self.adapter.is_fullscreen_active,
        )
        # v0.7 托盘「强制吐出」→ EatMouseSession.force_spit（停 CGEventTap + 回 idle）
        self.tray.set_spit_callback(self._proactive.force_spit)
        # v0.9 记忆管理页唤出
        self.tray.set_mem_callback(self._show_mem)
        self.tray.set_chat_emotion_callback(self._show_chat_emotion_settings)
        self._proactive_timer = QTimer(self.app)
        self._proactive_timer.timeout.connect(
            lambda: self._proactive.poll()
        )
        self._proactive_timer.start(30_000)

        # 独立于主动关怀：只负责每日低频聊天情绪推理，纯主线程、无平台依赖。
        self._chat_emotion_timer = QTimer(self.app)
        self._chat_emotion_timer.timeout.connect(self._poll_chat_emotion)
        self._chat_emotion_timer.start(60_000)
        QTimer.singleShot(3000, self._poll_chat_emotion)

        # v0.11 全局热键（Ctrl+Alt+P 唤聊天 / Ctrl+Alt+T 吐出）
        self._setup_hotkeys()
        # v0.11 托盘自启切换
        self.tray.set_autostart_callback(self._toggle_autostart)
        self.tray.set_autostart_state(self.adapter.is_autostart_enabled())

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
        """v0.4.15 多 provider：扫描已注入 key 的 provider → 弹选（多个时）
        → create_client 工厂实例化 → 工具注册表 + QML 面板。

        无 key → 气泡提示首次引导（QInputDialog）；仍无 key → 聊天禁用，宠物仍跑。
        目前只 deepseek（首批验证中间层）；后续加 claude/openai 只在 create_client
        工厂加分支 + config providers 段加条目。"""
        providers_cfg = self.cfg.get("llm", {}).get("providers", {})
        # 扫描已注入 key 的 provider（Keychain + env）
        available = []
        for name, pcfg in providers_cfg.items():
            env_var = pcfg.get("api_key_env", "")
            key = self.adapter.get_llm_key(name, env_var)
            if key:
                available.append((name, key))

        if not available:
            # 首次引导：默认 deepseek（config 里有的第一个）
            default_name = next(iter(providers_cfg), "deepseek")
            default_env = providers_cfg.get(default_name, {}).get("api_key_env", "DEEPSEEK_API_KEY")
            key = self._ensure_llm_key(default_name, default_env)
            if not key:
                self.logger.warning("LLM key 未设置，聊天禁用（宠物仍跑）")
                QTimer.singleShot(
                    1500,
                    lambda: self.bubble.show(
                        "还没设置 API key，聊天暂时不可用～",
                        anchor=self._pet_anchor(),
                    ),
                )
                return
            available = [(default_name, key)]

        # 多 provider 时弹选（每次启动都弹）
        if len(available) == 1:
            selected, key = available[0]
        else:
            selected, key = self._select_provider(available)

        # 工具注册表
        registry = ToolRegistry(confirm_fn=self.adapter.confirm_dangerous)
        if sys.platform == "darwin":
            from pet.tools_mac import build_mac_tools

            for schema, handler in build_mac_tools():
                registry.register(schema, handler)
        elif sys.platform == "win32":
            from pet.tools_win import build_win_tools

            for schema, handler in build_win_tools():
                registry.register(schema, handler)
        # v0.9 长期记忆工具（共享，与平台工具并列；加工具不改 llm.py）
        from pet.memory import MemoryStore
        from pet.memory_tools import build_memory_tools

        self._memory_path = os.path.join(self._paths["data_dir"],
                                         "memory.json")
        self.memory = MemoryStore.load(self._memory_path)
        for schema, handler in build_memory_tools(self.memory):
            registry.register(schema, handler)

        # v0.9/v0.8 面板 singleton 预注册：必须在 _build_chat_panel 创建首个
        # QQmlApplicationEngine 前注册，否则 PySide6 6.10 下后注册的 singleton
        # 不被后续 engine 解析 → perm/mem.qml 报 "Cannot assign QQuickText
        # to list property data"。bridge 实例留存，_show_mem/_show_perm 复用。
        from pet.ui.mem_bridge import MemBridge, register_mem_singleton
        self._mem_bridge = MemBridge(self.memory, self._save_memory)
        register_mem_singleton(self._mem_bridge)
        if sys.platform == "darwin":
            from pet.ui.perm_bridge_mac import (
                PermBridgeMac, register_perm_singleton,
            )
            self._perm_bridge = PermBridgeMac(self.adapter)
            register_perm_singleton(self._perm_bridge)
        else:
            from pet.ui.perm_bridge import (
                PermBridge, register_perm_singleton,
            )
            self._perm_bridge = PermBridge(self.adapter)
            register_perm_singleton(self._perm_bridge)

        # v0.4.15 工厂实例化（不再硬编码 DeepSeekClient）
        from pet.llm import create_client

        self._chat_client = create_client(selected, key, registry, self.cfg)
        # H4/M5 修（REVIEW-2026-08-25）：滚屏摘要走独立客户端实例——共享
        # 实例的 _resp/usage 跨线程互踩（摘要与在飞聊天流并发时 cancel
        # 可能误关对方的流）。配置同源，仅多一个实例。
        self._sum_client = create_client(selected, key, registry, self.cfg)
        # M5 修收尾：主动关怀决策同样独立实例（ChatWorker 与
        # _ProactiveWorker 共享时 cancel/usage 同源竞态）。三个 worker
        # 各持一个客户端，_resp 互不误伤。
        self._proactive_client = create_client(
            selected, key, registry, self.cfg)
        self.logger.info("LLM provider: %s", selected)
        self._build_chat_panel(registry)

    def _select_provider(self, available) -> tuple:
        """QInputDialog 下拉选 provider（每次启动多个时弹）。返 (name, key)。"""
        from PySide6.QtWidgets import QInputDialog

        names = [n for n, _ in available]
        choice, ok = QInputDialog.getItem(
            None,
            "选择 LLM Provider",
            "选择本次使用的 AI 模型：",
            names,
            0,
            False,
        )
        if ok and choice:
            for n, k in available:
                if n == choice:
                    return (n, k)
        return available[0]

    def _ensure_llm_key(self, provider: str, env_var: str) -> str | None:
        """首次启动 key 引导：platform.get_llm_key() 都无 → QInputDialog
        输入 → platform.set_llm_key() 存 Keychain。仍无 → None。"""
        key = self.adapter.get_llm_key(provider, env_var)
        if key:
            return key
        from PySide6.QtWidgets import QInputDialog

        text, ok = QInputDialog.getText(
            None,
            f"设置 {provider} API Key",
            f"请输入 {provider} API Key（存入系统密钥库，不写明文文件）：",
        )
        text = (text or "").strip()
        if ok and text:
            self.adapter.set_llm_key(provider, text)
            return self.adapter.get_llm_key(provider, env_var) or text
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
            self._chat_client, registry, self._make_tool_context,
            sum_client=getattr(self, "_sum_client", None),
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
            self._chat_bridge.on_user_message = self._on_user_message

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

    def _on_user_message(self, text: str) -> None:
        """发消息前钩子：v0.9 记忆注入 + v0.6 follow-up 启发式。"""
        # 记忆注入：recall 按当前消息 → 刷 system prompt 记忆段
        try:
            from pet.memory_tools import memory_context

            seg = memory_context(self.memory, text)
            self._chat_client.set_memory_context(seg)
        except Exception:
            self.logger.warning("记忆注入异常", exc_info=True)
        self._maybe_followup(text)
        if self._chat_emotion_store is not None:
            try:
                self._chat_emotion_store.add_user_message(text)
                self._evaluate_message_emotion()
            except Exception:
                self.logger.warning("聊天情绪上下文写入失败", exc_info=True)

    def _evaluate_message_emotion(self) -> None:
        """每条新用户消息只在检测到明显情绪波动时立即换表情。"""
        from pet.chat_emotion import is_significant, obvious_emotion
        store, engine = self._chat_emotion_store, self._chat_emotion_engine
        if store is None or engine is None:
            return
        messages = store.recent_messages()
        # 即时状态以最新一句为主：旧的开心/难过不能把新表达反向覆盖。
        # v2 句向量模型必须先判断，不能被关键词规则短路；显式词仅保留给旧 v1 的安全回退。
        result = engine.evaluate(messages[-1:])
        if result.used_fallback and engine.version != 2 and messages:
            result = obvious_emotion(messages[-1]["text"]) or result
        if not is_significant(result, self._chat_emotion_cfg.get(
                "event_confidence_threshold", .75)):
            return
        # 即时情绪在下一条明确情绪出现前保持，避免表情在状态间横跳。
        store.set_current(result, None)
        self._apply_chat_emotion(result.label, result.confidence)

    def _poll_chat_emotion(self) -> None:
        """执行到期时段。模型/存档故障都只降级，不影响桌宠主循环。"""
        store, engine = self._chat_emotion_store, self._chat_emotion_engine
        if store is None or engine is None:
            return
        try:
            active = store.active_label()
            if active != self._chat_emotion_active:
                self._chat_emotion_active = active
                self.window.set_conversation_mood(Mood(active) if active else None)
            from pet.chat_emotion import EmotionResult, is_significant
            for slot in store.due_slots(self._chat_emotion_cfg.get("schedule", [])):
                result = engine.evaluate(store.recent_messages(), slot)
                # 22:00 是休息提醒：只有明确的非中性情绪才覆盖 sleepy。
                if slot == "22:00" and not is_significant(
                        result, self._chat_emotion_cfg.get("event_confidence_threshold", .75)):
                    result = EmotionResult("sleepy", 0.0, True, result.model_version)
                hours = float(self._chat_emotion_cfg.get("expression_hours", 2))
                store.set_current(result, __import__("time").time() + hours * 3600)
                store.mark_slot(slot)
                self._apply_chat_emotion(result.label, result.confidence)
        except Exception:
            self.logger.warning("聊天情绪推理失败", exc_info=True)

    def _apply_chat_emotion(self, label: str, confidence: float = 0.0) -> None:
        """短时表情立即可见；养成 mood 仅受限地轻微移动。"""
        try:
            mood = Mood(label)
        except ValueError:
            return
        try:
            self._chat_emotion_active = label
            self.window.set_conversation_mood(mood)
        except Exception:
            self.logger.warning("聊天情绪立绘更新失败", exc_info=True)
        delta = float(self._chat_emotion_cfg.get("mood_delta", {}).get(label, 0))
        if delta and confidence >= float(self._chat_emotion_cfg.get("confidence_threshold", .55)):
            self.store.update(mood=delta)
        import random
        self.bubble.show(random.choice(_CHAT_EMOTION_BUBBLES[label]), anchor=self._pet_anchor())

    def _maybe_followup(self, text: str) -> None:
        """v0.6：聊天消息启发式排 follow-up（30min 后回访气泡）。"""
        for keys, msg in self._FOLLOWUP_RULES:
            if any(k in text for k in keys):
                import time as _t

                self._proactive.follow_up(msg, _t.time() + 30 * 60)
                break

    def _make_provider(self):
        """v0.10：config provider 切 emoji/ai/commission（§六三级）。

        EmojiProvider 的 idle_fn 注入保留（SLEEPY 判定两种 provider 均用）；
        AIArtProvider 构造透传 idle/sleepy + 降级内嵌 EmojiProvider。
        commission 走同 AIArtProvider 路径（读 assets/ 同命名约定）。
        """
        kind = self.cfg.get("provider", "emoji")
        idle_fn = lambda: self.sensors.idle_time
        sleepy_s = self.cfg.get("sleepy_idle_minutes", 10) * 60
        if kind in ("ai", "commission"):
            from pet.asset_provider import AIArtProvider

            return AIArtProvider(idle_fn=idle_fn, sleepy_idle_s=sleepy_s)
        return EmojiProvider(idle_fn=idle_fn, sleepy_idle_s=sleepy_s)

    def _setup_hotkeys(self) -> None:
        """v0.11 全局热键注册 + 冲突气泡提示。"""
        def on_conflict(name, key):
            self.bubble.show(
                f"热键 {key}（{name}）被占用，请在 config 中改键～",
                kind=BubbleType.WARNING, anchor=self._pet_anchor(),
            )

        # M7 修：热键线程回调经 Qt Signal 转主线程（跨线程 GUI 是 UB）
        from PySide6.QtCore import QTimer

        hotkey_bridge = None
        # 批次E/L1：预定义——旧版只在 try 内赋值，ImportError 分支后若
        # start_hotkeys 成功会在下方 bubble 引用 NameError；except 也只抓
        # ImportError，抓不到模块级 WinDLL 加载失败的真实形态（OSError）
        _hk_hint = ""
        try:
            if sys.platform == "darwin":
                from pet.hotkey_mac import _HotkeySignalBridge
                _hk_hint = "Cmd+Option+P 聊天 / Cmd+Option+T 吐出"
            else:
                from pet.hotkey_win import _HotkeySignalBridge
                _hk_hint = "Ctrl+Alt+P 聊天 / Ctrl+Alt+T 吐出"
            hotkey_bridge = _HotkeySignalBridge()
            hotkey_bridge.fired.connect(self._on_hotkey_fired)
            # M12 修：注册冲突也走信号转主线程（win bridge 提供 conflict；
            # mac bridge 无此信号——Carbon 回调本在主线程，直调安全）
            conflict_sig = getattr(hotkey_bridge, "conflict", None)
            if conflict_sig is not None:
                conflict_sig.connect(on_conflict)
        except (ImportError, OSError, AttributeError):
            # 平台热键模块不可用/平台库加载失败 → bridge=None，回调直调
            pass

        ok = self.adapter.start_hotkeys(
            self.cfg,
            on_chat=self._toggle_chat_panel,     # bridge 为 None 时直调
            on_spit=lambda: self._proactive.force_spit(),
            on_conflict=on_conflict,
            bridge=hotkey_bridge,
        )
        if not ok:
            self.logger.warning("[热键] 全部注册失败")
        else:
            self.logger.info("[热键] 就绪（%s）", _hk_hint)

    def _on_hotkey_fired(self, hid: int) -> None:
        """M7：热键信号主线程分发（hid=1 聊天 / hid=2 吐出）。"""
        if hid == 1:
            self._toggle_chat_panel()
        elif hid == 2:
            self._proactive.force_spit()

    def _toggle_chat_panel(self) -> None:
        """Ctrl+Alt+P 唤出/隐藏聊天面板（v0.11 Must）。"""
        if self._chat_window is None:
            self._show_chat()
            return
        if self._chat_window.isVisible():
            self._chat_window.hide()
        else:
            self._show_chat()

    def _toggle_autostart(self, enabled: bool) -> None:
        """v0.11 托盘自启切换。L6 修：设置失败回滚托盘勾选（旧版失败只弹
        气泡，勾选态与真实状态脱节到重启）。"""
        ok = self.adapter.set_autostart(enabled)
        if not ok:
            self.tray.set_autostart_state(False)
        self.bubble.show(
            "开机自启已开启～" if ok and enabled else
            "开机自启已关闭" if ok else "自启设置失败",
            anchor=self._pet_anchor(),
        )

    def _show_mem(self) -> None:
        """v0.9 记忆管理页（托盘'记忆管理'唤出；查看/删除/清空）。

        bridge + PetMem singleton 已在 __init__ 预注册（首个 engine 前），
        此处只建 engine + 载入 QML，复用 self._mem_bridge。"""
        if self._mem_window is None:
            from pet.ui.mem_bridge import load_mem_qml

            self._mem_engine, self._mem_window = load_mem_qml()
        if self._mem_window is None:
            self.bubble.show("记忆页加载失败～", anchor=self._pet_anchor())
            return
        self._mem_bridge.refresh()
        self._mem_window.show()
        self._mem_window.raise_()
        self._mem_window.requestActivate()

    def _show_chat_emotion_settings(self) -> None:
        """跨平台 Qt 小设置窗；避免给共享情绪模块引入任何平台 UI 依赖。"""
        from PySide6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox,
                                       QFormLayout, QLineEdit, QMessageBox)
        dialog = QDialog()
        dialog.setWindowTitle("聊天情绪设置")
        layout = QFormLayout(dialog)
        enabled = QCheckBox("启用本地聊天情绪推理")
        enabled.setChecked(bool(self._chat_emotion_cfg.get("enabled", True)))
        schedule = QLineEdit(", ".join(self._chat_emotion_cfg.get("schedule", ["22:00"])))
        schedule.setPlaceholderText("例如：22:00")
        layout.addRow(enabled); layout.addRow("每天推理时段：", schedule)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        layout.addRow(buttons); buttons.accepted.connect(dialog.accept); buttons.rejected.connect(dialog.reject)
        if dialog.exec() != QDialog.Accepted:
            return
        slots = [part.strip() for part in schedule.text().replace("，", ",").split(",") if part.strip()]
        import re
        if not slots or any(not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", slot) for slot in slots):
            QMessageBox.warning(dialog, "聊天情绪设置", "时段请填写 HH:MM，例如 22:00。")
            return
        new_cfg = dict(self._chat_emotion_cfg); new_cfg["enabled"] = enabled.isChecked(); new_cfg["schedule"] = slots
        try:
            path = self._paths["config_path"]
            raw = {}
            if os.path.exists(path):
                with open(path, encoding="utf-8") as f: raw = json.load(f)
            raw["chat_emotion"] = new_cfg
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(raw, f, ensure_ascii=False, indent=2); f.flush(); os.fsync(f.fileno())
            os.replace(tmp, path)
            self._chat_emotion_cfg = new_cfg
            self.bubble.show("聊天情绪设置已保存，重启后完全生效～", anchor=self._pet_anchor())
        except Exception:
            self.logger.warning("聊天情绪设置保存失败", exc_info=True)
            QMessageBox.warning(dialog, "聊天情绪设置", "保存失败，请检查配置文件权限。")

    def _save_memory(self) -> None:
        """UI 删除/清空后即时落盘。"""
        try:
            self.memory.save(self._memory_path)
        except Exception:
            self.logger.exception("记忆存档失败")

    def _on_file_dropped(self, path: str) -> None:
        """v0.9 拖放文件/文件夹 → 平台 open_path + 气泡反馈。"""
        import os as _os

        if not path or not _os.path.exists(path):
            self.bubble.show("拖入的东西打不开～", anchor=self._pet_anchor())
            return
        ok, msg = self.adapter.open_path(path)
        self.bubble.show(msg if ok else f"打不开: {msg}",
                         anchor=self._pet_anchor())
        self.logger.info("[拖放] %s -> %s", path, "OK" if ok else msg)

    def _show_perm(self) -> None:
        """v0.8 权限自检页（mac 系统特权自检 / win 运行时能力自检；
        右键"设置"唤出）。darwin 载 perm_bridge_mac，win 载 perm_bridge，
        两者复用共享 perm.qml（经 note 属性注入平台头部文案）。

        bridge + PetPerm singleton 已在 __init__ 预注册（首个 engine 前），
        此处只建 engine + 载入 QML，复用 self._perm_bridge。"""
        if self._perm_window is None:
            if sys.platform == "darwin":
                from pet.ui.perm_bridge_mac import load_perm_qml
            else:
                from pet.ui.perm_bridge import load_perm_qml
            self._perm_engine, self._perm_window = load_perm_qml()
        if self._perm_window is None:
            self.bubble.show("权限页加载失败～", anchor=self._pet_anchor())
            return
        self._perm_bridge.refresh()   # 每次唤出即复检（§十二 聚焦刷新）
        self._perm_window.show()
        self._perm_window.raise_()
        self._perm_window.requestActivate()

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
        """全屏时把聊天面板移到桌面 Space——委托 adapter.move_window_to_all_spaces
        （mac NSWindow collectionBehavior；app.py 不直 import objc/AppKit）。"""
        ok = self.adapter.move_window_to_all_spaces(self._chat_window)
        if ok:
            self.logger.info("聊天面板移到桌面 Space（全屏模式下可见）")
        else:
            self.logger.warning("聊天面板移 Space 失败（platform 返 False）")

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
        # debounce：500ms 内多次变更只存一次。批次E/M1（REVIEW-2026-08-28）：
        # 变更即重启定时器（QTimer.start 对活跃的单发定时器=重置到期点）——
        # 旧版 isActive 不重启，衰减默认 2-6.5/h 让 1s tick 每秒判"实际
        # 变化"，恰好每 ~1.05s 全量落盘一次（json dump+fsync+bak+replace）
        # 持续 ~27h 直到三项触底，防抖名存实亡
        self._save_timer.start(_SAVE_DEBOUNCE_MS)

    def _save_now(self) -> None:
        try:
            self.store.save(self._state_path)
        except Exception:
            self.logger.exception("存档失败")
        # 批次D/F16（REVIEW-2026-08-28）：记忆随周期存档落盘——旧版仅
        # shutdown/记忆页操作触发，进程崩溃即丢整段会话学到的记忆
        # （违背 v0.9"跨会话不丢"Must）。见脏才写，空转零 IO。
        mem = getattr(self, "memory", None)
        if mem is not None and mem.dirty:
            try:
                self._save_memory()
            except Exception:
                self.logger.exception("记忆周期落盘失败")

    def _refresh_sensors(self) -> None:
        # 批次C/L10：传感器链（EnumWindows 回调等）任何异常不能从 timer 槽
        # 冒泡——每 2s 刷一条 traceback 不致死但污染日志
        try:
            self.sensors = self.adapter.get_sensors()
        except Exception:
            self.logger.warning("传感器刷新异常", exc_info=True)

    def _check_fullscreen(self) -> None:
        """v0.3 全屏/演示检测（1s 轮询，双次确认去抖）：
        前台全屏 → 隐藏 + 暂停/收敛 FSM；退出单次确认即恢复。"""
        try:
            fs = self.adapter.is_fullscreen_active()
        except NotImplementedError:
            fs = False  # 平台未实现（mac 待补）不抑制
        except Exception:
            # 批次C/L10：win 路径 ctypes 失败会抛其他类型——旧版只兜
            # NotImplementedError，其余每秒刷 traceback
            self.logger.warning("全屏检测异常", exc_info=True)
            fs = False
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
            # 批次C/H2：气泡是独立 Tool|StaysOnTop 窗，不随 window.hide()
            # 收——不藏则久坐提醒/链式唤醒照样盖在全屏演示上
            self.bubble.hide()
            self.logger.info("全屏检测：隐藏宠物（含气泡）")
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

    def _set_motion_mode(self, mode: str) -> None:
        self.fsm.handle_event(f"motion_mode:{mode}")
        self.window.set_motion_mode(self.fsm.motion_mode)

    def _tick(self) -> None:
        # 可见性看门狗：非全屏被隐藏（异常/竞态）→ 立即恢复并留痕
        if not getattr(self, "_fullscreen", False) and not self.window.isVisible():
            self.window.show()
            self.logger.warning("宠物窗口异常隐藏，看门狗已恢复")
        # 实时鼠标：sensors.mouse_pos 走 2s 缓存会致跟随按旧位置走（A→B→C 折返路径感）；
        # 每 50ms tick 实时取 QCursor 塞 sensors.mouse_pos，跟随即跟当前指针
        mp = QCursor.pos()
        self.sensors.mouse_pos = (mp.x(), mp.y())
        # 批次E/L12：实测间隔喂物理（钳 [0.01, 0.25]s）——旧版硬编码 0.05，
        # 右键菜单/QInputDialog 等模态期间 QTimer 暂停，恢复后一拍仍只走
        # 0.05s 物理时间（挂起 10s 宠物只老 0.05s），与衰减的 wall-clock
        # 策略不一致；钳上限防恢复瞬间 dt 过大把宠物瞬移/穿墙
        import time as _time
        now = _time.monotonic()
        dt = min(0.25, max(0.01, now - getattr(self, "_last_tick_at", now)))
        self._last_tick_at = now
        action = self.fsm.step(self.store.get(), self.sensors, dt)
        if action.type == ActionType.ANIMATE and action.params.get("name"):
            self._play_animate(action.params["name"])
        # v0.7.3 两段式吃鼠标：FSM 奔到光标（EAT_APPROACH→EAT_MOUSE 转换）
        # 才真正启动抑制；eat_mouse_tick 处理追赶超时兜底
        mode = self.fsm.mode
        if mode == "eat_mouse" and getattr(self, "_fsm_last_mode", "")                 != "eat_mouse":
            self._proactive.eat_mouse_arrived()
        # v0.13 rig 呈现层运动参数：速度倾斜/行走律动/空中标志/步频（frames
        # 后端的基类 no-op 缺省让该调用在旧模式下零成本旁路）。
        # v0.14.4 刻意先于 _frame_tick：walking 上升沿在此改显 neutral 覆盖
        # 图，_frame_tick 的 part_walk_active 查询才能当拍生效（否则首拍
        # 误播 walk 帧、下一拍再停）。
        if getattr(self.window, "rig_active", False):
            vx, _vy = self.fsm.velocity
            tilt = max(-9.0, min(9.0, vx / 140.0))
            walking = (mode == "walk"
                       or (mode == "idle" and self.fsm.motion_mode == "follow"))
            # v0.14 步频随速度：walk_speed 120px/s≈1.2Hz、follow 600≈2Hz 上限
            hz = max(0.9, min(2.0, 0.9 + abs(vx) / 400.0))
            self.window.set_motion_params(
                tilt_deg=tilt, walking=walking, walk_hz=hz,
                airborne=mode in ("fall", "thrown", "drag"))
        # v0.10.15 状态驱动帧动画（行走交替/下落/落地瞬帧/咀嚼循环）
        self._frame_tick(action, mode, getattr(self, "_fsm_last_mode", ""))
        self._fsm_last_mode = mode
        try:
            self._proactive.eat_mouse_tick()
        except Exception:
            self.logger.warning("eat_mouse_tick 异常", exc_info=True)
        # 无条件按 FSM.pos 同步窗口位置：MOVE_TO/FALL 之外还有**静默位移**
        # （骑乘跟随移动窗口发生在 IDLE，step 返回 ANIMATE）——只听 action
        # 会漏掉这类位移，视觉上宠物悬空在旧高度。Qt 同坐标 move 是 no-op，
        # 每 tick 调用无代价。
        self.window.move_bottom_center(*self.fsm.pos)
        # v0.10.16 行走朝向：按位移方向翻转显示（帧素材统一面朝右）
        last = getattr(self, "_last_facing_x", None)
        if last is not None:
            dx = self.fsm.pos[0] - last
            if abs(dx) > 0.4:
                self.window.set_facing(1 if dx > 0 else -1)
        self._last_facing_x = self.fsm.pos[0]

    # ---- v0.3 动画 ----
    # H1 修（REVIEW-2026-08-25）：随机小动作 key 集——_frame_tick 的兜底停
    # 豁免这组（旧版 ANIMATE 刚启动就在同一 tick 被兜底停掉，永远不可见），
    # 终止改由 _play_animate 排的到期 singleShot 负责。
    _SMALL_ANIM_KEYS = ("stretch", "blink", "roll")

    def _play_animate(self, name: str) -> None:
        """随机小动作（v0.10.15 帧动画）：stretch/blink 播帧序列，
        roll 单帧定格；缺帧回退 get_frames（静帧）。"""
        key = {"stretch": "stretch", "blink": "blink", "roll": "roll"}.get(name)
        if key is None:
            return
        if isinstance(self.provider, AIArtProvider):
            frames = self.provider.frames_for(self.store.get().stage.value, key)
            if frames:
                interval = self.provider.frame_interval(key)
                self._play_key(key, frames, loop=(key == "blink"),
                               interval=interval)
                # H1 修：显式终止——blink 循环两轮后停；stretch/roll 播完
                # 定格一小会儿再回静帧（key 已被后续动画覆盖则不动）
                ms = (2 * len(frames) * interval if key == "blink"
                      else len(frames) * interval + 400)
                anim_key = key

                def _end_anim() -> None:
                    if getattr(self, "_anim_key", None) == anim_key:
                        self._stop_anim()

                QTimer.singleShot(ms + 120, _end_anim)
                return
        frames = self.provider.get_frames(self.store.get(), ActionType.ANIMATE)
        self.window.play_frames(frames)

    # ---- v0.10.15 状态驱动帧播放 ----
    def _play_key(self, key: str, frames: list, loop: bool = False,
                  interval: int = 150) -> None:
        """播放并记录当前 key（同 key 重入不重启计时器）。"""
        # v0.13：私有 _frames 直读收口为 is_playing()（两套呈现后端同语义）
        if getattr(self, "_anim_key", None) == key and self.window.is_playing():
            return
        self._anim_key = key
        for f in frames:
            f.width = self.window.width()
            f.height = self.window.height()
        self.window.play_frames(frames, loop=loop, interval_ms=interval)

    def _stop_anim(self) -> None:
        if getattr(self, "_anim_key", None) is not None:
            self._anim_key = None
            self.window.stop_frames()

    def _frame_tick(self, action, mode: str, prev_mode: str) -> None:
        """FSM 模式 → 帧：walk 交替 / fall 空中 / 落地瞬帧 / 吃鼠标咀嚼循环。"""
        provider = self.provider
        if not isinstance(provider, AIArtProvider):
            return
        stage = self.store.get().stage.value
        if prev_mode in ("fall", "thrown") and mode not in ("fall", "thrown"):
            land = provider.frames_for(stage, "fall")
            if len(land) > 1:
                self._anim_key = None  # 允许覆盖 air 循环
                self._play_key("land", [land[-1]], loop=False,
                               interval=provider.frame_interval("fall"))
                return
            self._stop_anim()
            return
        if mode in ("fall", "thrown"):
            air = provider.frames_for(stage, "fall")
            if air:
                self._play_key("fall_air", [air[0], air[0]], loop=True)
            return
        if mode == "eat_mouse":
            seq = provider.frames_for(stage, "chew")
            if seq:
                self._play_key("eat_mouse_chew", seq, loop=True,
                               interval=provider.frame_interval("chew"))
            return
        # L1 修（REVIEW-2026-08-25）：follow 判定读 FSM 真实模式（旧版
        # getattr(self,"_follow") 读 PetApp 不存在的属性恒 False——死分支）
        if (mode == "walk"
                or (mode == "idle" and self.fsm.motion_mode == "follow")):
            # v0.14 部件驱动步态优先（paperdoll）：当前 figure 挂 limb 部件
            # → 不播 walk 帧，正面原地步态由场景 limb 驱动器程序化合成；
            # 无 limb figure（mood 姿态/未铺量阶段）走下方帧路径自动回退。
            if getattr(self, "_part_walk", False) \
                    and self.window.part_walk_active():
                if self._anim_key == "walk":
                    self._stop_anim()
                return
            walk = provider.frames_for(stage, "walk")
            if walk:
                self._play_key("walk", walk, loop=True,
                               interval=provider.frame_interval("walk", stage))
            return
        # H1 修：兜底停豁免小动作（stretch/blink/roll 由 _play_animate 的
        # 到期 singleShot 终止）——旧版这里把刚启动的小动作同 tick 停掉
        if (getattr(self, "_anim_key", None) not in (None, "land")
                and self._anim_key not in self._SMALL_ANIM_KEYS):
            self._stop_anim()

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
        for name in ("_proactive_timer", "_chat_emotion_timer", "_periodic_save_timer", "_save_timer",
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
            # M6 修（REVIEW-2026-08-25）：收口在飞 proactive 决策线程（旧版
            # 不 cancel 不 wait，QThread 随 GC 触发 destroyed-while-running）
            try:
                self._proactive.shutdown()
            except Exception:
                self.logger.warning("shutdown 收口 proactive worker 异常",
                                    exc_info=True)
        # ③ 注销全局热键（v0.11 持久热键线程）
        try:
            self.adapter.stop_hotkeys()
        except Exception:
            pass
        # ④ 保存 PetState+Memory
        if getattr(self, "memory", None) is not None:
            try:
                self.memory.forget_expired()
                self.memory.save(self._memory_path)
            except Exception:
                self.logger.exception("记忆存档失败")
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
