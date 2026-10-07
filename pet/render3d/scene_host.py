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


class Render3DWindow:
    """QQuickWidget 场景宿主（v0.18.24：QQuickView→QQuickWidget）。

    QQuickWidget 是 QWidget——可直接 reparent 嵌入主窗（attach_render3d），
    且 WA_TransparentForMouseEvents 真正生效（事件落主窗=复用 2D 原生
    手势路径）；QQuickView+createWindowContainer 的 native surface 上该
    属性无效（事件被吞=交互全灭，实测）。2D rig（presenter._init_quick）
    的同款嵌入先例。顶层伴随模式照常可用（setWindowFlags+show）。

    延迟 import Qt：本模块被 bootstrap 在未启用时 import 也不拖 Qt 进内存。
    """

    def __init__(self, model_qml: str, cfg: dict, asset_dir: str | None = None):
        from PySide6.QtCore import QUrl, QTimer
        from PySide6.QtGui import QColor, Qt
        from PySide6.QtQuickWidgets import QQuickWidget

        self._view = QQuickWidget()
        self._view.setClearColor(QColor(0, 0, 0, 0))
        self._view.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._view.setResizeMode(QQuickWidget.ResizeMode.SizeRootObjectToView)
        self._view.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self._view.resize(int(cfg.get("window_size_w", 240)), int(cfg.get("window_size_h", 420)))
        self._view.setSource(QUrl.fromLocalFile(QML))
        if self._view.status() != QQuickWidget.Status.Error or not self._view.rootObject():
            pass
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
        """合成层保鲜脉冲（0.18.29）：请求 QQuickWidget 重绘——macOS 在
        Space 切换/显示器休眠/遮挡后可能持陈旧合成层（内容透明但窗仍
        "可见"，直到 expose 事件才恢复=用户报的图层消失），周期 update()
        保证最多 WATCHDOG_INTERVAL_MS 自愈。"""
        try:
            if not self.degraded:
                self._view.update()
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
