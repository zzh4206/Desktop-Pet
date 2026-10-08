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

from PySide6.QtCore import QThread, QTimer, Signal
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import QApplication

from pet import __version__ as PKG_VERSION
from pet.asset_provider import AIArtProvider, EmojiProvider, _mood_from_state
from pet.behavior import ActionType, BehaviorFSM
from pet.bubble import BubbleType, BubbleWidget
from pet.config import load_config
from pet.floating import FloatingTextWidget
from pet.interaction import memory_fact, pet_status_line
from pet.logging_setup import setup_logging
from pet.llm import create_client  # v0.4.15 工厂（不再硬编码 DeepSeekClient）
from pet.needs import NeedsEngine, status_line as needs_status_line
from pet.perf import FramePacer, TIER_NAME_ZH
from pet.pet_state import Mood, PetStateStore, Stage
from pet.platform import get_platform_adapter
from pet.sound import SoundFX
from pet.tools_schema import ToolContext, ToolRegistry
from pet.tray import TrayManager
# 新引擎有效部分经中间层 EngineBridge 接入（原有引擎 frames 恒为兜底）。
# v0.15.1 接回：motion/wind/sun 以「可选叠加」注入，任一环失败即降级恒等。
from pet.engine_bridge import (
    ChannelEnricher, EngineBridge, MotionEnricher, NullEnricher,
)

# 版本单一源 = pet/__init__.__version__（L2 治理：旧版三处硬编码漂移到
# v0.7.4+win / 0.9.3 / 幻影 v0.12.1 注释）。发版只改 pet/__init__.py。
APP_VERSION = f"v{PKG_VERSION}"

_SAVE_DEBOUNCE_MS = 500       # 变更后 500ms 内多次只存一次
_SAVE_PERIODIC_MS = 30_000    # 定时存档
_DECAY_INTERVAL_MS = 1000     # 衰减 1s 一次（wall-clock delta）

# GC 治理（长运行进程）：热身 _GC_FREEZE_DELAY_MS 后 collect+freeze 存量
# + 放宽分配阈值。gen0=10000 是平衡点——每万次分配回收一次，gen0 扫描
# （~1万个年轻对象，1–3ms）在 33ms 动画拍预算内；再大会出现可感知的
# 单次停顿。freeze 后 gen2 全量扫描只含热身后新对象（量级骤降），
# 故 gen1/gen2 倍数不必激进。见 PetApp._freeze_gc。
_GC_FREEZE_DELAY_MS = 30_000
_GC_THRESHOLD = (10_000, 50, 50)

# 交互语义（字段/动词/文案池/三态决策）收拢在 pet/interaction.py——
# 呈现无关，2D/3D 双实现共用（3D 契约 §6）。

_CHAT_EMOTION_BUBBLES = {
    "happy": ("太好了，替你开心～", "听起来真棒！", "今天有好消息呀～"),
    "neutral": ("我在这儿陪着你～", "慢慢来就好。", "今天也一起加油～"),
    "sad": ("抱抱你，难过也没关系。", "我会在这里听你说。", "先对自己温柔一点～"),
    "sleepy": ("辛苦啦，早点休息吧～", "慢一点，今晚好好放松。", "困了就和我一起歇会儿～"),
    "hungry": ("别忘了吃点东西呀～", "先补充一点能量吧。", "喝口水、吃点热乎的～"),
}


class _ChatEmotionWarmWorker(QThread):
    """后台预热聊天情绪引擎（onnxruntime session 加载 ~1s，不阻塞主线程）。

    只在 GUI 线程把建好的 engine 经 ``ready`` 信号投递回去；worker 自身只
    产对象，不触碰任何 Qt GUI。生命周期铁律：finished→deleteLater 单通道，
    避免「销毁运行中 QThread」的原生崩溃（全仓既有 pattern）。
    """

    ready = Signal(object)

    def __init__(self, model_path: str, threshold: float, parent=None) -> None:
        super().__init__(parent)
        self._model_path = model_path
        self._threshold = threshold

    def run(self) -> None:
        from pet.chat_emotion import ChatEmotionEngine
        engine = ChatEmotionEngine(self._model_path, self._threshold)
        self.ready.emit(engine)


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
        # v0.19.8 帧率分档（pet/perf.py）：FSM tick / rig 三档节奏由档位
        # 决定；菜单「流畅度」改选后持久化到 performance.frame_tier
        self.pacer = FramePacer(
            (self.cfg.get("performance") or {}).get("frame_tier", "auto"))
        # v0.15.1 接回：风/光影 + 运动引擎统一经中间层 EngineBridge 装配
        # （见 _build_engine_bridge，在 store 就绪后调用；任一环失败恒等）。
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
        self._chat_emotion_model_path = None
        self._chat_emotion_worker = None
        self._chat_emotion_active = None
        if self._chat_emotion_cfg.get("enabled", True):
            # 冷启动只建 store，不建 engine、不 import onnxruntime——无对话
            # 时省 ~70MB 常驻（见 wiki/设计-情绪模型按对话热度加载.md）。
            from pet.chat_emotion import ConversationEmotionStore
            self._chat_emotion_store = ConversationEmotionStore(
                os.path.join(paths["data_dir"], "chat_emotion.json"),
                self._chat_emotion_cfg.get("retention_hours", 48),
            )
            model_root = os.path.join(os.path.dirname(__file__), "pet", "models")
            v2_path = os.path.join(model_root, "chat_emotion_v2")
            self._chat_emotion_model_path = (
                v2_path if os.path.isdir(v2_path)
                else os.path.join(model_root, "chat_emotion_v1.npz"))

        # 养成 store：启动 load（无存档→default）；重启数值一致靠此
        self.store = PetStateStore.load(self._state_path)
        # v0.19.8 养成机制引擎（pet/needs.py）：区段/联动/衰减/交互落账收口。
        # 触线阈值仍以 proactive.need_bubble 为配置出处（alerts 按需传入）。
        icfg = self.cfg.get("interaction") or {}
        self.needs = NeedsEngine(
            self.store,
            needs_cfg=self.cfg.get("needs") or {},
            gains=self.cfg.get("interaction_gain") or {},
            interaction_cfg={
                "reject_fullness": float(icfg.get("reject_fullness", 92)),
                "fatigue_times": int(icfg.get("fatigue_times", 5)),
                "fatigue_window_min": float(icfg.get("fatigue_window_min", 10)),
            },
            msg_overrides=icfg.get("messages") or {},
        )
        # v0.19.2 F9/F10：需求触线阈值（托盘状态行 ⚠ 与右键菜单 ⚠ 共用；
        # 与 proactive.need_bubble 同源）
        need = (self.cfg.get("proactive") or {}).get("need_bubble")
        need = need if isinstance(need, dict) else {}
        self._need_thresholds = {
            "fullness": float(need.get("fullness", 30.0)),
            "cleanliness": float(need.get("cleanliness", 25.0)),
            "mood": float(need.get("mood", 20.0)),
        }
        # v0.15.1 接回：新引擎有效部分（motion + wind/sun）经中间层 EngineBridge
        # 叠加到原有引擎 frames；任一环失败 → 恒等（原有引擎兜底，不阻断启动）。
        self._bridge = self._build_engine_bridge()

        # v0.18.12 三维呈现（render3d 实验线）：flag 开才装配，失败/关闭零影响
        # 2D。3D 成功时预览窗伴随主宠物显示（M1 实验轨形态；窗体集成待定稿）。
        self._render3d = None
        self._r3_walk_phase = 0.0

        self.sensors = adapter.get_sensors()  # 注入式，不直 import sensor_mac
        # v0.10 provider 挂 idle_fn：idle 超时 → SLEEPY 立绘（_mood_from_state）
        self.provider = self._make_provider()
        wa = self.sensors.work_area
        self.fsm = BehaviorFSM(dict(wa), self.cfg.get("behavior", {}))

        # v0.13/v0.18 展示后端选择：presentation=rig（v0.17.8 起默认，
        # 侧身行走开箱即用）| frames（帧动画，低配回退项）| paperdoll（部件步态）。
        sprite0 = self.provider.get_static(self.store.get())
        presentation = self.cfg.get("presentation", "rig")
        if presentation in ("rig", "paperdoll"):
            from pet.rig.presenter import build_rig_window
            base_window = adapter.create_pet_window(sprite0)
            base_cls = base_window.__class__
            base_window.deleteLater()
            self.window = build_rig_window(
                base_cls,
                sprite0,
                self.store.get().stage.value,
                defer_quick=True,
            )
        else:
            self.window = adapter.create_pet_window(sprite0)
        self._part_walk = (presentation == "paperdoll")
        # 批次L/N3（实机审查 2026-08-31）：_anim_key 初始化——旧版首赋值在
        # _play_key，行走先于首个随机小动作时 _frame_tick 裸读
        # self._anim_key 每拍 AttributeError：FSM 照走、窗口位置同步被跳过
        # =宠物画面冻结直到首个小动作（15-35s）后才动
        self._anim_key = None
        self.window.set_sprite_provider(self.provider)
        # v0.19.2 F10：菜单 ⚠ 标记阈值与 proactive.need_bubble 同源
        self.window.set_need_thresholds(self._need_thresholds)
        # v0.19.8：流畅度子菜单选中态 + auto 判档结果显示
        self.window.set_frame_tier_state(
            self.pacer.choice, TIER_NAME_ZH[self.pacer.tier])
        # 批次A/H1（REVIEW-2026-08-31）：进化换档重载 rig spec——三阶段
        # manifest 共用 figure 键，spec 终生绑启动阶段会把新阶段 neutral
        # 映射回旧阶段派生核心图（宠物在 rig/paperdoll 档"长不大"直到重启）。
        # 订阅须先于 window.on_state_change（后者换图即按新 spec 解析）
        self._rig_stage = self.store.get().stage.value

        def _on_stage_maybe_changed(s) -> None:
            if s.stage.value == self._rig_stage:
                return
            self._rig_stage = s.stage.value
            set_stage = getattr(self.window, "set_stage", None)
            if callable(set_stage):
                set_stage(s.stage.value)
            self._setup_side_locomotion()

        self.store.on_change(_on_stage_maybe_changed)
        self._setup_side_locomotion()
        # 每次启动都以中性聊天表情开始，不恢复上次退出前的短时状态。
        if self._chat_emotion_store is not None:
            self._reset_chat_emotion_to_neutral()
        # Apply the startup expression immediately; an unchanged decay tick
        # need not emit a state update, and set_conversation_mood has no state yet.
        self.window.on_state_change(self.store.get())
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
        # G7 边缘修复：横向可达范围按真实窗口宽喂入（与 move_bottom_center
        # 的整窗在屏内钳制同语义；进化/重置变尺寸时同步更新）
        self.fsm.set_pet_width(self.window.width())
        self.store.on_change(lambda _s: (
            self.fsm.set_pet_height(self.window.height()),
            self.fsm.set_pet_width(self.window.width())))

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
        # v0.17.0：宠物右键"聊天"直达面板（与热键同走 toggle：可见即隐藏）
        self.window.chatRequested.connect(self._toggle_chat_panel)
        # v0.3 拖拽（拖动直接挪窗保跟手）+ 移动模式
        self.window.dragStarted.connect(self._on_drag_started)
        self.window.dragMoved.connect(self._on_drag_moved)
        self.window.dragReleased.connect(self._on_drag_released)
        self.window.motionModeRequested.connect(self._set_motion_mode)
        # v0.19.8 流畅度：菜单改档 → 即时生效 + 持久化
        self.window.frameTierRequested.connect(self._on_frame_tier_requested)
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
        self._status_window = None
        self._status_engine = None
        self._status_bridge = None
        # v0.3 气泡跟随宠物（§2.4 头顶 20px / 靠顶翻下）
        self.window.petMoved.connect(self._on_pet_moved)

        self.bubble = BubbleWidget()
        # v0.19.0 F1 数值飘字 HUD：与气泡同通道跟随，点击穿透
        self.floating = FloatingTextWidget()
        # v0.19.0 F4 音效管线：默认关；缺 QtMultimedia/资产全程静默
        self.sfx = SoundFX(self.cfg.get("sound", {}))
        # 图层探针排除自身（宠物站窗顶时探针点被自己身体覆盖 → 误否决支撑）
        self.adapter.register_own_windows(self.window, self.bubble, self.floating)
        self.tray = TrayManager(on_quit=self.shutdown, parent=self.app)
        self.tray.set_reset_callback(self._on_reset_requested)
        # v0.19.2 F9：状态进托盘（tooltip 状态行 + 图标红点）——数值变化
        # （交互/衰减/进化）即刷新；衰减按小时 tick，开销可忽略
        self.store.on_change(self._on_state_for_tray)
        self._on_state_for_tray(self.store.get())

        # v0.4 聊天：key 引导 + DS 客户端 + 工具注册表 + QML 面板
        self._chat_engine = None
        self._chat_bridge = None
        self._chat_window = None
        self._chat_client = None
        # v0.20 模型管理（_setup_chat 内填充；此处置默认防异常路径 AttributeError）
        self._model_registry = None
        self._model_providers = {}
        self._model_selected = ""
        try:
            self._setup_chat()
        except Exception:
            self.logger.exception(
                "聊天初始化失败（key/tool/QML），宠物本体继续运行")

        # v0.18.19 3D 装配时序：必须在所有 2D QML singleton 注册与 engine
        # 创建（_setup_chat→_build_chat_panel）**之后**——3D 的 QQuickView 若
        # 成为进程首个活跃 engine，PySide6 6.10 下后续注册的类型/singleton
        # 不被新 engine 解析（mem_bridge 已录同签名病：Cannot assign … to
        # list property "data"），聊天面板/rig 场景全挂（对照实验：3D 开=2
        # 报错、3D 关=0）。3D 关时本调用零成本。
        self._setup_render3d()

        # v0.6 主动关怀（win 主笔）+ v0.7 吃鼠标（mac）：
        # 30s 轮询；气泡锚宠物；idle 用传感器；有 DS key 时链式唤醒走 LLM
        # 隔离决策，否则本地罐头。v0.7 注入平台 mouse_lock + 四门禁检查器 +
        # FSM 事件派发（共享 ProactiveScheduler 零平台库，经 adapter 注入）。
        from pet.proactive import ProactiveScheduler

        proactive_cfg = self.cfg.get("proactive", {})
        self._proactive = ProactiveScheduler(
            store=self.store,
            bubble_fn=self._auto_bubble,
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
            # v0.19.4 F16：mac 日历节日源（EventKit；拒绝/无绑定回落内置表）
            festival_fn=self.adapter.get_festival_source(),
            # 批次B/P2-6（REVIEW-2026-09-05）：问候/节日/follow-up/唤醒链
            # 持久化——旧版全内存，重启重发早晚安/节日、回访静默丢失
            state_path=os.path.join(paths["data_dir"], "proactive_state.json"),
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
        self._chat_emotion_expiry_timer = QTimer(self.app)
        self._chat_emotion_expiry_timer.setSingleShot(True)
        self._chat_emotion_expiry_timer.timeout.connect(self._reset_chat_emotion_to_neutral)

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
        self.store.on_change(lambda _s: self._sync_chat_avatar())
        # 用当前 state 调制一次（启动即对齐数值，不等首次衰减）
        self.fsm.on_state_change(self.store.get())

        # 气泡骨架自检（证明 BubbleWidget.show(text) 能显示文字，挂宠物头顶）
        QTimer.singleShot(
            1500,
            lambda: self.bubble.show("我醒啦～", anchor=self._pet_anchor()),
        )

        # 传感器慢刷新（2s），FSM 快 tick（间隔按帧率档位 33/50/66ms），
        # 衰减 1s（wall-clock delta），全屏检测 1s（独立于传感器缓存，
        # 缩短可拖/可见窗口期）
        self._sensor_timer = QTimer(self.app)
        self._sensor_timer.timeout.connect(self._refresh_sensors)
        self._sensor_timer.start(2000)

        self._fullscreen_timer = QTimer(self.app)
        self._fullscreen_timer.timeout.connect(self._check_fullscreen)
        self._fullscreen_timer.start(1000)

        self._tick_timer = QTimer(self.app)
        self._tick_timer.timeout.connect(self._tick)
        # v0.19.8 帧率分档：FSM tick 间隔按档位（high 33 / medium 50 / low 66ms）
        self._tick_timer.start(self.pacer.fsm_ms)
        # v0.19.8：rig 三档常数按档位注入（frames 后端无此方法，幂等跳过）
        _apply_tier = getattr(self.window, "apply_frame_tier", None)
        if callable(_apply_tier):
            _apply_tier(*self.pacer.rig_intervals())

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

        # GC 治理：热身 30s（QML 引擎/纹理/部件模型/引擎状态全部建稳）后
        # 冻结存量对象 + 放宽阈值（见 _freeze_gc）。放末尾：只依赖 app 已建好。
        QTimer.singleShot(_GC_FREEZE_DELAY_MS, self._freeze_gc)

    def _freeze_gc(self) -> None:
        """长运行进程 GC 治理（idle 停顿与周期性回收治理）。

        背景：30/66Hz 渲染拍持续产小对象流（dict/dataclass/QVariant 临时），
        默认 (700,10,10) 阈值下 gen0 频繁回收、累积触发 gen2 全量扫描——
        既是可感知停顿的候选来源，也与监控里周期性内存波动相关。

        做法（CPython 长运行进程标准配方）：
        1. ``collect()``：先清掉启动期的循环垃圾，只冻结"确认存活"的存量；
        2. ``freeze()``：把当前全部被跟踪对象移入永久代——此后任何回收
           都跳过它们（热身后的常驻结构：解释器/Qt 绑定/资产/引擎状态）；
        3. ``set_threshold``：放宽 gen0 触发阈值，减少回收频率。

        边界：freeze 后**新分配**（聊天 worker、换挡资产、情绪推理等运行期
        对象）仍在正常代际，回收语义不变；freeze 只豁免"冻结时刻存活"的
        对象——因此必须排在启动结构稳定之后（过早冻结会把启动临时物
        变成永不回收的驻留）。"""
        try:
            import gc
            gc.collect()
            freeze = getattr(gc, "freeze", None)
            if callable(freeze):            # PyPy/极简构建无 freeze，跳过不误伤
                freeze()
            gc.set_threshold(*_GC_THRESHOLD)
            self.logger.info(
                "GC 治理完成：存量冻结 + 阈值 (700,10,10)→%s", _GC_THRESHOLD)
        except Exception:
            self.logger.warning("GC 治理失败（忽略，不影响运行）", exc_info=True)

    def _build_engine_bridge(self) -> EngineBridge:
        """装配中间层（原有引擎 frames ← EngineBridge ← 新引擎有效部分）。

        motion（呼吸/眨眼/squash/倾斜）+ wind/sun 两条通道各自独立兜底：
        任一环抛错 → 该环降级为恒等/静态，返回的 EngineBridge 永不抛错。
        spec 为 None（无 manifest）时 motion 仍可出整身增量（呼吸/眨眼/
        squash），只是无部件角 —— 不因此回退。

        CPU 优化：rig/paperdoll 后端下**不装配 MotionEnricher**。
        RigWindow 自持 MotionEngine（presenter._motion_timer 30Hz step），
        中间层这份 motion 的 body_angle/scale/part_angles/blink 在
        RigWindow.apply_enrichment 里只取 shadow、其余算完即弃（纯浪费
        20Hz 全量 FK/LBS）。rig 后端下 enricher 恒 NullEnricher，RigWindow
        ._engine 是唯一 step 者；frames 后端仍要 body_y（呼吸）故保留 motion。
        """
        presentation = self.cfg.get("presentation", "rig")
        enricher = NullEnricher()
        if presentation not in ("rig", "paperdoll"):
            try:
                from pet.rig.spec import load_rig_spec
                rig_root = os.path.join(os.path.dirname(__file__), "assets", "rig")
                stage = self.store.get().stage.value
                spec = load_rig_spec(os.path.join(rig_root, stage), stage)
                enricher = MotionEnricher(spec)
            except Exception:
                self.logger.warning("新引擎运动增强装配失败，回退恒等", exc_info=True)
        try:
            channels = ChannelEnricher(self.cfg)
        except Exception:
            self.logger.warning("新引擎风/光影通道装配失败，回退静态", exc_info=True)
            channels = None
        return EngineBridge(enricher, channels)

    # ---- v0.18.12 三维呈现（render3d 实验线；全部防御式，3D 失败零影响 2D） ----

    def _setup_render3d(self) -> None:
        """v0.18.16 互斥呈现：flag 开且装配成功 → 3D 嵌入主窗（2D 立绘层
        隐藏、窗尺寸切 3D 档）；失败/关闭 → 原路径一字不动。任何异常只记
        日志（2D 永远是兜底）。"""
        import time as _time
        self._r3_time = _time
        try:
            from pet.render3d.bridge import Render3DBridge

            self._render3d = Render3DBridge(
                self.cfg,
                on_click=None,               # 单击走 WindowBase 手势消解（下方注入）
                on_drag_start=lambda: self._r3_interact("drag"),
            )
            if not self._render3d.start():
                self._render3d = None
                return
            win = self._render3d._renderer._window
            # 嵌入主窗（互斥呈现）；失败回退伴随窗形态（实验轨保留）
            W3, H3 = 240, 420
            if self.window.attach_render3d(win._view):
                self.window.resize(W3, H3)
                # 单击命中在 QWidget 层（嵌入后 QML MouseArea 收不到事件）：
                # 单击消歧到期 → 3D 摸头语义（_interact("pet") 全链路）
                self.window.set_render3d_click(
                    lambda: self._r3_interact("click"))
                # 钉所有 Space（v0.18.30 修正：**不带 Stationary**——0.18.29b
                # 实测切应用后窗口 onscreen=None（层可见但被移出屏幕合成=
                # 用户看到的"切换应用就消失"）；Stationary 是聊天面板语义，
                # 桌宠要的是 CanJoinAllSpaces 跟随+IgnoresCycle）
                try:
                    if self.adapter.pin_pet_float_window(self.window):
                        self.logger.info("3D 主窗已钉所有 Space（无 Stationary）")
                    else:
                        self.logger.warning("3D 主窗钉 Space 失败，降级靠 raise 兜底")
                except Exception:
                    self.logger.warning("3D 主窗钉 Space 异常", exc_info=True)
                self._r3_embedded = True
                self.logger.info("render3d 已嵌入主窗（互斥呈现，%dx%d）", W3, H3)
            else:
                win.place_bottom_right()
                win.show()
                self._r3_embedded = False
                self.logger.info("render3d 主窗嵌入失败，退伴随预览窗")
            self.logger.info("render3d 预览窗几何=%s visible=%s",
                             win._view.geometry(), win._view.isVisible())
        except Exception:
            self.logger.warning("render3d 装配失败，保持 2D", exc_info=True)
            self._render3d = None

    def _r3_interact(self, kind: str) -> None:
        """3D 呈现下的交互入口——**完全复用 2D 主通道** `_interact`（三态
        决策/记忆/气泡/音效全链路）。单击=摸头语义（patRequested 等价），
        由 WindowBase 手势消解层触发（嵌入模式下 QML MouseArea 收不到
        事件——QWidget 是事件宿主）；拖拽走既有 dragStarted/dragReleased
        信号链（app 已接 FSM drag 会话），此处只补 3D 姿势窗口。"""
        if kind == "click":
            self._interact("pet")            # 与 2D 单击摸头完全同路径
        else:
            self._r3_airborne_until = self._r3_time.monotonic() + 2.0

    def _render3d_tick(self) -> None:
        """每拍喂 2D 实况 → 契约 → 3D 桥（内部任何失败自动永久降级）。"""
        r3 = self._render3d
        if r3 is None or r3.mode != "3d":
            return
        try:
            from pet.render3d.semantic_source import PetSnapshot, scene_state

            mode = self.fsm.mode
            walking = (mode == "walk"
                       or (mode == "idle" and self.fsm.motion_mode == "follow"))
            vx, _vy = self.fsm.velocity
            hz = max(0.9, min(2.0, 0.9 + abs(vx) / 400.0))
            self._r3_walk_phase = (self._r3_walk_phase + hz * 0.05) % 1.0
            airborne = mode in ("fall", "thrown", "drag") or \
                self._r3_time.monotonic() < getattr(self, "_r3_airborne_until", 0.0)
            # 交互反应窗口（_interact 置 1.2s）：3D 摆 click 反应姿势
            action_type = mode
            if self._r3_time.monotonic() < getattr(self, "_r3_react_until", 0.0):
                action_type = "animate"   # semantic_source 映射 → click 反应
            sun = None
            ch = getattr(self._bridge, "_channels", None)
            src = getattr(ch, "_sun", None) if ch else None
            try:
                sun = src.current() if src else None
            except Exception:
                sun = None
            # 风（S1.6 弹簧骨驱动源）：wind 通道 (gain 0-1, bias) → m/s 假设
            # 满档 8（spring_driver 的 gain 映射基准）
            try:
                wg, _wb = self._bridge.wind()
            except Exception:
                wg = 0.0
            # 行进朝向（v0.18.28 方向感知侧身）：与 2D 侧身线同款——按位移
            # 方向判定（|dx|>0.4 才翻转），存 _r3_facing 供 PetSnapshot
            last_x = getattr(self, "_r3_last_x", None)
            if last_x is not None:
                dx = self.fsm.pos[0] - last_x
                if abs(dx) > 0.4:
                    self._r3_facing = 1 if dx > 0 else -1
            self._r3_last_x = self.fsm.pos[0]
            snap = PetSnapshot(
                action_type=action_type, walking=walking,
                walk_phase=self._r3_walk_phase, airborne=airborne,
                facing=int(getattr(self, "_r3_facing", 1)),
                mood=self.store.get().mood,
                # 情绪/眨眼/凝视：chat_emotion 显式标签 + motion 眨眼脉冲
                # （0.19.3 聊天↔养成双向联动在 3D 同样生效）
                emotion_label=getattr(self, "_chat_emotion_active", None),
                blink_progress=self._r3_blink_progress(),
                sun_azimuth_deg=getattr(sun, "azimuth_deg", 0.0) if sun else 0.0,
                sun_elevation_deg=getattr(sun, "elevation_deg", 0.0) if sun else 0.0,
                wind_speed=max(0.0, float(wg)) * 8.0,
            )
            r3.apply(scene_state(snap))
        except Exception:
            self.logger.warning("render3d 喂入异常（已降级 2D）", exc_info=True)
            self._render3d = None

    def _r3_blink_progress(self) -> float:
        """本地低频自驱眨眼（4-7s 随机，300ms 三角波）→ 0-1 进度。"""
        import random
        now = self._r3_time.monotonic()
        period = getattr(self, "_r3_blink_period", 5.0)
        if now < getattr(self, "_r3_blink_until", 0.0):
            span = 0.30
            t = (now - (self._r3_blink_until - span)) / span
            return max(0.0, min(1.0, 1.0 - abs(2.0 * t - 1.0)))
        if now > getattr(self, "_r3_blink_next", 0.0):
            self._r3_blink_until = now + 0.30
            self._r3_blink_next = now + period * (0.6 + 0.8 * random.random())
        return 0.0

    def _pet_anchor(self) -> tuple:
        """气泡锚点：宠物当前 bottom_center + 窗口高。"""
        x, y = self.fsm.pos
        return (x, y, self.window.height())

    def _auto_bubble(self, text: str, kind=BubbleType.INFO,
                     duration_ms: int = 5000, anchor: tuple | None = None) -> None:
        """自动路径气泡统一入口（批次B/P2-4，REVIEW-2026-09-05）——全屏
        期间丢弃并留痕。H2 只挡了进入全屏瞬间的 hide（window+bubble），
        此后久坐提醒/早晚安/聊天情绪等新气泡 bubble.show 照常 show+raise，
        盖在演示/放映上（气泡是独立 Tool|StaysOnTop 窗，不随 window.hide
        收）。用户主动路径（托盘/右键/设置弹窗反馈）不走此入口不受限；
        退出全屏后下一轮周期照常发。"""
        if getattr(self, "_fullscreen", False):
            self.logger.info("全屏中丢弃气泡: %s", (text or "")[:40])
            return
        self.bubble.show(text, kind=kind, duration_ms=duration_ms,
                         anchor=anchor if anchor is not None
                         else self._pet_anchor())

    def _on_state_for_tray(self, state) -> None:
        """F9：PetState → 托盘状态行 + 触线红点（永不外抛，坏了只丢状态行）。

        v0.19.8 措辞/判线收口 needs 引擎（与右键菜单 ⚠、状态板同一出处）。"""
        try:
            line, alert = needs_status_line(state, self._need_thresholds)
            self.tray.set_status(line)
            self.tray.set_alert(alert)
        except Exception:
            self.logger.warning("托盘状态行更新异常", exc_info=True)

    # ---- v0.4 聊天 ----
    def _setup_chat(self) -> None:
        """v0.4.15 多 provider：扫描已注入 key 的 provider → 弹选（多个时）
        → create_client 工厂实例化 → 工具注册表 + QML 面板。

        无 key → 气泡提示首次引导（QInputDialog）；仍无 key → 聊天禁用，宠物仍跑。
        目前只 deepseek（首批验证中间层）；后续加 claude/openai 只在 create_client
        工厂加分支 + config providers 段加条目。

        批次B/H2（REVIEW-2026-08-31）：记忆库加载 + 工具注册表 + mem/perm
        singleton 预注册全部**先于** key 判定——旧版无 key 提前 return，
        记忆管理页 QML 因 singleton 未注册永不加载、MemoryStore 从不读盘，
        v0.9"UI 查看/删除/清空"Must 对无 key 用户整体失效。"""
        # v0.9 长期记忆（共享，与平台工具并列；加工具不改 llm.py）
        from pet.memory import MemoryStore
        from pet.memory_tools import build_memory_tools

        self._memory_path = os.path.join(self._paths["data_dir"],
                                         "memory.json")
        self.memory = MemoryStore.load(self._memory_path)

        # 工具注册表（无 key 也建：记忆工具随注册表常驻，key 补设后即可用）
        registry = ToolRegistry(confirm_fn=self.adapter.confirm_dangerous)
        if sys.platform == "darwin":
            from pet.tools_mac import build_mac_tools

            for schema, handler in build_mac_tools():
                registry.register(schema, handler)
        elif sys.platform == "win32":
            from pet.tools_win import build_win_tools

            for schema, handler in build_win_tools():
                registry.register(schema, handler)
        for schema, handler in build_memory_tools(self.memory):
            registry.register(schema, handler)

        # v0.9/v0.8 面板 singleton 预注册：必须在 _build_chat_panel 创建首个
        # QQmlApplicationEngine 前注册，否则 PySide6 6.10 下后注册的 singleton
        # （约束实测于 6.10；requirements 钉 >=6.5,<6.8，先注册在旧版无害，
        # 按 L24/REVIEW-2026-09-04 留档防御性保留）
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
        # v0.15 状态板：诊断可视化（无需 key 也可用，故与 mem/perm 同级预注册）
        from pet.ui.status_bridge import StatusBridge, register_status_singleton
        self._status_bridge = StatusBridge(self._status_rows)
        register_status_singleton(self._status_bridge)
        self.tray.set_status_callback(self._show_status)

        # v0.20 模型管理：注册表留存 + 托盘入口。无 key 引导路径也要能进
        # 管理对话框接入（新增模型+Key 后就地补建聊天，免重启）
        self._model_registry = registry
        providers_cfg = self.cfg.get("llm", {}).get("providers", {})
        self._model_providers = dict(providers_cfg)
        self._model_selected = (self.cfg.get("llm", {}) or {}).get("selected", "")
        self.tray.set_model_manager_callback(self._show_model_manager)
        self._sync_model_tray()

        # 扫描已注入 key 的 provider（Keychain + env）
        available = []
        for name, pcfg in providers_cfg.items():
            env_var = pcfg.get("api_key_env", "")
            key = self.adapter.get_llm_key(name, env_var)
            if key:
                available.append((name, key))

        selected, key = None, None
        # v0.20：记忆上次选择（llm.selected）——key 仍在就直接用，不再
        # 每次启动弹选（多 provider 弹选改只在无记忆/记忆失效时出现）
        if self._model_selected in providers_cfg:
            k = self.adapter.get_llm_key(
                self._model_selected,
                providers_cfg[self._model_selected].get("api_key_env", ""))
            if k:
                selected, key = self._model_selected, k

        if selected is None:
            if not available:
                # 首次引导：默认 deepseek（config 里有的第一个）
                default_name = next(iter(providers_cfg), "deepseek")
                default_env = providers_cfg.get(default_name, {}).get("api_key_env", "DEEPSEEK_API_KEY")
                key = self._ensure_llm_key(default_name, default_env)
                if not key:
                    self.logger.warning("LLM key 未设置，聊天禁用（宠物仍跑）")
                    # 托盘聊天 fallback 也注册——点击有反馈（气泡提示）而非静默
                    self.tray.set_chat_callback(self._show_chat)
                    QTimer.singleShot(
                        1500,
                        lambda: self.bubble.show(
                            "还没设置 API key，聊天暂时不可用～"
                            "（托盘“模型管理…”可接入模型）",
                            anchor=self._pet_anchor(),
                        ),
                    )
                    return
                available = [(default_name, key)]

            # 无记忆/记忆失效时才弹选
            if len(available) == 1:
                selected, key = available[0]
            else:
                selected, key = self._select_provider(available)
            if selected != self._model_selected:
                try:
                    from pet.model_registry import save_llm_section
                    save_llm_section(self._paths["config_path"], None, selected)
                except Exception:
                    self.logger.warning("模型选择写盘失败", exc_info=True)
                self._model_selected = selected

        self._activate_llm(selected, key)
        self._build_chat_panel(registry)
        # v0.21：swe_task 委托工具注册须在 ChatBridge 建好之后（runner 流式回显）
        self._setup_swe(registry)

    def _select_provider(self, available) -> tuple:
        """QInputDialog 下拉选 provider（仅无 llm.selected 记忆/记忆失效时弹）。
        返 (name, key)。"""
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

    # ---- v0.20 模型管理（接入/改 Key/增删/切换）----

    def _activate_llm(self, selected: str, key: str) -> None:
        """按 provider 名实例化客户端（聊天/滚屏摘要/主动关怀 + v0.21 swe）。

        启动选择与运行时切换共用入口；各实例隔离 _resp/usage 的既有约束
        不变（H4/M5）。调用方负责把新引用换进 bridge/scheduler（swap_clients
        /set_client）——启动路径此时面板未建，无需换。"""
        from pet.llm import create_client

        self._chat_client = create_client(
            selected, key, self._model_registry, self.cfg)
        self._sum_client = create_client(
            selected, key, self._model_registry, self.cfg)
        self._proactive_client = create_client(
            selected, key, self._model_registry, self.cfg)
        # v0.21：第 4 个客户端（swe 专用，独立实例隔离 _resp/usage）
        self._swe_client = None
        if (self.cfg.get("swe", {}) or {}).get("enabled"):
            self._swe_client = create_client(
                selected, key, self._model_registry, self.cfg)
        self._model_selected = selected
        self.logger.info("LLM provider: %s", selected)

    def _setup_swe(self, registry) -> None:
        """v0.21 mini-swe：装配沙箱环境 + 专用 registry + agent，并把 swe_task
        委托工具注册进主聊天 registry。

        须在 ChatBridge 建好之后调用（runner 经 bridge.sweStep 信号把每条
        bash 步骤流式回显进聊天面板）。swe.enabled=false 时不注册（工具不进
        schema，模型无从调用）。
        """
        swe_cfg = self.cfg.get("swe", {}) or {}
        if not swe_cfg.get("enabled"):
            return
        if self._swe_client is None:
            self.logger.warning("swe 已启用但客户端未建，跳过 swe_task 注册")
            return
        from pet.swe_agent import SweAgent
        from pet.swe_env import SweEnvironment
        from pet.swe_tools import (SWE_TASK_SCHEMA, SweTaskHandler,
                                   build_swe_tools)
        from pet.tools_schema import ToolRegistry

        workspace = (swe_cfg.get("workspace_dir") or ""
                     or os.path.join(self._paths["data_dir"], "workspace"))
        env = SweEnvironment(
            workspace,
            command_timeout_s=swe_cfg.get("command_timeout_s", 30.0),
            output_max_chars=swe_cfg.get("output_max_chars", 8000),
            block_patterns=swe_cfg.get("block_patterns") or [],
            allow_network=not swe_cfg.get("no_network", False),
            confirm_fn=self.adapter.confirm_dangerous,
        )
        swe_registry = ToolRegistry()
        for schema, handler in build_swe_tools(env):
            swe_registry.register(schema, handler)
        self._swe_agent = SweAgent(
            self._swe_client, swe_registry,
            step_limit=swe_cfg.get("step_limit", 10),
            wall_time_s=swe_cfg.get("wall_time_s", 0.0),
        )

        def _run(instruction: str) -> dict:
            bridge = getattr(self, "_chat_bridge", None)

            def _emit(command: str, output: str) -> None:
                if bridge is not None:
                    try:
                        bridge.sweStep.emit(command, output)
                    except RuntimeError:
                        pass  # bridge 已销毁（shutdown），丢弃步骤

            return self._swe_agent.run(instruction, on_step=_emit)

        registry.register(SWE_TASK_SCHEMA, SweTaskHandler(_run))
        self.logger.info("mini-swe 已装配（workspace=%s, step_limit=%s）",
                         env.workspace, swe_cfg.get("step_limit", 10))

    def _sync_model_tray(self) -> None:
        """托盘'切换模型'子菜单重建（当前项勾选 + 模型名/密钥态）。"""
        try:
            from pet.model_registry import entry_label

            entries = []
            for name, pcfg in (self._model_providers or {}).items():
                has_key = bool(self.adapter.get_llm_key(
                    name, pcfg.get("api_key_env", "")))
                entries.append((name, entry_label(
                    name, pcfg, self._model_selected, has_key)))
            self.tray.set_models(entries, self._model_selected,
                                 self._switch_model)
        except Exception:
            self.logger.warning("托盘模型菜单刷新失败", exc_info=True)

    def _show_model_manager(self) -> None:
        """托盘'模型管理…'：接入/编辑（自定义命名+URL+模型 ID+Key）、
        测试连接、删除、设为当前使用。

        对话框只管编辑态；落盘/切换经 on_commit/on_select 回调回来。"""
        from pet.ui.model_dialog import ModelManagerDialog

        def key_reader(name: str):
            pcfg = (self._model_providers or {}).get(name) or {}
            return self.adapter.get_llm_key(name, pcfg.get("api_key_env", ""))

        dlg = ModelManagerDialog(
            self._model_providers,
            self._model_selected,
            key_reader=key_reader,
            key_writer=self.adapter.set_llm_key,
            on_commit=self._commit_models,
            on_select=self._switch_model,
        )
        dlg.exec()

    def _commit_models(self, providers: dict, selected: str) -> None:
        """对话框保存/删除回调：config 原子写（只动 llm 段）+ 内存/托盘同步
        + 冷启动补建聊天（无 key 引导路径下首次接入立即生效，免重启）。"""
        from pet.model_registry import save_llm_section

        try:
            save_llm_section(self._paths["config_path"], providers,
                             selected or None)
        except Exception:
            self.logger.warning("模型配置写盘失败", exc_info=True)
        self._model_providers = dict(providers)
        self._model_selected = selected
        # 内存 config 同步（create_client 读 self.cfg；不刷会拿旧配置）
        llm_cfg = self.cfg.setdefault("llm", {})
        llm_cfg["providers"] = dict(providers)
        if selected:
            llm_cfg["selected"] = selected
        else:
            llm_cfg.pop("selected", None)
        self._sync_model_tray()
        # 冷启动补建：聊天从未初始化（无 key 引导路径），新接入立即生效
        if (self._chat_client is None and selected
                and self._model_registry is not None):
            pcfg = providers.get(selected) or {}
            if self.adapter.get_llm_key(selected, pcfg.get("api_key_env", "")):
                self._switch_model(selected)

    def _switch_model(self, name: str) -> None:
        """切换当前使用的模型（托盘子菜单/对话框'设为当前使用'）。

        在飞轮次仍持旧 client 跑完（worker 构造时已捕获引用），新消息即走
        新模型；llm.selected 写盘，下次启动直用。"""
        pcfg = (self._model_providers or {}).get(name) or {}
        key = self.adapter.get_llm_key(name, pcfg.get("api_key_env", ""))
        if not key:
            self.bubble.show(
                f"“{name}”还没有 API Key，请在“模型管理…”里设置～",
                anchor=self._pet_anchor())
            return
        try:
            self._activate_llm(name, key)
            llm_cfg = self.cfg.setdefault("llm", {})
            llm_cfg["providers"] = dict(self._model_providers)
            llm_cfg["selected"] = name
        except Exception as exc:
            self.logger.warning("切换模型 %s 失败: %s", name, exc)
            self.bubble.show(f"切换失败：{exc}", anchor=self._pet_anchor())
            return
        try:
            from pet.model_registry import save_llm_section

            save_llm_section(self._paths["config_path"], None, name)
        except Exception:
            self.logger.warning("模型选择写盘失败", exc_info=True)
        # 换引用：bridge 在飞 worker 不受影响；聊天未建过则就地补建
        if self._chat_bridge is not None:
            self._chat_bridge.swap_clients(self._chat_client, self._sum_client)
        elif self._model_registry is not None:
            self._build_chat_panel(self._model_registry)
        if getattr(self, "_proactive", None) is not None:
            self._proactive.set_client(self._proactive_client)
        # v0.21：swe agent 换 client（在飞任务已捕获旧 client，跑完不受影响）
        if getattr(self, "_swe_agent", None) is not None:
            self._swe_agent.set_client(self._swe_client)
        self._sync_model_tray()
        self.bubble.show(f"已切换到 {name}～", anchor=self._pet_anchor())

    def _chat_avatar_url(self) -> str:
        """当前宠物立绘 → file:// URL（聊天面板"对方"头像）。

        按当前 state 经 provider 解析静帧立绘；emoji provider/缺图返回空串
        （QML 回退 🐱）。异常只降级不崩。
        """
        try:
            sprite = self.provider.get_static(self.store.get())
            path = getattr(sprite, "path", "") or ""
            if path and os.path.isfile(path):
                from PySide6.QtCore import QUrl

                return QUrl.fromLocalFile(path).toString()
        except Exception:
            self.logger.warning("聊天头像立绘解析失败", exc_info=True)
        return ""

    def _sync_chat_avatar(self) -> None:
        """把当前立绘路径推给聊天面板（进化/衰减/情绪变化时经 on_change 触发）。"""
        if self._chat_bridge is not None:
            self._chat_bridge.set_pet_avatar(self._chat_avatar_url())

    def _build_chat_panel(self, registry) -> None:
        """载入 QML 聊天面板（不可见，托盘/聚焦唤出）。

        v0.6.3：先注册 fallback chat callback（_show_chat 会气泡提示未设 key），
        防 QML 载入抛异常时 tray callback 未注册致托盘聊天点击静默无反应。
        """
        import os

        # 先注册 fallback：即使 QML 载入失败，托盘聊天也有反馈
        self.tray.set_chat_callback(self._show_chat)
        from pet.ui.chat_bridge import ChatBridge, load_chat_panel
        from pet.ui.session_store import SessionStore

        qml_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "pet", "ui", "main.qml"
        )
        self._chat_bridge = ChatBridge(
            self._chat_client, registry, self._make_tool_context,
            sum_client=getattr(self, "_sum_client", None),
            # v0.17.1 多会话：JSON 持久化（原子写），重启恢复
            store=SessionStore(os.path.join(
                self._paths["data_dir"], "chat_sessions.json")),
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
        # 聊天面板"对方"头像 = 当前宠物立绘（首帧同步；后续经 on_change 更新）
        self._sync_chat_avatar()

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
        """发消息前钩子：v0.9 记忆注入 + v0.6 follow-up 启发式。

        v0.19.3 F11：记忆段后追加宠物实时状态一行——LLM 可自然地说
        "我都饿了"，聊天与养成从单向（消息→数值）变双向。"""
        # 记忆注入：recall 按当前消息 → 刷 system prompt 记忆段
        try:
            from pet.memory_tools import memory_context

            seg = memory_context(self.memory, text)
            status = pet_status_line(self.store.get())
            if getattr(self, "_render3d", None) is not None \
                    and self._render3d.mode == "3d":
                status = (f"{status}\n（当前以 3D 立体形象陪伴主人，"
                          f"可以自然提及自己的动作和姿态）") if status else \
                        "（当前以 3D 立体形象陪伴主人）"
            if status:
                seg = f"{seg}\n\n{status}" if seg else status
            self._chat_client.set_memory_context(seg)
        except Exception:
            self.logger.warning("记忆注入异常", exc_info=True)
        # L22（REVIEW-2026-09-04）：follow-up 排程也要兜底——旧版裸调，
        # 异常被 chat_bridge 的钩子包装吞掉后无任何日志，排障黑洞
        try:
            self._maybe_followup(text)
        except Exception:
            self.logger.warning("follow-up 排程异常", exc_info=True)
        # M2（REVIEW-2026-09-04）：禁用即时生效——旧版只判启动时创建的
        # store，设置页关掉后仍持续把用户消息写进 chat_emotion.json 直到重启
        if (self._chat_emotion_store is not None
                and self._chat_emotion_cfg.get("enabled", True)):
            try:
                self._chat_emotion_store.add_user_message(text)
                self._ensure_chat_emotion_warm()   # 冷态首条消息触发预热（安全网）
                self._evaluate_message_emotion()
            except Exception:
                self.logger.warning("聊天情绪上下文写入失败", exc_info=True)

    def _ensure_chat_emotion_warm(self) -> None:
        """冷态 → 后台 QThread 预热情绪引擎（幂等；主线程不阻塞）。

        onnxruntime session 加载 ~1s，放后台线程避免卡 GUI。engine 建好后经
        ``ready`` 信号回主线程赋值；加载失败也返回一个内部已降级的 engine
        （session/weights 全 None），调用方据此走规则兜底。
        """
        if (not self._chat_emotion_cfg.get("enabled", True)
                or self._chat_emotion_engine is not None
                or self._chat_emotion_model_path is None):
            return
        worker = self._chat_emotion_worker
        if worker is not None:
            import shiboken6
            # 陈旧 worker（C++ 已随 finished→deleteLater 删除）会抛
            # 「Internal C++ object already deleted」，先判有效性
            if shiboken6.isValid(worker) and worker.isRunning():
                return  # 已在预热，幂等
            self._chat_emotion_worker = None  # 陈旧或已结束，清引用后重建
        worker = _ChatEmotionWarmWorker(
            self._chat_emotion_model_path,
            self._chat_emotion_cfg.get("confidence_threshold", .55),
            parent=self.app,
        )
        # lambda 默认参捕获 worker 判归属，替代 self.sender()（PetApp 非 QObject）
        worker.ready.connect(
            lambda engine, w=worker: self._on_chat_emotion_warmed(w, engine)
        )
        # finished→deleteLater 唯一删除通道（销毁运行中 QThread = 原生崩溃）
        worker.finished.connect(worker.deleteLater)
        self._chat_emotion_worker = worker
        worker.start()

    def _on_chat_emotion_warmed(self, worker, engine) -> None:
        """预热完成，engine 投递回主线程（陈旧 worker 的迟到 ready 不覆盖）。"""
        if worker is not self._chat_emotion_worker:
            return
        self._chat_emotion_engine = engine
        self._chat_emotion_worker = None

    def _evaluate_message_emotion(self) -> None:
        """每条新用户消息只在检测到明显情绪波动时立即换表情。"""
        from pet.chat_emotion import is_significant, obvious_emotion
        store, engine = self._chat_emotion_store, self._chat_emotion_engine
        if store is None:
            return
        messages = store.recent_messages()
        if not messages:
            return
        event_threshold = self._chat_emotion_cfg.get("event_confidence_threshold", .5)
        if engine is None:
            # 冷态（模型未预热/加载失败）：关键词规则兜底最直白情绪，
            # 不阻塞等待 ~1s 的模型加载；模型 ready 后下一句接管。
            result = obvious_emotion(messages[-1]["text"])
            if result is None:
                return
        else:
            # 即时状态以最新一句为主：旧的开心/难过不能把新表达反向覆盖。
            # v2 句向量模型必须先判断，不能被关键词规则短路；显式词仅保留给旧 v1 的安全回退。
            # v1 即时路径刻意只喂 messages[-1:]（新情绪不被历史旧句稀释），
            # 与 v1 训练的 2-5 句窗口有分布差异——取舍留档（批次C/P3-17 注）。
            # M3（REVIEW-2026-09-04）：evaluate 传 event 阈值——引擎内层默认
            # 0.55 截断先于 app 层判定生效，event_confidence_threshold<0.55 时是死旋钮
            result = engine.evaluate(messages[-1:], threshold=event_threshold)
            if result.used_fallback and engine.version != 2:
                # P3-17：该分支只在 v1 引擎（version==1 或 None）走——元数据带
                # 真实版本，v2 短路守卫（M10c）据此成立
                result = obvious_emotion(messages[-1]["text"],
                                         model_version=engine.version) or result
        if not is_significant(result, event_threshold):
            return
        # 每个短时状态最多五分钟；新消息只会在明确的新情绪时覆盖。
        duration_s = self._chat_emotion_duration_seconds()
        store.set_current(result, __import__("time").time() + duration_s)
        self._chat_emotion_expiry_timer.start(int(duration_s * 1000))
        self._apply_chat_emotion(result.label, result.confidence)

    def _chat_emotion_duration_seconds(self) -> float:
        return max(60., float(self._chat_emotion_cfg.get("expression_minutes", 5)) * 60)

    def _reset_chat_emotion_to_neutral(self) -> None:
        """启动或短时状态过期后，恢复可预测的中性表情。"""
        if self._chat_emotion_store is not None:
            self._chat_emotion_store.clear_current()
        self._chat_emotion_active = "neutral"
        try:
            self.window.set_conversation_mood(Mood.NEUTRAL)
        except Exception:
            self.logger.warning("聊天情绪重置失败", exc_info=True)

    def _poll_chat_emotion(self) -> None:
        """执行到期时段。模型/存档故障都只降级，不影响桌宠主循环。"""
        store, engine = self._chat_emotion_store, self._chat_emotion_engine
        if (store is None or engine is None
                or not self._chat_emotion_cfg.get("enabled", True)):  # M2 同上
            return
        try:
            active = store.active_label() or "neutral"
            if active != self._chat_emotion_active:
                self._chat_emotion_active = active
                self.window.set_conversation_mood(Mood(active) if active else None)
            from pet.chat_emotion import EmotionResult, is_significant
            for slot in store.due_slots(self._chat_emotion_cfg.get("schedule", [])):
                result = engine.evaluate(
                    store.recent_messages(), slot,
                    threshold=self._chat_emotion_cfg.get(
                        "event_confidence_threshold", .5))  # M3：同即时路径
                # 22:00 是休息提醒：只有明确的非中性情绪才覆盖 sleepy。
                if slot == "22:00" and not is_significant(
                        result, self._chat_emotion_cfg.get("event_confidence_threshold", .5)):
                    result = EmotionResult("sleepy", 0.0, True, result.model_version)
                duration_s = self._chat_emotion_duration_seconds()
                store.set_current(result, __import__("time").time() + duration_s)
                self._chat_emotion_expiry_timer.start(int(duration_s * 1000))
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
            self.window.set_conversation_mood(mood)
            # 批次C/P3-8（REVIEW-2026-09-05）：成功后才置 active——旧版先置
            # 再更新，更新失败时 change-detector 视为已应用，到期前永不重试
            self._chat_emotion_active = label
        except Exception:
            self.logger.warning("聊天情绪立绘更新失败", exc_info=True)
        delta = float(self._chat_emotion_cfg.get("mood_delta", {}).get(label, 0))
        if delta and confidence >= float(self._chat_emotion_cfg.get("confidence_threshold", .55)):
            self.store.update(mood=delta)
        import random
        # v0.19.3 F13：用户说饿了 → 宠物同步表达自己的需求（确实饿了才开口，
        # 不饿不打扰）；发了求助就不再叠共情气泡（同气泡位会互相覆盖）
        if label == "hungry":
            fired = False
            try:
                fired = self._proactive.check_needs_now()
            except Exception:
                self.logger.warning("hungry 联动需求表达异常", exc_info=True)
            if fired:
                return
        self._auto_bubble(random.choice(_CHAT_EMOTION_BUBBLES[label]))

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
        # L8（REVIEW-2026-09-04）：0=禁用睡姿——旧版 0 会变成门限 0s 恒 SLEEPY，
        # 且配置值此前从未真正接入判定（见 asset_provider 修复说明）
        raw_sleepy = float(self.cfg.get("sleepy_idle_minutes", 10))
        sleepy_s = raw_sleepy * 60 if raw_sleepy > 0 else None
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
            # v0.17.0：热键提示进托盘 tooltip（悬停可见，缓解热键太隐蔽）
            self.tray.set_tooltip(f"桌宠 · {_hk_hint}")

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
        气泡，勾选态与真实状态脱节到重启）。
        批次J/L1（REVIEW-2026-08-31）：回滚到操作**前**状态（not enabled）
        ——旧版恒回滚 False，"关闭失败"时勾选态与实际脱节方向相反。"""
        ok = self.adapter.set_autostart(enabled)
        if not ok:
            self.tray.set_autostart_state(not enabled)
        self.bubble.show(
            "开机自启已开启～" if ok and enabled else
            "开机自启已关闭" if ok else "自启设置失败",
            anchor=self._pet_anchor(),
        )

    def _status_rows(self, dev_mode: bool = False) -> list:
        """状态板行数据（诊断可视化用）。每段 try/except 兜底，绝不崩。

        默认仅展示「养成」；行为/传感器/聊天情绪/呈现/主动关怀均为开发
        诊断项，dev_mode=True 时才追加。"""
        rows: list = []
        # ---- 养成 ----
        try:
            state = self.store.get()
            sc = self.cfg.get("score", {})
            score = (float(sc.get("mood_weight", .4)) * state.mood
                     + float(sc.get("fullness_weight", .4)) * state.fullness
                     + float(sc.get("cleanliness_weight", .2)) * state.cleanliness)
            raw_sleepy = float(self.cfg.get("sleepy_idle_minutes", 10))
            sleepy_s = raw_sleepy * 60 if raw_sleepy > 0 else None
            mood = _mood_from_state(state, self.sensors.idle_time, sleepy_s).value
            healthy_thr = float(sc.get("healthy_threshold", 70))

            rows.append({"type": "section", "name": "养成"})
            # v0.19.8：区段命名 + 触线判级收口 needs 引擎（与托盘/菜单同源）
            _zones = self.needs.zones(state)
            _alerts = set(self.needs.alerts(state, self._need_thresholds))
            rows.append({"type": "field", "name": "阶段", "value": state.stage.value, "level": "ok"})
            rows.append({"type": "field", "name": "分支", "value": state.branch.value,
                         "level": "ok" if state.branch.value == "healthy" else "warn"})
            rows.append({"type": "field", "name": "心情",
                         "value": f"{state.mood:.1f} · {mood} / {_zones['mood']}",
                         "level": "warn" if "mood" in _alerts else "ok"})
            rows.append({"type": "field", "name": "饱食",
                         "value": f"{state.fullness:.1f} · {_zones['fullness']}",
                         "level": "warn" if "fullness" in _alerts else "ok"})
            rows.append({"type": "field", "name": "清洁",
                         "value": f"{state.cleanliness:.1f} · {_zones['cleanliness']}",
                         "level": "warn" if "cleanliness" in _alerts else "ok"})
            rows.append({"type": "field", "name": "年龄", "value": f"{state.age:.1f} 天", "level": "ok"})
            rows.append({"type": "field", "name": "养护分", "value": f"{score:.1f}",
                         "level": "ok" if score >= healthy_thr else "warn"})
        except Exception as exc:
            rows.append({"type": "field", "name": "养成", "value": f"读取失败 {exc}", "level": "bad"})

        # ---- 行为（开发模式） ----
        if dev_mode:
            try:
                fsm = self.fsm
                cx, by = fsm.pos
                vx, vy = fsm.velocity
                rows.append({"type": "section", "name": "行为"})
                rows.append({"type": "field", "name": "FSM 状态", "value": fsm.mode, "level": "ok"})
                rows.append({"type": "field", "name": "移动模式", "value": fsm.motion_mode, "level": "ok"})
                rows.append({"type": "field", "name": "位置", "value": f"({cx:.0f}, {by:.0f})", "level": "ok"})
                rows.append({"type": "field", "name": "速度", "value": f"({vx:.0f}, {vy:.0f}) px/s", "level": "ok"})
            except Exception as exc:
                rows.append({"type": "field", "name": "行为", "value": f"读取失败 {exc}", "level": "bad"})

        # ---- 传感器（开发模式） ----
        if dev_mode:
            try:
                idle = float(getattr(self.sensors, "idle_time", 0.0))
                mp = getattr(self.sensors, "mouse_pos", (0, 0))
                fs = bool(getattr(self, "_fullscreen", False))
                rows.append({"type": "section", "name": "传感器"})
                rows.append({"type": "field", "name": "系统空闲", "value": f"{idle:.1f}s", "level": "ok"})
                rows.append({"type": "field", "name": "鼠标", "value": f"({int(mp[0])}, {int(mp[1])})", "level": "ok"})
                rows.append({"type": "field", "name": "全屏", "value": "是" if fs else "否",
                             "level": "warn" if fs else "ok"})
                if sys.platform == "darwin":
                    try:
                        from pet.mouse_lock_mac import frontmost_app_name
                        rows.append({"type": "field", "name": "前台应用", "value": frontmost_app_name() or "-", "level": "ok"})
                    except Exception:
                        pass
            except Exception as exc:
                rows.append({"type": "field", "name": "传感器", "value": f"读取失败 {exc}", "level": "bad"})

        # ---- 聊天情绪（开发模式） ----
        if dev_mode:
            try:
                engine = self._chat_emotion_engine
                store = self._chat_emotion_store
                rows.append({"type": "section", "name": "聊天情绪"})
                ver = getattr(engine, "version", None)
                rows.append({"type": "field", "name": "引擎", "value": f"v{ver}" if ver else "禁用",
                             "level": "ok" if ver else "warn"})
                rows.append({"type": "field", "name": "当前情绪", "value": self._chat_emotion_active or "neutral", "level": "ok"})
                rows.append({"type": "field", "name": "已存消息", "value": str(len(store.messages) if store else 0), "level": "ok"})
            except Exception as exc:
                rows.append({"type": "field", "name": "聊天情绪", "value": f"读取失败 {exc}", "level": "bad"})

        # ---- 呈现（开发模式） ----
        if dev_mode:
            try:
                rows.append({"type": "section", "name": "呈现"})
                rows.append({"type": "field", "name": "立绘来源", "value": self.cfg.get("provider", "emoji"), "level": "ok"})
                rows.append({"type": "field", "name": "展示后端", "value": self.cfg.get("presentation", "rig"), "level": "ok"})
                # v0.19.8 帧率分档：显示生效档位与 FSM tick 目标间隔
                rows.append({"type": "field", "name": "流畅度",
                             "value": f"{TIER_NAME_ZH[self.pacer.tier]}档"
                                      f"（{self.pacer.choice}，FSM {self.pacer.fsm_ms}ms）",
                             "level": "ok"})
                w = self.window
                rows.append({"type": "field", "name": "窗口", "value": f"{w.width()}×{w.height()} @ ({w.x()},{w.y()})", "level": "ok"})
            except Exception as exc:
                rows.append({"type": "field", "name": "呈现", "value": f"读取失败 {exc}", "level": "bad"})

        # ---- 主动关怀（开发模式） ----
        if dev_mode:
            try:
                proactive = getattr(self, "_proactive", None)
                eat = bool(getattr(getattr(proactive, "_eat_session", None), "active", False))
                rows.append({"type": "section", "name": "主动关怀"})
                rows.append({"type": "field", "name": "吃鼠标", "value": "锁定中" if eat else "空闲",
                             "level": "warn" if eat else "ok"})
                nw = getattr(proactive, "_next_wake_at", None)
                if nw:
                    import time as _t
                    secs = float(nw) - _t.time()
                    rows.append({"type": "field", "name": "下次唤醒",
                                 "value": f"{int(secs)}s 后" if secs >= 0 else "待定", "level": "ok"})
                else:
                    rows.append({"type": "field", "name": "下次唤醒", "value": "未排期", "level": "ok"})
            except Exception as exc:
                rows.append({"type": "field", "name": "主动关怀", "value": f"读取失败 {exc}", "level": "bad"})

        return rows

    def _show_status(self) -> None:
        """v0.15 状态板（托盘'状态板'唤出；诊断可视化内部状态）。"""
        if self._status_window is None:
            from pet.ui.status_bridge import load_status_qml
            self._status_engine, self._status_window = load_status_qml()
        if self._status_window is None:
            self.bubble.show("状态板加载失败～", anchor=self._pet_anchor())
            return
        self._status_bridge.refresh()
        self._status_window.show()
        self._status_window.raise_()
        self._status_window.requestActivate()

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
                                       QFormLayout, QLabel, QLineEdit,
                                       QMessageBox)
        dialog = QDialog()
        dialog.setWindowTitle("聊天情绪设置")
        layout = QFormLayout(dialog)
        enabled = QCheckBox("启用本地聊天情绪推理")
        enabled.setChecked(bool(self._chat_emotion_cfg.get("enabled", True)))
        schedule = QLineEdit(", ".join(self._chat_emotion_cfg.get("schedule", ["22:00"])))
        schedule.setPlaceholderText("例如：22:00")
        note = QLabel("消息文本仅在本机保留最近 48 小时；关闭开关即停止记录，"
                      "档案可随时删除。")
        note.setWordWrap(True)
        layout.addRow(enabled); layout.addRow("每天推理时段：", schedule)
        layout.addRow(note)
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
            self.bubble.show("聊天情绪设置已保存～", anchor=self._pet_anchor())
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
                "还没设置 API key，聊天暂不可用～", anchor=self._pet_anchor()
            )
            return
        self._ensure_chat_emotion_warm()   # 打开面板即预热情绪引擎（~1s 被打字掩盖）
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
        self._auto_bubble("当前离线，聊天暂不可用～", kind=BubbleType.WARNING)

    def _on_pet_moved(self, x: float, y: float, h: int) -> None:
        self.bubble.follow((x, y, h))
        self.floating.follow((x, y, h))

    def _interact(self, kind: str) -> None:
        """v0.2 养成交互入口：window signal 触发 → 决策 → 四通道反馈。

        v0.19.0 起反馈四通道：数值飘字（F1）+ 音效（F4）+ 文案池气泡（F3）
        + 喂食咀嚼覆盖（F2，临时态不进 FSM）。v0.19.1 起三态决策：正常生效
        / 饱和拒绝 / 互动疲劳——拒绝与疲劳不加数值但反馈照走（拒绝也是反馈）。
        v0.19.8 决策+数值落账+疲劳记账收口 needs 引擎，此处只做反馈装配。
        emoji 切换仍由 on_change 订阅自动处理。
        """
        import time

        out = self.needs.interact(kind, now=time.monotonic())
        if out.field is None:
            return
        if out.floating_text:
            self.floating.pop(out.floating_text, tone=out.floating_tone,
                              anchor=self._pet_anchor())
        if out.sound:
            self.sfx.play(out.sound)
        if out.message:
            self.bubble.show(out.message, anchor=self._pet_anchor())
        if out.chew:
            self._play_feed_chew()
        self._remember_interaction(out)
        # v0.18.14 3D 同步表达：任何生效交互让 3D 形象摆出反应姿势（click
        # 反应动画语义）；喂食另有咀嚼帧（2D），3D 走同款 click 语义
        r3 = getattr(self, "_render3d", None)
        if r3 is not None and r3.mode == "3d":
            self._r3_airborne_until = 0.0
            self._r3_react_until = self._r3_time.monotonic() + 1.2

    def _remember_interaction(self, out) -> None:
        """F12：交互/拒绝/疲劳事件写 episodic 记忆。

        日期戳前缀 + memorize 同文去重 = 同类事件当日合并为一条；重要度
        0.2–0.35，靠 forget_expired 的按天衰减自然淘汰（低价值不占库）。
        写入永不外抛（记忆坏了不能断交互）。"""
        mem = getattr(self, "memory", None)
        if mem is None:
            return
        try:
            pair = memory_fact(out.kind, out)
            if pair is None:
                return
            from datetime import date

            fact, importance = pair
            mem.memorize(f"{date.today().isoformat()} {fact}", importance)
        except Exception:
            self.logger.warning("交互记忆写入异常", exc_info=True)

    def _play_feed_chew(self) -> None:
        """F2 手动喂食咀嚼覆盖：复用吃鼠标 chew 帧源，播 ~1.5s。

        临时态不进 FSM（避免搅动吃鼠标会话逻辑）；终止走 _play_animate 同款
        到期 singleShot + key 比对（被后续动画覆盖则不动）。_frame_tick 的
        兜底停豁免 _INTERACT_ANIM_KEYS（同 _SMALL_ANIM_KEYS 机制）。
        """
        provider = self.provider
        if not isinstance(provider, AIArtProvider):
            return
        seq = provider.frames_for(self.store.get().stage.value, "chew")
        if not seq:
            return
        interval = provider.frame_interval("chew")
        one_pass_ms = max(1, len(seq) * interval)
        cycles = max(1, round(1500 / one_pass_ms))
        key = "feed_chew"
        self._play_key(key, seq, loop=True, interval=interval)

        def _end_anim() -> None:
            if getattr(self, "_anim_key", None) == key:
                self._stop_anim()

        QTimer.singleShot(cycles * one_pass_ms + 120, _end_anim)

    # ---- v0.19.8 帧率分档（流畅度菜单） / config 段落盘 ----
    def _on_frame_tier_requested(self, choice: str) -> None:
        """右键菜单「流畅度」改档：切档 → 节拍全链路即时生效 → 持久化。"""
        tier = self.pacer.set_choice(choice)
        # FSM tick：start() 对活跃 timer 即重启，换档当拍生效
        self._tick_timer.start(self.pacer.fsm_ms)
        apply_tier = getattr(self.window, "apply_frame_tier", None)
        if callable(apply_tier):
            apply_tier(*self.pacer.rig_intervals())
        self.window.set_frame_tier_state(
            self.pacer.choice, TIER_NAME_ZH[tier])
        zh = TIER_NAME_ZH[tier]
        msg = (f"已恢复自动判档（当前{zh}档）" if self.pacer.choice == "auto"
               else f"已切换到{zh}档帧率")
        if self._save_config_section(
                "performance", {"frame_tier": self.pacer.choice}):
            self.bubble.show(msg, anchor=self._pet_anchor())
        else:
            self.bubble.show(msg + "（写盘失败，重启后失效）",
                             kind=BubbleType.WARNING,
                             anchor=self._pet_anchor())

    def _save_config_section(self, key: str, value: dict) -> bool:
        """菜单改动持久化到用户 config（仅覆盖该段；原子写同聊天情绪先例）。"""
        try:
            path = self._paths["config_path"]
            raw: dict = {}
            if os.path.exists(path):
                with open(path, encoding="utf-8") as f:
                    raw = json.load(f)
            if not isinstance(raw, dict):
                raw = {}
            raw[key] = value
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(raw, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
            return True
        except Exception:
            self.logger.warning("config 段 %s 保存失败", key, exc_info=True)
            return False

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
        # v0.19.8：衰减 + 联动修正收口 needs 引擎（wall-clock 语义不变：
        # 基于时间戳补算，离线期间照衰减；联动取回档时点状态近似）
        self.needs.tick_decay(
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
        self.fsm.set_pet_width(self.window.width())
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
        self.fsm.set_pet_width(self.window.width())
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
        # H1（REVIEW-2026-09-04）：零变更不落盘——旧版周期档（30s）与防抖档
        # 都无条件 dump+fsync+bak+replace。dirty 由 update/reset 置位、save
        # 成功清零；getattr 兼容测试桩 store（无 dirty 视为恒脏，保持旧行为）。
        if getattr(self.store, "dirty", True):
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
            # 进全屏停 rig 常驻 30Hz 运动/渲染循环（隐藏后仍在空转
            # setBonePose/update）；frames 后端无此方法，getattr 幂等。
            pause = getattr(self.window, "pause_render", None)
            if callable(pause):
                pause()
            self.logger.info("全屏检测：隐藏宠物（含气泡）")
        elif was and not fs:
            self._fullscreen = False
            self.fsm.handle_event("fullscreen_off")
            self.window.show()
            resume = getattr(self.window, "resume_render", None)
            if callable(resume):
                resume()
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

    # ---- ADULT / FINAL 侧身行走（各阶段 locomotion 配置）----
    _LOCO_SPEED_CAP = 200.0     # px/s；跟随模式 600 px/s 限速到步行上限（方案 §8 决策 4 缺省）
    _LOCO_BREAK_MODES = ("fall", "thrown", "drag", "climb", "eat_approach", "eat_mouse")
    # G7 跟手/边缘：意图按误差比例给速——远处贴上限、近处随距离减速，步态
    # 刹车（0.4s 线性）+ 收步前移的动量不再冲过目标（旧版恒速 120/200 到
    # 达，过冲 ~30-70px → follow 反向再起步 = 侧身⇄正面转身片段循环）。
    # 增益 1.2/s：停下距离 ≈ 0.45·v < 误差（稳定收敛，不振荡）。
    _LOCO_INTENT_GAIN = 1.2             # (px/s) / px
    _LOCO_INTENT_DEADZONE_PX = 4.0      # |dx| 死区：到位即 0，防边界意图翻转

    def _locomotion_pre_step(self) -> bool:
        avail = getattr(self.window, "locomotion_available", None)
        if not (callable(avail) and avail()):
            self.fsm.hold_x = False
            return False
        if self.window.locomotion_controls_x():
            self.fsm.sync_x(self.window.x() + self.window.width() / 2.0)
            self.fsm.hold_x = True
        else:
            self.fsm.hold_x = False
        return True

    def _locomotion_post_step(self) -> None:
        mode = self.fsm.mode
        if mode in self._LOCO_BREAK_MODES:
            self.window.set_locomotion_intent(0.0)
            self.window.locomotion_interrupt()
            self.fsm.hold_x = False
            return
        target = self.fsm.walk_target
        if mode == "walk" and target is not None:
            dx = target[0] - self.fsm.pos[0]
            if abs(dx) <= self._LOCO_INTENT_DEADZONE_PX:
                self.window.set_locomotion_intent(0.0)
                return
            cap = (self._LOCO_SPEED_CAP if self.fsm.motion_mode == "follow"
                   else min(self.fsm._speed, self._LOCO_SPEED_CAP))
            speed = min(cap, self._LOCO_INTENT_GAIN * abs(dx))
            self.window.set_locomotion_intent(speed if dx > 0 else -speed)
        else:
            self.window.set_locomotion_intent(0.0)

    def _setup_side_locomotion(self) -> None:
        """按当前阶段配置和资产包启用侧身行走。"""
        enable = getattr(self.window, "enable_side_locomotion", None)
        disable = getattr(self.window, "disable_side_locomotion", None)
        stage = getattr(self.store.get(), "stage", None)
        stage = getattr(stage, "value", stage)
        if not callable(enable):
            return
        if stage in ("adult", "final") and self.cfg.get(f"{stage}_locomotion", "side_rig") == "side_rig":
            pkg = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", f"rig_{stage}_walk_v1")
            if not enable(pkg):
                self.logger.warning("%s_locomotion=side_rig 但侧身行走资产不可用，回退旧路径", stage)
        elif callable(disable):
            disable()

    def _tick(self) -> None:
        # 可见性看门狗：非全屏被隐藏（异常/竞态）→ 立即恢复并留痕
        if not getattr(self, "_fullscreen", False) and not self.window.isVisible():
            self.window.show()
            self.logger.warning("宠物窗口异常隐藏，看门狗已恢复")
        # 批次I/M6（REVIEW-2026-08-31）：mac 上气泡 show 前 view.window()
        # 为 None——启动时登记自身窗口缺气泡 wid，图层探针会把气泡误判为
        # 遮挡（宠物站窗顶+气泡弹出时支撑被误否决）。气泡首次可见时补登记
        # （win 端 winId 随时有效，补登记幂等无害）
        if not getattr(self, "_bubble_wid_registered", False) \
                and self.bubble.isVisible():
            self._bubble_wid_registered = True
            try:
                self.adapter.register_own_windows(self.window, self.bubble)
            except Exception:
                self._bubble_wid_registered = False
                self.logger.warning("气泡窗口补登记失败", exc_info=True)
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
        _last = getattr(self, "_last_tick_at", None)
        _raw_dt = (now - _last) if _last is not None else 0.0
        dt = min(0.25, max(0.01, _raw_dt))
        self._last_tick_at = now
        # v0.19.8 过载守卫：auto 档持续超时自动降档（纯记账，绝不抛）
        self.pacer.record_fsm_tick(_raw_dt if _raw_dt > 0 else None)
        loco = self._locomotion_pre_step()
        action = self.fsm.step(self.store.get(), self.sensors, dt)
        if loco:
            self._locomotion_post_step()
        if action.type == ActionType.ANIMATE and action.params.get("name"):
            self._play_animate(action.params["name"])
        # v0.7.3 两段式吃鼠标：FSM 奔到光标（EAT_APPROACH→EAT_MOUSE 转换）
        # 才真正启动抑制；eat_mouse_tick 处理追赶超时兜底
        mode = self.fsm.mode
        if mode == "eat_mouse" and getattr(self, "_fsm_last_mode", "") != "eat_mouse":
            self._proactive.eat_mouse_arrived()
        # v0.15.1 接回：新引擎有效部分（motion + wind/sun）经中间层 EngineBridge
        # 逐帧叠加到原有引擎 frames。风/光影逐拍刷新（内部节流）；落地沿
        # squash 由 MotionEnricher 在 set_motion 的下降沿自触发。整块永不抛错。
        self._bridge.refresh_channels()
        vx, _vy = self.fsm.velocity
        tilt = max(-9.0, min(9.0, vx / 140.0))
        walking = (mode == "walk"
                   or (mode == "idle" and self.fsm.motion_mode == "follow"))
        hz = max(0.9, min(2.0, 0.9 + abs(vx) / 400.0))
        wg, wb = self._bridge.wind()
        self._bridge.set_motion(
            tilt_deg=tilt, walking=walking, walk_hz=hz,
            airborne=mode in ("fall", "thrown", "drag"),
            wind_gain=wg, wind_bias_deg=wb)
        enrichment = self._bridge.tick(dt * 1000.0)
        self.window.apply_enrichment(enrichment)
        set_motion_params = getattr(self.window, "set_motion_params", None)
        if callable(set_motion_params):
            set_motion_params(
                tilt_deg=tilt, walking=walking, walk_hz=hz,
                airborne=mode in ("fall", "thrown", "drag"),
                wind_gain=wg, wind_bias_deg=wb)
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
        # v0.18.12 三维呈现喂入（内部守卫 mode!=3d 即返回，零成本）
        self._render3d_tick()

    # ---- v0.3 动画 ----
    # H1 修（REVIEW-2026-08-25）：随机小动作 key 集——_frame_tick 的兜底停
    # 豁免这组（旧版 ANIMATE 刚启动就在同一 tick 被兜底停掉，永远不可见），
    # 终止改由 _play_animate 排的到期 singleShot 负责。
    _SMALL_ANIM_KEYS = ("stretch", "blink", "roll")
    # v0.19.0 F2：交互触发的临时动画 key 集——同上豁免（到期 singleShot 终止）
    _INTERACT_ANIM_KEYS = ("feed_chew",)

    def _play_animate(self, name: str) -> None:
        """随机小动作（v0.10.15 帧动画）：stretch/blink 播帧序列，
        roll 单帧定格；缺帧回退 get_frames（静帧）。"""
        key = {"stretch": "stretch", "blink": "blink", "roll": "roll"}.get(name)
        if key is None:
            return
        if isinstance(self.provider, AIArtProvider):
            # L21（REVIEW-2026-09-04）：paperdoll 档已有引擎级 blinkOn 贴片
            # （场景每 4.7s 自脉冲），帧版 blink 会切到烤死全帧渲染，6 sway
            # 件+腿件微动骤停 ~1.3s——重复且劣化，跳过（stretch/roll 保留）
            if name == "blink" and (
                    getattr(self.window, "skinned_motion_active", lambda: False)()
                    or (getattr(self, "_part_walk", False)
                        and self.window.part_walk_active())):
                return
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
            # L4（REVIEW-2026-09-04）：emoji 档行走 2 帧交替恢复——v0.10.15
            # 收帧驱动后 get_frames(MOVE_TO) 全仓零调用，行走中 emoji 宠物
            # 是静止贴图（v0.3 行为回归）。paperdoll/降级实例不受影响。
            if mode == "walk" and isinstance(provider, EmojiProvider):
                frames = provider.get_frames(
                    self.store.get(), ActionType.MOVE_TO)
                if len(frames) > 1:
                    self._play_key("emoji_walk", frames, loop=True,
                                   interval=260)
                    return
            if getattr(self, "_anim_key", None) == "emoji_walk":
                self._stop_anim()
            return
        stage = self.store.get().stage.value
        if prev_mode in ("fall", "thrown") and mode not in ("fall", "thrown"):
            land = provider.frames_for(stage, "fall")
            if len(land) > 1:
                self._anim_key = None  # 允许覆盖 air 循环
                self._play_key("land", [land[-1]], loop=False,
                               interval=provider.frame_interval("fall"))

                # L3（REVIEW-2026-09-04）：land 单帧序列无终止路径——旧版
                # 落地蹲伏定格到下次游走（free 5-15s）/随机小动作（edge
                # 15-35s），对齐小动作的到期 singleShot 工艺
                def _end_land() -> None:
                    if getattr(self, "_anim_key", None) == "land":
                        self._stop_anim()

                QTimer.singleShot(520, _end_land)
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
            if (getattr(self.window, "skinned_motion_active", lambda: False)()
                    or (getattr(self, "_part_walk", False)
                        and self.window.part_walk_active())):
                # 批次L/N3：裸读改 getattr——与本函数其他 _anim_key 读取一致
                if getattr(self, "_anim_key", None) == "walk":
                    self._stop_anim()
                return
            # 交互覆盖（喂食咀嚼）期间不抢播旧 walk 帧：咀嚼帧让蒙皮不可见，
            # 旧版此处落到帧路径把 feed_chew 换成循环 walk（到期 singleShot
            # 比对 key 失败、走完全程才停），侧身会话也随之丢 neglected 灰调
            if getattr(self, "_anim_key", None) in self._INTERACT_ANIM_KEYS:
                return
            walk = provider.frames_for(stage, "walk")
            if walk:
                self._play_key("walk", walk, loop=True,
                               interval=provider.frame_interval("walk", stage))
            return
        # H1 修：兜底停豁免小动作（stretch/blink/roll 由 _play_animate 的
        # 到期 singleShot 终止）——旧版这里把刚启动的小动作同 tick 停掉
        if (getattr(self, "_anim_key", None) not in (None, "land")
                and self._anim_key not in self._SMALL_ANIM_KEYS
                and self._anim_key not in self._INTERACT_ANIM_KEYS):
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
        for name in ("_proactive_timer", "_chat_emotion_timer", "_chat_emotion_expiry_timer", "_periodic_save_timer", "_save_timer",
                     "_sensor_timer", "_fullscreen_timer", "_tick_timer",
                     "_decay_timer", "_sig_timer"):
            t = getattr(self, name, None)
            if t is not None:
                try:
                    t.stop()
                except Exception:
                    pass
        # ①′ 收口情绪预热 worker（防「销毁运行中 QThread」原生崩溃）
        _ew = getattr(self, "_chat_emotion_worker", None)
        if _ew is not None:
            import shiboken6
            if shiboken6.isValid(_ew) and _ew.isRunning():
                try:
                    _ew.quit()
                    _ew.wait(2000)
                except Exception:
                    pass
            self._chat_emotion_worker = None
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
        # 批次H/L14（REVIEW-2026-08-31）：perm/mem 面板一并收口——旧版只
        # 关聊天引擎，两副引擎挂到进程退出（QML 对象树滞留 + 潜在告警）
        for attr in ("_mem_window", "_perm_window"):
            w = getattr(self, attr, None)
            if w is not None:
                try:
                    w.close()
                except Exception:
                    pass
        self._mem_engine = None
        self._perm_engine = None
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
