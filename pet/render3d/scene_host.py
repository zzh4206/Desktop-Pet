"""场景宿主（Qt 依赖）——QQuickView 直窗 + View3D 场景装载 + D16 看门狗。

形态依据 M1 spike 实测：QQuickView 直窗在 macOS 透明置顶全链路可用
（QQuickWidget 双壳亦可；等 S1.4 骨骼模型定了集成形态再定稿壳选型）。
D16 安全上限：看门狗周期自测 RSS，超 safe_rss_mb → hide + is_ready=False，
外层（EngineBridge/app）据此走 3D→2D 降级，用户无感。
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger("pet.render3d")

QML = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scene3d.qml")

# 看门狗缺省：D16 安全上限（增量 +500MB，基线=进程装载完 3D 后的首测值）
DEFAULT_SAFE_RSS_MB = 500.0
WATCHDOG_INTERVAL_MS = 5000


def _rss_mb() -> float:
    try:
        import psutil

        return psutil.Process().memory_info().rss / (1024 * 1024)
    except ImportError:
        import resource

        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


class _ExposeLogger:
    """RENDER3D_WD_DEBUG=1 的渲染表面事件记录器（QObject eventFilter）。"""

    def __init__(self, view):
        from PySide6.QtCore import QObject

        # 动态建 QObject 子类实例（避免模块级 import Qt 破坏延迟导入约定）
        class _F(QObject):
            def eventFilter(self, obj, event) -> bool:
                from PySide6.QtCore import QEvent

                t = event.type()
                if t in (QEvent.Type.Expose, QEvent.Type.Hide, QEvent.Type.Show,
                         QEvent.Type.HideToParent, QEvent.Type.ShowToParent,
                         QEvent.Type.WindowDeactivate, QEvent.Type.UpdateRequest):
                    import logging
                    logging.getLogger("pet.render3d").warning(
                        "R3EV %s visible=%s", str(t).split(".")[-1],
                        view.isVisible())
                return False

        self._f = _F(view)
        self.qobj = self._f


class Render3DWindow:
    """3D 场景宿主（v0.18.32：QQuickView 独立窗 / native surface 对照实验）。

    QQuickWidget 嵌入（FBO 贴图）在 mac 上连续暴露合成层问题（StackOnTop
    坑 / 失活 unexpose 停帧丢 FBO / repaint 与 AppKit nudge 均无效），
    故换 QQuickView（native surface）——理论上免疫 FBO 失活剔除。
    嵌入主窗时由 window.attach_render3d 用 createWindowContainer 包一层，
    交互由事件过滤器转发（eventFilter 把 native container 的鼠标事件转给
    本窗手势消解；WA_TransparentForMouseEvents 管不到 native 层命中测试，
    0.18.23 实测事件被吞）。
    顶层伴随模式照常可用（setFlags+show）。

    延迟 import Qt：本模块被 bootstrap 在未启用时 import 也不拖 Qt 进内存。
    """

    def __init__(self, model_qml: str, cfg: dict, asset_dir: str | None = None):
        from PySide6.QtCore import QUrl, QTimer
        from PySide6.QtGui import QColor, Qt
        # v0.18.32 架构对照实验：QQuickView 独立窗（native surface）——
        # QQuickWidget 嵌入（FBO 贴图）在 mac 上连续暴露合成层问题
        # （StackOnTop 坑/失活剔除/repaint 与 AppKit nudge 均无效），
        # native surface 理论上免疫。若实测通过则正式定型。
        from PySide6.QtQuick import QQuickView

        self._view = QQuickView()
        self._view.setColor(QColor(0, 0, 0, 0))
        self._view.setResizeMode(QQuickView.ResizeMode.SizeRootObjectToView)
        self._view.setFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self._view.resize(int(cfg.get("window_size_w", 240)), int(cfg.get("window_size_h", 420)))
        self._view.setSource(QUrl.fromLocalFile(QML))
        if self._view.rootObject() is None:
            errs = "; ".join(e.toString() for e in self._view.errors())
            raise RuntimeError(f"scene3d.qml 加载失败: {errs}")
        self._root = self._view.rootObject()
        # 模型 = Balsam 组件（Loader 装载，Joint 按骨名寻址）；贴图给 toon 材质
        self._root.setProperty("modelUrl", QUrl.fromLocalFile(model_qml).toString())
        tex = self._find_texture(asset_dir or os.path.dirname(model_qml))
        if tex:
            self._root.setProperty("textureUrl", QUrl.fromLocalFile(tex).toString())
        # D16 看门狗：基线=装载完的首测（含 mesh/引擎），超限即降级
        self._rss_baseline = _rss_mb()
        self._safe_rss_mb = float(cfg.get("safe_rss_mb", DEFAULT_SAFE_RSS_MB))
        self.degraded = False
        self._timer = QTimer()
        self._timer.setInterval(WATCHDOG_INTERVAL_MS)
        self._timer.timeout.connect(self._on_timer)
        self._timer.start()

        # 停帧保鲜独立 1s 定时器（RSS 看门狗保持 5s 不变）
        self._nudge_timer = QTimer()
        self._nudge_timer.setInterval(1000)
        self._nudge_timer.timeout.connect(self.nudge)
        self._nudge_timer.start()

        # 事件级日志检测（0.18.31 排障）：记录 expose/hide/show 事件——
        # 用户复现"消失"时日志直接给出发病瞬间真实状态
        self._dbg_on = bool(os.environ.get("RENDER3D_WD_DEBUG"))
        if self._dbg_on:
            self._dbg_filter = _ExposeLogger(self._view)
            self._view.installEventFilter(self._dbg_filter.qobj)

    def _on_timer(self) -> None:
        """看门狗 + 合成层保鲜（同一定时器，双职责）。"""
        self._check_rss()
        self.nudge()
        self._check_compositor()

    def _check_compositor(self) -> None:
        """3D 看门狗（0.18.30 收敛为**默认关闭**）：occlusionState 指标
        对「QQuickWidget 嵌入+LSUIElement」组合不可靠（实测 occ 恒 8192
        且 hide+show/raise 均翻不过来——发病判据无法与常态区分）。
        根治走 platform.pin_pet_float_window 的 NSWindow 锁层（Status
        WindowLevel）。此看门狗仅排障用：RENDER3D_WD_DEBUG=1 时打印
        occ 原始值（每 30s 一次）。"""
        import os as _os
        if not _os.environ.get("RENDER3D_WD_DEBUG"):
            return
        import time as _t
        if _t.monotonic() - getattr(self, "_wd_dbg", 0.0) < 30.0:
            return
        try:
            from ctypes import c_void_p

            from objc import objc_object

            self._wd_dbg = _t.monotonic()
            view = objc_object(c_void_p=int(self._view.winId()))
            nswin = view.window() if view is not None else None
            if nswin is not None:
                import logging
                logging.getLogger("pet.render3d").info(
                    "WD_DEBUG occ=%d level=%d visible=%s",
                    int(nswin.occlusionState()), int(nswin.level()),
                    bool(nswin.isVisible()))
        except Exception:
            pass

    def nudge(self) -> None:
        """合成层保鲜（0.18.32 QQuickView 版）：update() 调度渲染 +
        NSWindow setViewsNeedDisplay 强制合成器取帧。"""
        try:
            if self.degraded:
                return
            self._view.update()
            if getattr(self, "_nswin", None) is None:
                from ctypes import c_void_p

                from objc import objc_object

                view = objc_object(c_void_p=int(self._view.winId()))
                self._nswin = view.window() if view is not None else False
            if self._nswin:
                self._nswin.setViewsNeedDisplay_(True)
        except Exception:
            pass

    # ---- 窗口管理 ----

    def place_bottom_right(self) -> None:
        from PySide6.QtGui import QGuiApplication

        screen = QGuiApplication.primaryScreen().availableGeometry()
        g = self._view.geometry()
        self._view.move(screen.right() - g.width() - 40,
                        screen.bottom() - g.height() - 40)

    def show(self) -> None:
        self._view.show()

    def hide(self) -> None:
        self._view.hide()

    def close(self) -> None:
        try:
            self._timer.stop()
            self._view.close()
        except Exception:
            pass

    @property
    def root(self) -> Any:
        return self._root

    @property
    def win_id(self) -> int:
        return int(self._view.winId())

    # ---- D16 看门狗 ----

    def _check_rss(self) -> None:
        if self.degraded:
            return
        delta = _rss_mb() - self._rss_baseline
        if delta > self._safe_rss_mb:
            logger.warning("render3d RSS 增量 %.0fMB 超 %.0fMB 安全上限，降级回 2D",
                           delta, self._safe_rss_mb)
            self.degraded = True
            self.hide()

    # ---- 场景属性注入 ----

    def apply_uniforms(self, payload: dict) -> None:
        for k, v in payload.items():
            self._root.setProperty(k, v)

    def apply_pose(self, payload: dict) -> None:
        """bone_bridge 输出 {骨名: [qx,qy,qz,qw]} → QML posePayload 分发。"""
        self._root.setProperty("posePayload", payload)

    # ---- 交互回调（D11 整窗语义：调用方注册，缺省=无操作） ----

    def set_interaction(self, on_click=None, on_drag_start=None) -> None:
        """挂接 QML petClicked/petDragStarted 信号（app 侧决定语义：喂食/
        抚摸/提起等）。拖拽期间建议同步喂 PetSnapshot(airborne=True) → drag 姿势。"""
        for name, cb in (("petClicked", on_click), ("petDragStarted", on_drag_start)):
            sig = getattr(self._root, name, None)
            if sig is None or cb is None:
                continue
            try:
                sig.connect(cb)
            except Exception:  # noqa: BLE001
                logger.warning("render3d 交互信号挂接失败: %s", name)

    @staticmethod
    def _find_texture(asset_dir: str) -> str | None:
        import glob

        for pat in ("maps/textureData.png", "maps/*.png", "*.png"):
            hits = sorted(glob.glob(os.path.join(asset_dir, pat)))
            if hits:
                return hits[0]
        return None
