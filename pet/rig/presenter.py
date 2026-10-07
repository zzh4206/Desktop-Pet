"""RigWindow —— 分层绑骨呈现后端（v0.13），``WindowBase`` 的同接口替换实现。

复用 ``WindowBase`` 全部交互（手势消解/右键菜单/拖放/移动模式），把画面
驱动换成 Qt Quick 场景（``rig_scene.qml``）：

* **交叉淡化**：帧序列逐帧过渡而非硬切（过渡占帧间隔的 ~45%）——既有多套
  验收帧直接变丝滑，且不新增任何生成像素 ⇒ 画风构造性一致；
* **常驻微动**：呼吸缩放；行走律动与速度倾斜由 app 经 ``set_motion_params``
  喂 FSM 真实速度/模式（WindowBase 有同名 no-op 缺省）；
* **部件层**：清单驱动的正弦摆动件（当前仅鲸尾），under_core 渲染在主体
  之下 —— 接缝被核心图天然遮挡；部件只绑定其来源 figure，动作帧展示期间
  activeFigure 不匹配自动隐藏，杜绝跨图错位。
* **降级铁律**：Qt Quick 初始化失败 / rig 资产缺失 → 经 ``build_rig_window``
  回退基类实例（QLabel 位图路径），永不阻断启动。

差异边界：``set_sprite``/``play_frames``/``stop_frames``/``_advance_frame``/
``set_facing`` 重写为场景驱动，但保留同名私有簿记字段（``_frames``/
``_static_sprite`` 等）语义 —— app 层 ``_frame_tick/_play_key`` 与测试观察
方式不变。

双槽状态机（刻意简化成单规范形）：静止时恒为"A 槽前景 + mix=0"；任何过渡
只写 B 槽并补间 mix→1；下一次操作先 ``_canonicalize()``——mix≥0.5 视为 B
已成前景，把它滚动进 A 槽再清空 B。因此无乒乓簿记、无完成回调链，中断与
自然完成走同一条收敛代码。
"""

from __future__ import annotations

import json
import logging
import math
import os
import time

import numpy as np

from PySide6.QtCore import QPropertyAnimation, QTimer, QUrl
from PySide6.QtGui import QFont, QImage

from ..asset_provider import SpriteRef
from ..window import WindowBase
from . import skinned_mesh_item  # noqa: F401 —— 注册 PetRig 1.0 QML 模块
from .gait import GaitSolver
from .side_locomotion import SideLocomotion, TurnClip
from .motion import MotionEngine, MotionInputs
from .spec import RigSpec, load_rig_spec

log = logging.getLogger("pet")

_STAGE_KEYS = ("young", "adult", "final")

# ---- 自适应逻辑拍（idle CPU 优化：静止期渲染频率减半）----
# 三档：16ms = side 编排活跃（对齐旧 _apply_loco 的 60Hz clip 播放）；
# 33ms = 活跃期（行走/空中/squash/眨眼/注视/步态窗口位移，与旧固定拍
# 一致）；66ms = 静止期（只剩呼吸 3.2s / 漂移 15.5s / 尾巴头发慢摆
# 1.8–2.9s 周期正弦——15Hz 采样每周期仍有 27+ 帧，视觉无差）。
# 省的是「渲染侧」：QML 属性写 / setBonePose 桥调用 / QSG update()
# （一帧重绘 + RHI 提交 + WindowServer 合成）随慢拍减半。引擎数学不
# 受降频影响：spring_step 内部 16.6ms 子步 + 隐式阻尼（任意 dt 绝对
# 稳定），相位量为线性累加。档位裁决统一收口 _adapt_tick。
_TICK_LOCO_MS = 16
_TICK_FAST_MS = 33
_TICK_SLOW_MS = 66


def figure_key_from_path(path: str) -> str | None:
    """静态立绘文件名反推 figure 名：``{stage}_{branch}_{mood}.png`` →

    ``{branch}_{mood}``。非该命名（帧/emoji 文本）返回 None（部件随之隐藏）。
    """
    stem = os.path.splitext(os.path.basename(path))[0]
    for st in _STAGE_KEYS:
        prefix = st + "_"
        if stem.startswith(prefix):
            return stem[len(prefix):] or None
    return None


def default_rig_root() -> str:
    return os.path.normpath(os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "..", "..", "assets", "rig"))


def build_rig_window(base_cls, sprite: SpriteRef, stage: str,
                     rig_root: str = "",
                     defer_quick: bool = False) -> WindowBase:
    """装配入口：任一环节不满足即返回 ``base_cls(sprite)``（旧行为原样）。

    三类降级点（Qt Quick 导入失败 / manifest 缺失 / 场景加载失败）全部收敛
    在此处一次判定，app 装配侧只看返回值。

    ``defer_quick=True``（app 实装用）：QQuickWidget/引擎延到事件循环首拍
    再建 —— v0.13.3 修：QQuickWidget 构造即建全进程**首个 QML 引擎**，而
    mem/perm/chat 三个 QML singleton 必须在首个引擎前注册（app.py 同源
    约束，实测于 PySide6 6.10；requirements 钉 >=6.5,<6.8，先注册在钉内
    版本无害，按 REVIEW-2026-09-04 L24 留档防御性保留），否则聊天面板
    "Cannot assign..." 载入失败。
    延迟后聊天引擎（_setup_chat 同步建）保持首位，rig 引擎退居其次。
    此路径下rig_active 在构造期尚为 False，以 ``_rig_pending`` 表示待就绪。
    """
    try:
        from PySide6.QtQuickWidgets import QQuickWidget  # noqa: F401
    except Exception as e:                # pragma: no cover - 环境缺件
        log.warning("rig 后端不可用（%s），回退帧动画", e)
        return base_cls(sprite)

    if not rig_root:
        rig_root = default_rig_root()
    spec = load_rig_spec(os.path.join(rig_root, stage), stage)
    if spec is None:
        log.info("无 %s 阶段 rig 清单，回退帧动画", stage)
        return base_cls(sprite)

    # 平台基类混入：RigWindow 直继承 WindowBase（纯 Qt）会丢 mac 侧
    # PetWindow 的 NSWindow polish（floating level / CanJoinAllSpaces /
    # Stationary）→ 切屏/切 Space 时不再置顶、不跟随全空间。v0.16 起
    # presentation=rig 才真正启用（此前强制 frames 走 PetWindow），此
    # 回归只在 rig 呈现路径暴露。动态子类让 rig 继承平台 PetWindow
    # （win 侧 PetWindow 为平凡子类，无额外 polish），fallback 语义不变。
    win_cls = type("_RigPlatformWindow", (RigWindow, base_cls), {})
    win = win_cls(sprite, spec, defer_quick=defer_quick)
    win._rig_root = rig_root   # 进化换档重载用（set_stage）
    if not (win.rig_active or getattr(win, "_rig_pending", False)):
        win.deleteLater()                 # 场景加载失败 → 换干净基类实例
        return base_cls(sprite)
    log.info("rig 后端就绪：%d figures / %d parts",
             len(spec.figures), len(spec.parts))
    return win


class RigWindow(WindowBase):
    """Qt Quick 驱动的呈现窗。构造即尽力初始化；失败时行为等同基类。"""

    # 类级缺省：WindowBase.__init__ 会先调 set_sprite，实例属性彼时尚未赋
    _quick_ok = False
    _quick = None
    _root = None
    _rig_pending = False
    _rig_root = ""             # build_rig_window 注入（set_stage 重载用）

    def __init__(self, sprite: SpriteRef, spec: RigSpec | None = None,
                 defer_quick: bool = False):
        super().__init__(sprite)
        self._spec = spec
        self._quick_ok = False
        self._quick = None                # QQuickWidget（成功后非 None）
        self._root = None                 # QML 根 Item
        self._skinned_item = None         # SkinnedMeshItem 节点（当蒙皮激活时）
        self._mix_anim: QPropertyAnimation | None = None
        self._fade_ms = 110
        self._src_size_cache: dict[str, tuple[int, int]] = {}
        self._src_bounds_cache: dict[str, tuple[int, int, int, int]] = {}
        self._air_prev = False            # 空中标志边沿检测（落地压扁）
        self._contact = 1.0               # P3 接触阴影：离地→收缩系数 lerp
        self._walk_sprite = None          # v0.14.4 行走覆盖图（neutral 核心）
        self._walk_showing = False
        self._engine = MotionEngine(spec) if spec is not None else None
        self._motion_inputs = MotionInputs()
        self._motion_timer: QTimer | None = None
        # v0.19.8 帧率分档：三档常数的实例副本（apply_frame_tier 改写；
        # 缺省=模块常数，即 medium 档，独立使用 presenter 时行为不变）
        self._tick_loco_ms = _TICK_LOCO_MS
        self._tick_fast_ms = _TICK_FAST_MS
        self._tick_slow_ms = _TICK_SLOW_MS
        # ---- v0.19 步态引擎（模块 3）：变 dt 时钟 + 原子提交 ----
        self._gait: GaitSolver | None = None
        self._gait_desired_vx = 0.0
        self._last_tick_s: float | None = None      # perf_counter 单调时钟
        # ---- G6 ADULT 侧身行走（side_locomotion）：缺省关闭，app 按配置启用 ----
        self._loco: SideLocomotion | None = None
        self._loco_pkg = ""
        self._loco_vx = 0.0
        self._side_item = None
        self._loco_last = None             # 最近一帧 LocoFrame（测试/门禁观察）
        self._loco_pending = ""            # defer_quick 期间收到的启用请求
        self._loco_carrying = False        # side session uses one rig; _sprite stays the logical mood
        self._loco_prewarm_frames: list[str] = []   # 片段帧缓存预热队列（_prewarm_clip_frames）
        self._setup_gait_solver()
        if spec is not None:
            if defer_quick:
                # 引擎延至事件循环首拍（见 build_rig_window docstring：
                # singleton 注册须先于首个 QML 引擎）。失败时 _init_quick
                # 自行降级为基类行为，无需换实例。
                self._rig_pending = True
                QTimer.singleShot(0, self._init_quick)
            else:
                self._init_quick()

    @staticmethod
    def _parts_model(spec: RigSpec) -> list:
        """spec.parts → QML partsModel（dict 列表）。_init_quick 与
        set_stage（进化换档重建）共用。"""
        parts = []
        for p in spec.parts:
            parts.append({
                "id": p.id,
                "_url": _file_url(p.path),
                "source_figure": p.source_figure,
                "px_rect": [float(v) for v in p.px_rect],
                "pivot": [float(v) for v in p.pivot],
                "z": p.z,
                "kind": p.kind,
                "base_deg": float(p.base_deg),
                "sway": {"amp_deg": float(p.amp_deg),
                         "period_ms": float(p.period_ms),
                         "phase_ms": float(p.phase_ms)},
            })
        return parts

    # ---------------- 场景初始化 ----------------
    def _init_quick(self) -> None:
        if getattr(self, "_r3_active", False):
            # 3D 互斥呈现已接管（attach 先于延迟初始化的时序）：跳过 2D QML
            # 场景创建，避免 2D/3D 重合（互斥双向守卫，另一侧在 attach 内）
            return
        try:
            from PySide6.QtCore import Qt
            from PySide6.QtQuickWidgets import QQuickWidget

            w = QQuickWidget(self)
            w.setClearColor(Qt.transparent)
            w.setAttribute(Qt.WA_TranslucentBackground, True)
            w.setAttribute(Qt.WA_AlwaysStackOnTop, True)
            # v0.13.4 关键：场景无任何交互 QML，必须对鼠标透明——否则实机
            # （D3D11 RHI）下 QQuickWidget 吞掉原生 WM_LBUTTONDOWN，WindowBase
            # 的手势/拖拽/右键全部失效（表现为"物理交互全灭"）。offscreen+
            # QTest 测不出此 bug：QTest 直接对目标控件投递事件，绕过 OS 命中
            # 分发（mock 盲区，真实验证须 SendInput 打实机窗口）。
            w.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            # 根 Item 无显式尺寸：默认 SizeViewToRootObject 会把控件缩成
            # 0×0（P0 spike 实测，spikes/spike_rig_qtquick.py）
            w.setResizeMode(QQuickWidget.ResizeMode.SizeRootObjectToView)

            parts = self._parts_model(self._spec)
            qml = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "rig_scene.qml")
            w.setSource(QUrl.fromLocalFile(qml))
            if w.status() == QQuickWidget.Status.Error or not w.rootObject():
                errs = "; ".join(e.toString() for e in w.errors())
                log.warning("rig 场景加载失败：%s", errs)
                w.deleteLater()
                return

            w.setGeometry(0, 0, self.width(), self.height())
            w.show()
            self._quick = w
            self._root = w.rootObject()
            self._label.hide()            # 场景接管后位图 label 不再参与
            self._quick_ok = True
            self._rig_pending = False
            self._motion_timer = QTimer(self)
            self._motion_timer.setInterval(_TICK_FAST_MS)
            self._motion_timer.timeout.connect(self._motion_tick)
            self._motion_timer.start()
            self._mix_anim = QPropertyAnimation(self._root, b"mix", self)
            self._root.setProperty("partsModel", parts)
            self._set_prop("facing", int(getattr(self, "_facing", 1)))
            self._setup_skinned_mesh()
            if getattr(self, "_loco_pending", ""):
                self.enable_side_locomotion(self._loco_pending)
            if os.path.isfile(self._sprite.path):
                self._show_now(self._sprite.path)
            else:
                # L2（REVIEW-2026-09-04）：rig+emoji 组合——初始 sprite 是
                # emoji 文本，场景让位 label 接管；旧版隐藏 label 又不上图，
                # 启动后 ~1s（首个 decay tick 前）画面空白
                self._quick.setVisible(False)
                self._label.show()
                super().set_sprite(self._sprite)
            # QQuickWidget 上屏会重置顶层 NSWindow 的 level/collectionBehavior
            # （defer_quick=True 时 _init_quick 在 showEvent polish 之后跑，
            # 覆盖掉 mac 的 floating/全 Space 设置）→ 场景初始化末尾重施加。
            self._reapply_platform_polish()
        except Exception as e:            # pragma: no cover - 环境缺件
            log.warning("Qt Quick 初始化失败，rig 回退 QLabel 路径：%s",
                        e, exc_info=True)
            self._quick_ok = False
            self._rig_pending = False     # 已裁定（降级），不再是"待就绪"

    def _setup_skinned_mesh(self) -> None:
        """若 spec 配置了 2D 骨骼蒙皮，向 QML 注入资产路径并提取 SkinnedMeshItem 节点。"""
        if not self._root or not self._spec:
            self._skinned_item = None
            return
        sp = getattr(self._spec, "skinned_spec", "")
        mp = getattr(self._spec, "skinned_mesh", "")
        lp = getattr(self._spec, "skinned_layers", "")
        if sp and mp and lp and os.path.isfile(sp) and os.path.isfile(mp) and os.path.isdir(lp):
            self._root.setProperty("skinnedMeshEnabled", False)
            self._root.setProperty("specFile", sp)
            self._root.setProperty("meshDataFile", mp)
            self._root.setProperty("layersDir", lp)
            self._root.setProperty("skinnedGroundYPx", float(getattr(self._spec, "ground_anchor_y_px", 0.0)))
            from PySide6.QtQuick import QQuickItem
            self._skinned_item = self._root.findChild(QQuickItem, "skinnedMesh")
            from PySide6.QtQuick import QQuickWindow
            enabled = (QQuickWindow.sceneGraphBackend() != "software"
                       and self._skinned_item is not None
                       and self._skinned_item.prepare())
            if enabled and self._skinned_item._rt is not None:
                tv = getattr(self._spec, "turn_views_file", "")
                if tv:
                    # 连续视角关键形态（§4）：挂接失败自动回退正面单视角
                    self._skinned_item._rt.attach_view_keyforms(tv)
                self._root.setProperty("skinnedSourceW", float(self._skinned_item._rt.img_w))
                self._root.setProperty("skinnedSourceH", float(self._skinned_item._rt.img_h))
            self._root.setProperty("skinnedMeshEnabled", enabled)
            sf = getattr(self._spec, "source_facing", -1 if (self._spec and self._spec.stage == "young") else 1)
            self._root.setProperty("skinnedSourceFacing", int(sf))
            log.info("RigWindow 蒙皮可用=%s（spec=%s, sourceFacing=%d）", enabled, sp, sf)
        else:
            self._root.setProperty("skinnedMeshEnabled", False)
            self._root.setProperty("skinnedGroundYPx", 0.0)
            self._skinned_item = None

    # ---------------- ADULT / FINAL 侧身行走 ----------------
    def enable_side_locomotion(self, pkg_dir: str) -> bool:
        """启用当前阶段的侧身行走包（ADULT / FINAL）。

        资产缺件 / 不支持的阶段 / 蒙皮不可用 → 保持旧路径。"""
        self.disable_side_locomotion()
        if not self.rig_active and getattr(self, "_rig_pending", False):
            self._loco_pending = pkg_dir          # 场景延迟初始化（defer_quick）：就绪后再启用
            return True
        self._loco_pending = ""
        if not self.rig_active or self._spec is None or self._spec.stage not in ("adult", "final"):
            return False
        spec_file = os.path.join(pkg_dir, "spec.json")
        mesh_file = os.path.join(pkg_dir, "mesh", "mesh_data.json")
        layers = os.path.join(pkg_dir, "layers")
        # 片段按窗口高度选 1×（256 高）/ 2×（512 高）/ 4×（1024 高，仅大窗口与参考导出）帧集
        # （内存门禁：单条解码 ≤ 12 MB；缺对应帧集时回退 512 高）
        win_h = float(self.height() or 256)
        suffix = "_h256" if win_h <= 320 else ("_h1024" if win_h >= 768 else "")
        clip_out = os.path.join(pkg_dir, "clips", "turn_front_to_side" + suffix)
        clip_in = os.path.join(pkg_dir, "clips", "turn_side_to_front" + suffix)
        if suffix and not os.path.isfile(os.path.join(clip_out, "clip.json")):
            clip_out = os.path.join(pkg_dir, "clips", "turn_front_to_side")
            clip_in = os.path.join(pkg_dir, "clips", "turn_side_to_front")
        try:
            needed = (spec_file, mesh_file, os.path.join(clip_out, "clip.json"),
                      os.path.join(clip_in, "clip.json"))
            if not all(os.path.isfile(x) for x in needed):
                raise FileNotFoundError("side rig / clip assets missing")
            if not self._root.property("skinnedMeshEnabled"):
                raise RuntimeError("front skinned mesh not active")
            with open(spec_file, "r", encoding="utf-8") as f:
                side_spec = json.load(f)
            canvas = tuple(float(v) for v in side_spec["skeleton"]["source_reference"]["image_size_px"])
            front = self._skinned_item._rt if self._skinned_item else None
            if (front is None or len(canvas) != 2 or
                    canvas != (float(front.img_w), float(front.img_h))):
                raise ValueError("side rig canvas does not match the current stage")
            out_clip, in_clip = TurnClip(clip_out), TurnClip(clip_in)
            if not all(os.path.isfile(fr.path) for clip in (out_clip, in_clip) for fr in clip.frames):
                raise FileNotFoundError("turn clip frame missing")
            from PySide6.QtQuick import QQuickItem
            self._root.setProperty("sideSpecFile", spec_file)
            self._root.setProperty("sideMeshDataFile", mesh_file)
            self._root.setProperty("sideLayersDir", layers)
            item = self._root.findChild(QQuickItem, "sideMesh")
            if item is None or not item.prepare():
                raise RuntimeError("side mesh item failed to prepare")
            self._loco_canvas = canvas
            scale = min(self.width() / canvas[0], self.height() / canvas[1])
            self._loco = SideLocomotion(side_spec, out_clip, in_clip, scale)
            self._side_item = item
            self._loco_pkg = pkg_dir
            self._root.setProperty("sideMeshEnabled", True)
            # 片段帧缓存预热（mac 首播卡顿第二轮：60Hz 拍内首次换帧需
            # 同步 PNG 解码 ~2-18ms 尖刺；逐帧预热入 QQuickPixmapCache）
            self._loco_prewarm_frames = [fr.path for clip in (out_clip, in_clip)
                                         for fr in clip.frames]
            QTimer.singleShot(33, self._prewarm_clip_frames)
            log.info("%s 侧身行走已启用：%s", self._spec.stage.upper(), pkg_dir)
            return True
        except Exception as e:
            log.warning("侧身行走不可用，保持旧行走路径：%s", e)
            self.disable_side_locomotion()
            return False

    def disable_side_locomotion(self) -> None:
        self._loco_pending = ""
        self._loco_prewarm_frames = []
        if self._root is not None:
            self._root.setProperty("locoMode", 0)
            self._root.setProperty("sideMeshEnabled", False)
            self._root.setProperty("clipFrameSrc", "")
            self._root.setProperty("sideSpecFile", "")
            self._root.setProperty("sideMeshDataFile", "")
            self._root.setProperty("sideLayersDir", "")
        self._release_loco_figure()
        self._loco = None
        self._loco_canvas = None
        self._loco_last = None
        self._side_item = None
        self._loco_vx = 0.0
        # interval 回落交 _adapt_tick 下一拍裁决（33/66ms）

    def locomotion_available(self) -> bool:
        return self._loco is not None and self.rig_active

    def _prewarm_clip_frames(self) -> None:
        """逐帧预热片段 QML 图像缓存（33ms 一帧，~2.5s 预热完 74 帧）。

        clipFrame Image 不可见（locoMode=0）但 source 变更即触发加载，
        cache:true 落入 QQuickPixmapCache——首播 60Hz 拍内不再有 PNG 同步
        解码尖刺。会话开始（loco.active）或禁用即停，余下帧由首播自解码。"""
        frames = self._loco_prewarm_frames
        if (self._root is None or not frames
                or self._loco is None or self._loco.active):
            self._loco_prewarm_frames = []
            return
        self._root.setProperty("clipFrameSrc", _file_url(frames.pop(0)))
        QTimer.singleShot(33, self._prewarm_clip_frames)

    def set_locomotion_intent(self, desired_vx: float) -> None:
        """行为层行走意图（逻辑 px/s，带方向；0 = 停）。会话内窗口 x 由编排驱动。"""
        self._loco_vx = float(desired_vx or 0.0)
        if self._loco is not None and abs(self._loco_vx) > 1.0:
            self._take_loco_figure()

    def _take_loco_figure(self) -> None:
        """Keep mood sprites out of the rig/clip session, including brake and turn-back.

        Each stage's bundle currently has one skinned character. Mood poses use this
        carrier during locomotion; the logical sprite remains the restoration target.
        Neglected uses the same geometry with its muted palette across all three modes.
        """
        if not self.rig_active or self._spec is None or self._spec.stage not in ("adult", "final"):
            return
        if self._walk_showing:
            self._walk_showing = False
            self._sprite = getattr(self, "_static_sprite", self._sprite)
        key = self._display_figure_key(self._sprite.path)
        carrier = self._spec.figures.get("healthy_neutral", "")
        if not key.startswith(("healthy_", "neglected_")) or not os.path.isfile(carrier):
            return
        self._loco_carrying = True
        self._root.setProperty("locoNeglected", key.startswith("neglected_"))
        if self._root.property("activeFigure") != "healthy_neutral":
            self._show_now(carrier)

    def _release_loco_figure(self) -> None:
        if not getattr(self, "_loco_carrying", False):
            return
        self._loco_carrying = False
        if self._root is not None:
            self._root.setProperty("locoNeglected", False)
        if self.rig_active and os.path.isfile(self._sprite.path) and not self._frames:
            self._show_now(self._sprite.path)

    def locomotion_controls_x(self) -> bool:
        return self._loco is not None and self._loco.active

    def locomotion_interrupt(self) -> None:
        if self._loco is not None and (self._loco.active or self._loco_carrying):
            self._apply_loco(self._loco.interrupt(), None)

    def _apply_loco(self, lf, frame) -> None:
        """LocoFrame → 场景（显示模式 / 片段帧 / 姿态推入 / 窗口 x）。"""
        self._loco_last = lf
        r = self._root
        mode = {"front": 0, "clip": 1, "side": 2}[lf.mode]
        r.setProperty("locoMode", mode)
        if lf.controls_x or mode:
            if getattr(self, "_facing", 1) != lf.facing:
                self._facing = lf.facing
                if self._motion_inputs is not None:
                    self._motion_inputs.facing = int(lf.facing)
            self._set_prop("facing", int(lf.facing))
        if (self._motion_timer is not None and self._loco is not None
                and self._loco.active):
            # 升频沿即时设（interrupt/侧身启动不等下一拍）；降频统一由
            # _adapt_tick 裁决（active=False 时下一拍回落 fast/slow 档）
            self._motion_timer.setInterval(self._tick_loco_ms)
        if mode == 1 and lf.clip is not None:
            fr = lf.clip.frames[lf.clip_index]
            x, y, w, h = fr.canvas_rect
            r.setProperty("clipCanvasX", float(x))
            r.setProperty("clipCanvasY", float(y))
            r.setProperty("clipCanvasW", float(w))
            r.setProperty("clipCanvasH", float(h))
            r.setProperty("clipFrameSrc", _file_url(fr.path))
            r.setProperty("clipOpacity", float(lf.clip_alpha))
            under = {"front": 0, "side": 2}.get(lf.under, -1)
            r.setProperty("clipUnder", under)
            r.setProperty("clipUnderOpacity", float(lf.under_alpha))
            if under == 2 and self._side_item is not None:
                self._pose_rest(self._side_item, {})
            elif under == 0 and self._skinned_item is not None:
                self._pose_rest(self._skinned_item, getattr(self._spec, "rest_pose_angles", {}) or {})
        if mode and frame is not None:
            # 片段/侧身：整体变换归零（片段帧是静止画面；侧身起伏由骨盆 dip 负责）
            r.setProperty("bodyAngle", 0.0)
            r.setProperty("bodyY", 0.0)
            r.setProperty("bodyScaleX", 1.0)
            r.setProperty("bodyScaleY", 1.0)
        if mode == 2 and frame is not None and self._side_item is not None:
            item = self._side_item
            f = self._settle_frame(frame, lf.settle, rest={})   # 侧身静止 = 全骨 0
            for b, deg in f.bone_angles.items():
                if b in item._rt.bone_index:
                    item.setBonePose(b, deg, f.bone_tx.get(b, 0.0), f.bone_ty.get(b, 0.0))
            item.setBlink(f.blink_progress)
            g = lf.gait
            if g is not None:
                k = float(lf.gait_scale)
                for bone, rad in g.bone_rotations.items():
                    ox, oy = g.bone_offsets.get(bone, (0.0, 0.0))
                    item.setBonePose(bone, math.degrees(rad) * k, tx=ox * k, ty=oy * k)
                sway, dip = g.pelvis_offset
                item.setBonePose("root_hip", math.degrees(g.bone_rotations.get("root_hip", 0.0)) * k,
                                 tx=sway * k, ty=dip * k)
        if mode == 2 and lf.window_x is not None:
            nx = int(round(lf.window_x))
            if nx != self.x():
                self.move(nx, self.y())
        if lf.mode == "front" and not lf.controls_x:
            self._release_loco_figure()

    @staticmethod
    def _pose_rest(item, rest: dict) -> None:
        """骨骼项摆到静止姿态（片段两端交叉淡化时垫在片段下方）。"""
        rt = getattr(item, "_rt", None)
        if rt is None:
            return
        for b in rt.bone_index:
            item.setBonePose(b, float(rest.get(b, 0.0)), 0.0, 0.0)
        item.setBlink(0.0)
        item.setLookAt(0.0, 0.0)

    def _settle_frame(self, frame, w: float, rest: dict | None = None):
        """骨骼姿态向静止姿态混合（转身片段首尾帧 = 静止渲染）；rest 缺省 = 正面 rest_pose_angles。"""
        if w <= 0.0:
            return frame
        import copy
        if rest is None:
            rest = getattr(self._spec, "rest_pose_angles", {}) or {}
        f = copy.copy(frame)
        k = 1.0 - w
        f.bone_angles = {b: a * k + w * float(rest.get(b, 0.0)) for b, a in frame.bone_angles.items()}
        f.bone_tx = {b: v * k for b, v in frame.bone_tx.items()}
        f.bone_ty = {b: v * k for b, v in frame.bone_ty.items()}
        f.blink_progress = frame.blink_progress * k
        f.look_at = (frame.look_at[0] * k, frame.look_at[1] * k)
        f.body_angle = frame.body_angle * k
        f.body_y = frame.body_y * k
        f.body_scale_x = math.copysign(1.0 + (abs(frame.body_scale_x) - 1.0) * k, frame.body_scale_x)
        f.body_scale_y = 1.0 + (frame.body_scale_y - 1.0) * k
        return f

    def _setup_gait_solver(self) -> None:
        """装配步态求解器（spec 带 gait 配置时；失败静默保持旧路径）。

        生产包（assets/rig/adult）无 gait 段 → 不激活，行为与旧版完全一致；
        新资产包（rig_adult_turn_v1）带 gait → 蒙皮渲染由步态骨骼驱动，
        窗口位移与姿态在同一渲染拍原子提交（§5.2）。
        """
        spec = self._spec
        if spec is None or not getattr(spec, "gait_config", None):
            self._gait = None
            return
        try:
            with open(spec.skinned_spec, "r", encoding="utf-8") as f:
                spec_data = json.load(f)
            win_h = float(self.height() or 256)
            scale = win_h / 1696.0 if win_h > 0 else 256.0 / 1696.0
            self._gait = GaitSolver(spec_data, window_scale=scale)
        except Exception as e:                # pragma: no cover —— 资产缺件
            log.warning("步态求解器装配失败，保持旧运动路径：%s", e)
            self._gait = None

    def set_gait_command(self, desired_vx: float) -> None:
        """行为层行走意图入口（逻辑 px/s，256 尺度；0=停止）。"""
        self._gait_desired_vx = float(desired_vx or 0.0)

    @property
    def gait_active(self) -> bool:
        return self._gait is not None

    def _gait_tick(self, dt: float) -> None:
        """步态原子提交（§5.2）：窗口位移与骨骼姿态同一拍生效。

        求解器内部维护浮点窗口累加器（防整数量化的系统性速度损失，见
        gait.update docstring）；本层按累加器落位窗口 int 坐标。
        """
        solver = self._gait
        if solver is None or not self.rig_active:
            return
        dragged = bool(getattr(self, "_dragging", False))
        grounded = bool(self._motion_inputs.grounded) if self._motion_inputs else True
        out = solver.update(dt, self._gait_desired_vx, (self.x(), self.y()),
                            is_grounded=grounded, is_dragged=dragged)
        # 窗口位移残差供 _adapt_tick 判档（停步残余收敛期不可降慢拍）
        self._gait_last_dx = out.delta_window_x
        if abs(out.delta_window_x) > 1e-9 and not dragged:
            # 与姿态同帧提交窗口位移（严禁跨帧延迟）
            self.move(int(round(solver.window_x_float)), self.y())
            item = self._skinned_item
            if item is not None and self._root.property("skinnedMeshVisible"):
                # 骨骼角（弧度→度）+ 骨盆平移（root_hip）+ 连续视角，一次推入
                for bone, rad in out.bone_rotations.items():
                    ox, oy = out.bone_offsets.get(bone, (0.0, 0.0))
                    item.setBonePose(bone, math.degrees(rad), tx=ox, ty=oy)
                sway, dip = out.pelvis_offset
                item.setBonePose("root_hip",
                                 math.degrees(out.bone_rotations.get("root_hip", 0.0)),
                                 tx=sway, ty=dip)
                self._root.setProperty("viewYaw", float(out.view_yaw))

    def _reapply_platform_polish(self) -> None:
        """场景初始化后重施加平台窗口 polish。

        RigWindow 经 build_rig_window 动态继承平台 PetWindow：mac 侧
        ``_polish_mac_window`` 施加 floating level / CanJoinAllSpaces /
        Stationary；win 侧 PetWindow 无此方法 → no-op。QQuickWidget 上屏
        会把顶层 NSWindow 的 level/collectionBehavior 重置，故须在
        ``_init_quick`` 末尾补一次（幂等，showEvent 首次 polish 不受影响）。
        """
        polish = getattr(self, "_polish_mac_window", None)
        if callable(polish):
            try:
                polish()
            except Exception:                # pragma: no cover - 环境缺件
                log.warning("rig 平台 polish 重施加失败", exc_info=True)

    @property
    def rig_active(self) -> bool:
        """场景是否在驱动画面（降级判定统一入口）。"""
        return self._quick_ok and self._root is not None

    def set_stage(self, stage: str) -> None:
        """进化换档（REVIEW-2026-08-31 H1）：重载该阶段清单 + 重建部件模型
        + 当前画面按新 spec 重解析。

        三阶段 manifest 共用 figure 键（healthy_neutral 等）——不换档则
        ``_resolve_display`` 把新阶段立绘映射回启动阶段的派生核心图，
        宠物在 rig/paperdoll 档视觉上"长不大"直到重启。新阶段清单缺失/
        非法 → 保持旧 spec（降级铁律：展示层永不因换档崩）。"""
        if self._spec is not None and stage == self._spec.stage:
            return
        root = self._rig_root or default_rig_root()
        spec = load_rig_spec(os.path.join(root, stage), stage)
        if spec is None:
            log.warning("rig 阶段 %s 清单缺失/非法，spec 保持 %s 不变",
                        stage, self._spec.stage if self._spec else None)
            return
        old = self._spec.stage if self._spec else None
        self.disable_side_locomotion()
        self._spec = spec
        self._engine = MotionEngine(spec)
        self._setup_gait_solver()
        self._src_size_cache.clear()
        self._src_bounds_cache.clear()
        log.info("rig 换档 %s → %s：%d figures / %d parts",
                 old, stage, len(spec.figures), len(spec.parts))
        if not self.rig_active:
            return
        self._root.setProperty("partsModel", self._parts_model(spec))
        self._setup_skinned_mesh()
        # 当前画面按新 spec 重解析（帧序列播放中不动——收尾路径自然重解）
        if self._walk_showing and self._walk_sprite is not None:
            self._show_now(self._walk_sprite.path)
        elif not self._frames and os.path.isfile(self._sprite.path):
            self._show_now(self._sprite.path)

    # ---------------- 渲染主路径（基类语义的场景版） ----------------
    def set_sprite(self, sprite: SpriteRef) -> None:
        """文件路径 → 场景同步直显；emoji 文本 → 还给基类 label 路径。"""
        self._sprite = sprite
        if getattr(self, "_r3_active", False):
            # 3D 互斥呈现：只记逻辑 sprite——场景/label 通道与 resize 全旁路
            # （不拦则 _quick 被 show 回来=2D/3D 重合复活；窗口被 resize 回
            # sprite 档=3D 画面被压缩，实测 240x420 被打回 256x256）
            return
        is_file = os.path.isfile(sprite.path)
        if self.rig_active and is_file:
            if not self._quick.isVisible():
                self._label.hide()
                self._quick.setVisible(True)
            if getattr(self, "_loco_carrying", False):
                key = self._display_figure_key(sprite.path)
                if key.startswith(("healthy_", "neglected_")):
                    self._take_loco_figure()
                else:
                    self.locomotion_interrupt()
                    self._show_now(sprite.path)
            else:
                self._show_now(sprite.path)
        elif self.rig_active and not is_file:
            self.locomotion_interrupt()
            # emoji 降级：场景让位避免双层叠加，label 接管
            self._quick.setVisible(False)
            self._label.show()
            super().set_sprite(sprite)
        else:
            super().set_sprite(sprite)     # 降级实例走全量旧路径

        # 与基类一致：SpriteRef 尺寸 ≠ 当前窗口时 resize（进化换档）
        if (sprite.width, sprite.height) != (self.width(), self.height()):
            self.resize(sprite.width, sprite.height)
            self._label.resize(sprite.width, sprite.height)
            font = QFont()
            font.setPointSizeF(sprite.width * 0.62)
            self._label.setFont(font)
            if self._quick is not None:
                self._quick.setGeometry(0, 0, sprite.width, sprite.height)

    def play_frames(self, frames: list, loop: bool = False,
                    interval_ms: int = 150) -> None:
        """序列播放；簿记语义与基类一致（L4 恢复目标规则）。

        v0.13.5 混合播放策略：**大位移动作（walk/chew/eat_mouse）硬切**，
        其余（表情/眨眼/小动作/落地帧）交叉淡化。依据：步姿两帧间腿部
        位移大，淡化=A 淡出+B 淡入=双影糊（用户实测"看不出迈步"）；
        经典 2/4 帧小跑的正确播法是快速硬切。表情类变化连续、淡化才丝滑。
        判定按首帧行为文件名前缀，零配置数据。
        """
        if frames and getattr(self, "_loco_carrying", False):
            self.locomotion_interrupt()
        if not frames or not self.rig_active \
                or not os.path.isfile(frames[0].path):
            # L2（REVIEW-2026-09-04）：非文件帧（emoji 文本，rig+emoji 组合
            # 或 AI 静态缺档降级）走基类 label 路径——旧版 emoji 串直接进
            # QImage/figASrc=垃圾 URL 污染场景状态
            super().play_frames(frames, loop, interval_ms)
            return
        # 批次C/P3-26（REVIEW-2026-09-05）：文件帧播前重显场景——emoji 降
        # 级 set_sprite 曾隐藏 _quick（场景让位 label），后续文件帧序列不
        # 重显则动画画进隐藏场景、label 停在旧 emoji（rig+emoji+文件帧组合）
        if not self._quick.isVisible():
            self._label.hide()
            self._quick.setVisible(True)
        if not self._frames:
            self._static_sprite = self._sprite
        self._frames = list(frames)
        self._frame_idx = 0
        self._frame_loop = bool(loop)
        self._sync_frame_palette()
        first = os.path.basename(self._frames[0].path)
        # 文件名形如 {stage}_walk_0.png —— 阶段前缀在前，须用子串判定
        # v0.14.3：stretch/roll/fall 也硬切——伸懒腰等动作的姿态幅度大
        # （手臂扬起/抬脚），淡化=新旧两图肢体同时半透明（实机目检抓到
        # 双臂双脚残影）。
        # 批次F/rM6（REVIEW-2026-08-28）：默认反转——名单外的新动作也
        # 硬切（旧版默认淡化，任何未登记的大姿态动作都会回归"双臂残影"，
        # 两轮实机事故同族）；只有已知的同姿态微变（blink 闭眼）保留淡化。
        if any(seg in first for seg in ("_walk_", "_chew_", "_eat_mouse_",
                                        "_stretch_", "_roll", "_fall_")):
            self._fade_ms = 0          # 硬切（既有名单）
        elif "_blink" in first:
            # 过渡时长 = 帧间隔的 ~45%，钳在 70–150ms（间隔过短也保底可读）
            self._fade_ms = int(min(max(interval_ms * 0.45, 70), 150))
        else:
            self._fade_ms = 0          # 未知动作默认硬切（宁可硬切不留残影）
        self._show_now(self._frames[0].path)
        if len(self._frames) > 1:
            self._frame_timer.setInterval(interval_ms)
            self._frame_timer.start()

    def stop_frames(self) -> None:
        if not self.rig_active:
            super().stop_frames()
            return
        if self._frames:
            self._frames = []
            self._frame_timer.stop()
            self._restore_after_sequence()

    def _restore_after_sequence(self) -> None:
        """序列收尾恢复：walking 覆盖期间回覆盖图（否则 mood 图在行进中
        闪现一拍，等下一个 walking 沿才被纠正）。"""
        self._sync_frame_palette()
        if self._walk_showing and self._walk_sprite is not None:
            self._show_now(self._walk_sprite.path)
            return
        self.set_sprite(getattr(self, "_static_sprite", self._sprite))

    def _sync_frame_palette(self) -> None:
        """动作帧各阶段共用彩色版：neglected（按播放前的恢复目标判定）播放期间
        开场景灰调层，序列收尾即关。与 locoNeglected 共用 mirrorNode 同一层。"""
        if self._root is None:
            return
        on = False
        if self._frames:
            target = getattr(self, "_static_sprite", None) or self._sprite
            key = figure_key_from_path(target.path) or ""
            on = key.startswith("neglected_")
        self._root.setProperty("frameNeglected", on)

    def _advance_frame(self) -> None:
        if not self.rig_active:
            super()._advance_frame()
            return
        self._frame_idx += 1
        if self._frame_idx >= len(self._frames):
            if getattr(self, "_frame_loop", False):
                self._frame_idx = 0
            else:
                self._frame_timer.stop()
                self._frames = []
                self._restore_after_sequence()
                return
        nxt = self._frames[self._frame_idx]
        self._transition_to(nxt.path, self._fade_ms)

    def set_facing(self, d: int) -> None:
        """镜像语义与基类相同；朝向同步写场景属性（即时翻转对齐旧行为）。

        批次F/H4（REVIEW-2026-08-28）：行走覆盖/帧序列播放中不换图——
        基类 set_facing 会 set_sprite(self._sprite)（mood 立绘）→
        activeFigure 变为无 limb 的 figure → part_walk_active()=False →
        隐藏的第三类帧回退（v0.14.4"只剩两类回退"承诺被破坏），且夹一拍
        mood 图闪现。与 on_state_change 同款守卫：只落 _facing + 场景
        facing 属性即时镜像，画面恢复交给既有收尾路径。
        """
        if d not in (-1, 1) or d == getattr(self, "_facing", 1):
            return
        if self._motion_inputs is not None:
            self._motion_inputs.facing = int(d)
        if self.rig_active and (self._walk_showing or self._frames):
            self._facing = d
            self._set_prop("facing", int(d))
            return
        super().set_facing(d)
        if self.rig_active:
            self._set_prop("facing", int(d))

    # ---------- v0.13 运动参数钩子（app._tick 每 tick 调用） ----------
    def set_motion_params(self, tilt_deg: float = 0.0, walking: bool = False,
                          airborne: bool = False, walk_hz: float = 0.0,
                          wind_gain: float = 1.0,
                          wind_bias_deg: float = 0.0) -> None:
        """喂 FSM 实况：倾斜目标角 / 行走律动开关 / 空中标志（落地沿→squash）/
        步态频率 Hz（v0.14，limb 部件与 bob/rot 共用；0=部件周期缺省）/
        风通道（v0.16：sway 幅度倍率 + 顺风偏置）。

        v0.15：运动数学已下沉 motion.MotionEngine——此处只更新引擎输入 +
        回写镜像属性（bodyTilt/walking/walkHz/squashAt 供测试与门禁观察），
        每帧姿态由 _motion_tick → engine.step 产出。
        """
        if not self.rig_active:
            return
        if self._motion_inputs is not None:
            self._motion_inputs.tilt_deg = float(tilt_deg)
            self._motion_inputs.walking = bool(walking)
            self._motion_inputs.walk_hz = float(walk_hz)
            self._motion_inputs.grounded = not bool(airborne)
            self._motion_inputs.facing = int(getattr(self, "_facing", 1))
            self._motion_inputs.wind_gain = float(wind_gain)
            self._motion_inputs.wind_bias_deg = float(wind_bias_deg)
        self._set_prop("bodyTilt", float(tilt_deg))
        self._set_prop("walking", bool(walking))
        self._set_prop("walkHz", float(walk_hz))
        self._walk_edge(bool(walking))
        if (not airborne) and self._air_prev:
            if self._engine is not None:
                self._engine.trigger_squash()
            self._set_prop("squashAt",
                           float(self._engine.squash_at
                                 if self._engine is not None else 0.0))
        self._air_prev = bool(airborne)

    # ---------- v0.17 光影通道 ----------
    def set_shadow(self, alpha: float = 0.0, offset_x: float = 0.0,
                   scale_x: float = 1.0, scale_y: float = 0.08,
                   airborne: bool = False) -> None:
        """太阳位置 → 地面阴影参数（写 QML 阴影项，见 pet/sun.py）。

        太阳是慢变量，app._tick 每 tick 重算并推入；QML 阴影项贴窗口底部、
        不受 bodyAngle/bodyScale 影响（影随太阳走，不随身体摇晃）。
        P3 接触阴影：离地（airborne=被抛/下落）时影子收缩变淡（lerp 0.6→1.0，
        设计 §4.5 随距地高度反比缩放）。
        """
        if not self.rig_active:
            return
        target = 0.6 if airborne else 1.0
        self._contact += (target - self._contact) * 0.15   # ~330ms 平滑
        k = self._contact
        self._set_prop("shadowAlpha", float(alpha) * (0.6 + 0.4 * k))
        self._set_prop("shadowOffsetX", float(offset_x))
        self._set_prop("shadowScaleX", float(scale_x) * k)
        self._set_prop("shadowScaleY", float(scale_y))

    def apply_enrichment(self, enrichment=None) -> None:
        """从 EngineBridge 接收光影参数（地面实时阴影）注入场景。"""
        if enrichment is None or not self.rig_active:
            return
        try:
            self.set_shadow(
                alpha=float(getattr(enrichment, "shadow_alpha", 0.0) or 0.0),
                offset_x=float(getattr(enrichment, "shadow_offset_x", 0.0) or 0.0),
                scale_x=float(getattr(enrichment, "shadow_scale_x", 1.0) or 1.0),
                scale_y=float(getattr(enrichment, "shadow_scale_y", 0.08) or 0.08),
            )
        except Exception:
            pass

    def _motion_tick(self) -> None:
        """运动逻辑拍：单调时钟实测 dt（§5.1，摒弃固定 33ms）。

        掉帧/卡顿恢复的巨帧被钳到 0.25s（后台暂停回来不瞬移）；dt 交给
        MotionEngine（弹簧 16.6ms 内部子步 + 隐式阻尼，任意 dt 绝对稳定）
        与步态求解器（≤5ms 相位子步），30/60Hz 与不均匀 dt 的轨迹一致
        （§7 时间一致性）。

        拍间自适应（idle CPU 优化）：interval 由 _adapt_tick 按活跃度
        三档切换（16/33/66ms）——静止期渲染侧（QML 属性写 / setBonePose
        桥调用 / QSG update）随慢拍减半。"""
        if not self.rig_active or self._engine is None:
            return
        now = time.perf_counter()
        if self._last_tick_s is None:
            self._last_tick_s = now
        dt = min(max(now - self._last_tick_s, 0.0), 0.25)
        self._last_tick_s = now
        if self._motion_inputs is not None:
            self._motion_inputs.source_facing = int(self._root.property("sourceFacing"))
            try:
                from PySide6.QtGui import QCursor
                c_pos = QCursor.pos()
                self._motion_inputs.cursor_pos = (float(c_pos.x()), float(c_pos.y()))
                self._motion_inputs.pet_rect = (float(self.x()), float(self.y()),
                                                float(self.width()), float(self.height()))
            except Exception:
                pass
        frame = self._engine.step(self._motion_inputs, dt * 1000.0)
        if self._loco is not None:
            cw, ch = self._loco_canvas
            self._loco.set_window_scale(min(self.width() / cw, self.height() / ch))
            # G7 边缘修复：会话可驱动范围 = 整窗在屏内（与 move_bottom_center
            # 同语义）。注意坐标系：_win_x 是窗口 **top-left x**（update 喂
            # self.x()、_apply_loco 按 move(nx, y) 落位），故范围是
            # [min_x, max_x - width] 而非中心 ±半宽——兜住步态收步过冲，防
            # 会话推窗出屏/app 拉回的边缘抖动
            loco_bounds = None
            sb = self._screen_bounds()
            if sb is not None:
                lo, hi = sb[0], sb[1] - self.width()
                if lo <= hi:
                    loco_bounds = (lo, hi)
                else:
                    c = (sb[0] + sb[1]) / 2.0 - self.width() / 2.0
                    loco_bounds = (c, c)
            # 动作帧播放期间不推进行走意图：play_frames 已打断会话，此处防
            # 同期意图把会话重新拉起——帧期间 _display_figure_key 恒空，
            # _take_loco_figure 接不上载体，neglected 灰调丢失（彩色侧身）
            loco_vx = 0.0 if self._frames else self._loco_vx
            lf = self._loco.update(dt, loco_vx, float(self.x()),
                                   grounded=bool(self._motion_inputs.grounded),
                                   dragged=bool(getattr(self, "_dragging", False)),
                                   bounds=loco_bounds)
            if lf.mode == "front":
                self._push_frame(self._settle_frame(frame, lf.settle))
            self._apply_loco(lf, frame)
            self._adapt_tick(frame, lf)
            return
        self._push_frame(frame)
        self._gait_tick(dt)
        self._adapt_tick(frame)

    def _adapt_tick(self, frame, lf=None) -> None:
        """按活跃度三档切换 motion timer：16 / 33 / 66ms。

        - 16ms：side 编排活跃（loco.active，转身 clip 60Hz 播放 + 侧身
          步态）。升频沿由 _apply_loco 即时设置（interrupt 等边沿不等
          下一拍），这里兜底一致；
        - 33ms（快拍，任一命中）：
          · walking / airborne：步态 bob·rot·四肢与空中物理；
          · gait_k > 0.01：停步后步态包络仍在衰减（τ=150ms，~750ms）；
          · squash 弹簧未收敛：压缩→过冲→回弹是全动画最快瞬态，
            慢拍会把回弹曲线采成折线（见 squash_settled）；
          · blink 脉冲内：130ms 脉冲在慢拍下只剩 2 帧（进入沿最多迟
            66ms——3–6s 一次的眨眼无感）；
          · look_at_busy：注视仍在收敛（动眼期）；
          · loco 非 front（clip/side）或 front 回正过渡（settle<1）；
          · 正面步态求解器仍在驱动窗口位移（desired_vx≠0 或上拍
            delta_window_x≠0——停步残余收敛期窗口移动不能降频）。
        - 66ms（慢拍）：只剩呼吸（3.2s）/漂移（15.5s）/尾巴头发慢摆
          （1.8–2.9s 周期正弦）——15Hz 采样视觉平滑，安全降频。"""
        loco = self._loco
        if loco is not None and loco.active:
            want = self._tick_loco_ms
        else:
            eng = self._engine
            inp = self._motion_inputs
            fast = (
                bool(inp is not None and inp.walking)
                or self._air_prev                   # set_motion_params 存的当前拍空中态
                or (eng is not None and eng.gait_k > 0.01)
                or (eng is not None and not eng.squash_settled)
                or bool(frame is not None and frame.blink_on)
                or (eng is not None and eng.look_at_busy)
                or bool(lf is not None
                        and (lf.mode != "front" or lf.settle < 0.999))
                or self._gait_moving()
            )
            want = self._tick_fast_ms if fast else self._tick_slow_ms
        timer = self._motion_timer
        # 不查 isActive：QA 工艺是停表手动步进 _motion_tick（qa_skinned_visual
        # 等皆 stop() 接管），守卫会让手动步进下切换永不发生；真实运行中本
        # 方法只经活跃 timer 的 timeout 进入，无停表路径。
        if timer is not None and timer.interval() != want:
            timer.setInterval(want)

    def _gait_moving(self) -> bool:
        """正面步态求解器是否仍在驱动窗口/姿态（降频会掉位移平滑度）。"""
        if getattr(self, "_gait_desired_vx", 0.0):
            return True
        return abs(getattr(self, "_gait_last_dx", 0.0)) > 1e-9

    def apply_frame_tier(self, loco_ms: int, fast_ms: int, slow_ms: int) -> None:
        """v0.19.8 帧率分档：换三档常数（FramePacer 注入）。

        interval 由 _adapt_tick 逐拍对齐（含升频沿 _apply_loco），这里只改
        值不碰 timer——最快下一拍（≤慢拍周期）即生效，无需即时重启。"""
        self._tick_loco_ms = max(8, int(loco_ms))
        self._tick_fast_ms = max(8, int(fast_ms))
        self._tick_slow_ms = max(int(self._tick_fast_ms), int(slow_ms))

    def pause_render(self) -> None:
        """暂停常驻运动+渲染循环（全屏/不可见时由 app 调用）。

        Qt 最小化/遮挡会切 system timer 驱动动画，但不会替自定义 QTimer
        停表（QQuickWidget 隐藏后 Python 侧 engine.step + setBonePose +
        update() 仍在 30Hz 空转）。停表只停「渲染推进」；逻辑时钟保留——
        衰减已用 wall-clock delta（app._apply_decay），恢复后数值正确、
        姿态从暂停处继续。frames 后端无此方法，app 经 getattr 幂等旁路。
        """
        if self._motion_timer is not None and self._motion_timer.isActive():
            self._motion_timer.stop()

    def resume_render(self) -> None:
        """恢复常驻运动+渲染循环（退出全屏/重新可见时由 app 调用）。"""
        if (self._motion_timer is not None and self.rig_active
                and not self._motion_timer.isActive()):
            self._motion_timer.start()

    def _push_frame(self, frame) -> None:
        """把 MotionFrame 一次性写到 QML（body 变换 + 眨眼 + 部件角度）。

        步态路径激活且接地时，body 级 bob/rotation/呼吸被置零——脚部世界
        接触由骨骼解算锁定，QML 整体变换的二次移动会破坏零滑步（计划 §7
        "时间与渲染前置"）。身体起伏已由骨盆 dip 在接触解算之前完成。
        """
        r = self._root
        gait_driven = (self._gait is not None
                       and r.property("skinnedMeshVisible")
                       and bool(self._motion_inputs.grounded))
        r.setProperty("bodyAngle", 0.0 if gait_driven else float(frame.body_angle))
        r.setProperty("bodyScaleX", float(frame.body_scale_x))
        r.setProperty("bodyScaleY", float(frame.body_scale_y))
        r.setProperty("bodyY", 0.0 if gait_driven else float(frame.body_y))
        r.setProperty("blinkOn", bool(frame.blink_on))
        r.setProperty("partAngles", dict(frame.part_angles))
        # 镜像属性（测试/门禁观察 gaitK/gaitPhase 的收敛与相位连续性）
        r.setProperty("gaitK", float(self._engine.gait_k))
        r.setProperty("gaitPhase", float(self._engine.gait_phase))

        # 2D 骨骼蒙皮姿态推入（若蒙皮节点存活）。步态路径下腿部/骨盆由
        # _gait_tick 的解算值接管（后写覆盖），其余骨骼（尾/发/臂）沿用引擎帧
        item = self._skinned_item
        if item is not None and r.property("skinnedMeshVisible"):
            for b, deg in frame.bone_angles.items():
                item.setBonePose(b, deg, frame.bone_tx.get(b, 0.0), frame.bone_ty.get(b, 0.0))
            item.setBlink(frame.blink_progress)
            item.setLookAt(frame.look_at[0], frame.look_at[1])

    def set_walk_figure(self, sprite) -> None:
        """行走覆盖图（v0.14.4）：walking 期间改显该 figure。

        paperdoll 的部件步态载体是 neutral 核心（唯一有 limb 腿部件）；
        mood 图（happy 跳姿/hungry 等）无腿部件，不覆盖则行走静默回退
        GPT 帧环——帧间烤死的手臂摆动/尾巴位移/色调差即实机报告的
        "手臂未遮挡 + 尾巴形态颜色微变"。walking 上升沿改显、下降沿还原。
        """
        self._walk_sprite = sprite
        if self._walk_showing and self.rig_active and sprite is not None:
            self._show_now(sprite.path)   # 覆盖图热替换（阶段进化换档）
        elif sprite is None and self._walk_showing:
            # L5（REVIEW-2026-09-04）：行走中覆盖图变 None（新档缺 side/
            # neutral 静态图）——旧版 _walk_edge 从此永早退，_walk_showing
            # 卡 True，on_state_change 只更新恢复目标不刷画面=冻结在旧图
            self._walk_showing = False    # 恢复目标切回静态图
            if not self._frames:
                self.set_sprite(getattr(self, "_static_sprite", self._sprite))

    def _walk_edge(self, walking: bool) -> None:
        if self._loco is not None:
            return                   # full side session owns its carrier, including stop/turn-back
        if not self.rig_active or self._walk_sprite is None:
            return
        if walking and not self._walk_showing:
            self._walk_showing = True
            self._show_now(self._walk_sprite.path)
        elif not walking and self._walk_showing:
            self._walk_showing = False
            # 序列播放中（如 blink 尾巴）只落标志，恢复交给既有收尾路径
            if not self._frames:
                self.set_sprite(getattr(self, "_static_sprite", self._sprite))

    def on_state_change(self, state) -> None:
        """行走覆盖期间只更新恢复目标、不动当前画面——否则衰减 tick 每 1s
        把 neutral 覆盖图翻回 mood 图，与下一拍 _walk_edge 打架=闪烁。"""
        if self._provider is None:
            return
        # 并行线（v0.15.x 聊天情绪）的 set_conversation_mood 会在
        # _last_state 未设时传 None——get_static(None) 每启动刷一条
        # AttributeError traceback（实机日志 2026-09-01 实证），守卫之
        if state is None:
            return
        self._last_state = state
        sprite = self._provider.get_static(
            state, mood_override=getattr(self, "_conversation_mood", None))
        if self._walk_showing or self._frames:
            self._static_sprite = sprite
            self._sync_frame_palette()   # 帧期间分支变化（进化/重置）即时跟随
            return
        self.set_sprite(sprite)

    def skinned_motion_active(self) -> bool:
        """Whether the visible mesh already provides walking and blinking."""
        return bool(self.rig_active and self._root.property("skinnedMeshVisible"))

    def part_walk_active(self) -> bool:
        """当前展示 figure 是否挂有 limb 部件（部件驱动步态可用，v0.14）。

        动作帧播放期间 activeFigure 为帧名反推（多为 None）→ False，
        天然与"帧序列展示期间部件隐藏"的既有机制一致。
        """
        if not (self.rig_active and self._spec is not None):
            return False
        if self._root.property("skinnedMeshVisible"):
            return True
        key = self._root.property("activeFigure") or ""
        return any(p.kind == "limb" and p.source_figure == key
                   for p in self._spec.parts)

    # ---------------- 场景私有工具 ----------------
    def _set_prop(self, name: str, value) -> None:
        if self.rig_active:
            self._root.setProperty(name, value)

    def _src_size(self, path: str) -> tuple[int, int]:
        size = self._src_size_cache.get(path)
        if size is None:
            img = QImage(path)
            size = (img.width(), img.height())
            self._src_size_cache[path] = size
        return size

    def _source_bounds(self, path: str) -> tuple[int, int, int, int]:
        """Visible bounds, cached per sprite; padding does not determine FINAL's size."""
        bounds = self._src_bounds_cache.get(path)
        if bounds is None:
            img = QImage(path).convertToFormat(QImage.Format_RGBA8888)
            if img.isNull():
                return (0, 0, 1, 1)
            rgba = np.frombuffer(img.constBits(), np.uint8).reshape(
                img.height(), img.bytesPerLine() // 4, 4)[:, :img.width()]
            ys, xs = np.where(rgba[:, :, 3] >= 24)
            bounds = ((int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1)
                      if len(xs) else (0, 0, img.width(), img.height()))
            self._src_bounds_cache[path] = bounds
        return bounds

    def _align_static_figure(self, path: str, display: str) -> None:
        key = self._display_figure_key(path, display)
        align = bool(self._spec and self._spec.stage == "final"
                     and key.startswith(("healthy_", "neglected_"))
                     and key != "healthy_side")
        if align:
            # Provider sprites include the whole figure; a derived core omits parts.
            bounds_path = path
            if os.path.abspath(path) == os.path.abspath(display):
                assets_dir = os.path.dirname(os.path.dirname(self._spec.skinned_spec))
                original = os.path.join(assets_dir, "ai", f"final_{key}.png")
                if os.path.isfile(original):
                    bounds_path = original
            self._root.setProperty("staticBounds", list(self._source_bounds(bounds_path)))
        self._root.setProperty("staticAlignEnabled", align)

    def _canonicalize(self) -> None:
        """双槽状态收敛回规范形"A 前景 + mix=0"（中断与完成共用一条路）。"""
        anim = self._mix_anim
        if anim.state() == QPropertyAnimation.State.Running:
            anim.stop()
        mix = float(self._root.property("mix") or 0.0)
        if mix >= 0.5:                    # B 已主导 → 滚动进 A 槽
            bsrc = self._root.property("figBSrc") or ""
            if bsrc:
                self._root.setProperty("figASrc", bsrc)
            self._root.setProperty("figBSrc", "")
        self._root.setProperty("mix", 0.0)

    def _resolve_display(self, path: str) -> str:
        """静态立绘 → 派生核心图（部件挖除版）的翻译。

        provider 只会给 assets/ai 原图（带完整烘焙尾件）；若该 figure 存在
        派生件而不做替换，静止画面将出现"原图整尾 + 下方摆动件"重影。
        非 figure 命名（动作帧等）原样返回。"""
        key = figure_key_from_path(path)
        if key and self._spec is not None:
            mapped = self._spec.figure_for(key)
            if mapped:
                return mapped
        return path

    def _display_figure_key(self, path: str, display: str = "") -> str:
        if self._frames:
            return ""  # actions must always display their own frame
        key = figure_key_from_path(path) or figure_key_from_path(display)
        if key:
            return key
        if self._spec:
            for name, figure in self._spec.figures.items():
                if os.path.abspath(path) == os.path.abspath(figure):
                    return name
        return ""

    def _show_now(self, path: str) -> None:
        """同步直显（无过渡）：A 槽显源、刷新源图尺寸与部件绑定名。"""
        if not self.rig_active:
            return
        self._canonicalize()
        disp = self._resolve_display(path)
        w, h = self._src_size(disp)
        self._root.setSourceSize(w, h)
        self._align_static_figure(path, disp)
        self._root.setProperty("figASrc", _file_url(disp))
        self._root.setProperty("figBSrc", "")
        self._root.setProperty("mix", 0.0)
        key = self._display_figure_key(path, disp)
        self._set_prop("activeFigure", key)

    def _transition_to(self, path: str, fade_ms: int) -> None:
        """切到下一图源。

        fade_ms>0：交叉淡出到 B 槽（mix 0→1，见模块 docstring）。
        fade_ms<=0（v0.13.6 硬切）：**直接换前景槽**，B 槽保持清空——
        绝不让两帧叠放：双槽叠放下旧帧恒不透明，会从新帧的透明区域
        （迈步扫开的腿档）透出来 = "多手多脚"残影。
        """
        if not self.rig_active:
            return
        if fade_ms <= 0:
            self._canonicalize()
            w, h = self._src_size(path)
            self._root.setSourceSize(w, h)
            self._align_static_figure(path, path)
            self._root.setProperty("figASrc", _file_url(path))
            self._root.setProperty("figBSrc", "")
            self._root.setProperty("mix", 0.0)
            self._set_prop("activeFigure", self._display_figure_key(path))
            return
        self._canonicalize()
        disp = self._resolve_display(path)
        w, h = self._src_size(disp)
        self._root.setSourceSize(w, h)
        self._align_static_figure(path, disp)
        self._root.setProperty("figBSrc", _file_url(disp))
        self._set_prop("activeFigure", self._display_figure_key(path, disp))
        anim = self._mix_anim
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setDuration(int(fade_ms))
        anim.start()

    def resizeEvent(self, event):     # noqa: N802（Qt 命名约定）
        super().resizeEvent(event)
        if self._quick is not None:
            self._quick.setGeometry(0, 0, self.width(), self.height())


def _file_url(path: str) -> str:
    return QUrl.fromLocalFile(os.path.abspath(path)).toString()
