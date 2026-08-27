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

import logging
import os

from PySide6.QtCore import QPropertyAnimation, QTimer, QUrl
from PySide6.QtGui import QFont, QImage

from ..asset_provider import SpriteRef
from ..window import WindowBase
from .spec import RigSpec, load_rig_spec

log = logging.getLogger("pet")

_STAGE_KEYS = ("young", "adult", "final")


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
    mem/perm/chat 三个 QML singleton 按 PySide6 6.10 约束必须在首个引擎前
    注册（app.py:349 注释），否则聊天面板 "Cannot assign..." 载入失败。
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

    win = RigWindow(sprite, spec, defer_quick=defer_quick)
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

    def __init__(self, sprite: SpriteRef, spec: RigSpec | None = None,
                 defer_quick: bool = False):
        super().__init__(sprite)
        self._spec = spec
        self._quick_ok = False
        self._quick = None                # QQuickWidget（成功后非 None）
        self._root = None                 # QML 根 Item
        self._mix_anim: QPropertyAnimation | None = None
        self._fade_ms = 110
        self._src_size_cache: dict[str, tuple[int, int]] = {}
        self._air_prev = False            # 空中标志边沿检测（落地压扁）
        if spec is not None:
            if defer_quick:
                # 引擎延至事件循环首拍（见 build_rig_window docstring：
                # singleton 注册须先于首个 QML 引擎）。失败时 _init_quick
                # 自行降级为基类行为，无需换实例。
                self._rig_pending = True
                QTimer.singleShot(0, self._init_quick)
            else:
                self._init_quick()

    # ---------------- 场景初始化 ----------------
    def _init_quick(self) -> None:
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

            parts = []
            for p in self._spec.parts:
                parts.append({
                    "id": p.id,
                    "_url": _file_url(p.path),
                    "source_figure": p.source_figure,
                    "px_rect": [float(v) for v in p.px_rect],
                    "pivot": [float(v) for v in p.pivot],
                    "z": p.z,
                    "sway": {"amp_deg": float(p.amp_deg),
                             "period_ms": float(p.period_ms),
                             "phase_ms": float(p.phase_ms)},
                })
            w.rootContext().setContextProperty("partsModelInit", parts)

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
            self._mix_anim = QPropertyAnimation(self._root, b"mix", self)
            self._root.setProperty("partsModel", parts)
            self._set_prop("facing", int(getattr(self, "_facing", 1)))
            if os.path.isfile(self._sprite.path):
                self._show_now(self._sprite.path)
        except Exception as e:            # pragma: no cover - 环境缺件
            log.warning("Qt Quick 初始化失败，rig 回退 QLabel 路径：%s",
                        e, exc_info=True)
            self._quick_ok = False
            self._rig_pending = False     # 已裁定（降级），不再是"待就绪"

    @property
    def rig_active(self) -> bool:
        """场景是否在驱动画面（降级判定统一入口）。"""
        return self._quick_ok and self._root is not None

    # ---------------- 渲染主路径（基类语义的场景版） ----------------
    def set_sprite(self, sprite: SpriteRef) -> None:
        """文件路径 → 场景同步直显；emoji 文本 → 还给基类 label 路径。"""
        self._sprite = sprite
        is_file = os.path.isfile(sprite.path)
        if self.rig_active and is_file:
            if not self._quick.isVisible():
                self._label.hide()
                self._quick.setVisible(True)
            self._show_now(sprite.path)
        elif self.rig_active and not is_file:
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
        """交叉淡化序列播放；簿记语义与基类一致（L4 恢复目标规则）。"""
        if not frames or not self.rig_active:
            super().play_frames(frames, loop, interval_ms)
            return
        if not self._frames:
            self._static_sprite = self._sprite
        self._frames = list(frames)
        self._frame_idx = 0
        self._frame_loop = bool(loop)
        # 过渡时长 = 帧间隔的 ~45%，钳在 70–150ms（间隔过短也保底可读）
        self._fade_ms = int(min(max(interval_ms * 0.45, 70), 150))
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
            self.set_sprite(getattr(self, "_static_sprite", self._sprite))

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
                self.set_sprite(
                    getattr(self, "_static_sprite", self._sprite))
                return
        nxt = self._frames[self._frame_idx]
        self._transition_to(nxt.path, self._fade_ms)

    def set_facing(self, d: int) -> None:
        """镜像语义与基类相同；朝向同步写场景属性（即时翻转对齐旧行为）。"""
        if d not in (-1, 1) or d == getattr(self, "_facing", 1):
            return
        super().set_facing(d)
        if self.rig_active:
            self._set_prop("facing", int(d))

    # ---------- v0.13 运动参数钩子（app._tick 每 tick 调用） ----------
    def set_motion_params(self, tilt_deg: float = 0.0, walking: bool = False,
                          airborne: bool = False) -> None:
        """喂 FSM 实况：倾斜目标角 / 行走律动开关 / 空中标志（落地沿→squash）。"""
        if not self.rig_active:
            return
        self._set_prop("bodyTilt", float(tilt_deg))
        if bool(walking) != bool(self._root.property("walking")):
            self._set_prop("walking", bool(walking))
        if (not airborne) and self._air_prev:
            self._root.squash()           # 空中→地面 边沿触发压扁回弹
        self._air_prev = bool(airborne)

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

    def _show_now(self, path: str) -> None:
        """同步直显（无过渡）：A 槽显源、刷新源图尺寸与部件绑定名。"""
        if not self.rig_active:
            return
        self._canonicalize()
        disp = self._resolve_display(path)
        w, h = self._src_size(disp)
        self._root.setSourceSize(w, h)
        self._root.setProperty("figASrc", _file_url(disp))
        self._root.setProperty("figBSrc", "")
        self._root.setProperty("mix", 0.0)
        key = figure_key_from_path(path) or figure_key_from_path(disp)
        self._set_prop("activeFigure", key or "")

    def _transition_to(self, path: str, fade_ms: int) -> None:
        """交叉淡出到下一图源：目标恒写 B 槽，mix 0→1（见模块 docstring）。"""
        if not self.rig_active:
            return
        self._canonicalize()
        disp = self._resolve_display(path)
        w, h = self._src_size(disp)
        self._root.setSourceSize(w, h)
        self._root.setProperty("figBSrc", _file_url(disp))
        key = figure_key_from_path(path)
        if key:
            self._set_prop("activeFigure", key)
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
