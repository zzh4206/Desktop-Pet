"""W1 Spike：Windows 透明置顶浮窗 + 点击穿透验证（v0.1 前置，见 平台适配与分工.md §六）。

验证目标：
1. 窗口透明、无边框、置顶，且不遮挡底层桌面的鼠标交互（背景穿透）；
2. 宠物渲染矩形（灰色圆形区域）可接收单击/双击/右键（不穿透）；
3. 记录手势消解所需原始数据（mousedown 起点 → mouseup 位移+时长），供 v0.2/v0.3 填入。

候选方案（本 Spike 双方案并测，结论写入文件头）：
- 方案A（单层窗 + 动态 setMask）：Qt setMask 之外的区域在 Windows 上天然点击穿透，
  mask 随宠物矩形移动更新。优点：单窗口、无层级同步问题；缺点：mask 是硬边缘，
  半透明渐变边缘会被裁掉。
- 方案B（双层窗）：底层全穿透（WS_EX_TRANSPARENT|WS_EX_LAYERED）纯渲染层
  + 顶层仅宠物矩形大小的交互层。优点：边缘抗锯齿完整；缺点：双窗同步、更复杂。

【W1 结论（待实测后填写）】：优先方案A，若边缘裁剪不可接受再切方案B。
"""

from __future__ import annotations

import sys
import time

from PySide6.QtCore import QPoint, QRect, Qt, QTimer
from PySide6.QtGui import QColor, QGuiApplication, QPainter, QRegion
from PySide6.QtWidgets import QApplication, QWidget

PET_SIZE = 96          # ADULT 显示尺寸（设计思路 §2.2）
MOVE_STEP = 4          # px / tick，用于验证 mask 跟随
TICK_MS = 16           # ~60fps 驱动（正式版动画限 30fps，此处仅验证穿透）


class SpikePetWindow(QWidget):
    """方案A：单层窗 + 动态 mask。灰色圆 = 宠物渲染矩形占位。"""

    def __init__(self) -> None:
        super().__init__(
            None,
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool,  # 不占任务栏
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        screen = QGuiApplication.primaryScreen().availableGeometry()
        self.setWindowTitle("W1 Spike")
        self.setGeometry(screen)  # 窗口铺满工作区，靠 mask 决定可见/可点区域

        # 宠物逻辑坐标（bottom_center 锚点，从底边中部出发）
        self._cx = screen.center().x()
        self._bottom = screen.bottom() + 1
        self._vx = MOVE_STEP

        # 手势消解原始数据（v0.2/v0.3 填入，这里只采集打印）
        self._press_pos: QPoint | None = None
        self._press_t: float = 0.0

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(TICK_MS)

    # ---- 几何 ----

    def _pet_rect(self) -> QRect:
        return QRect(
            self._cx - PET_SIZE // 2,
            self._bottom - PET_SIZE,
            PET_SIZE,
            PET_SIZE,
        )

    def _update_mask(self) -> None:
        # mask 外区域：Windows 上点击直接穿透到桌面
        self.setMask(QRegion(self._pet_rect(), QRegion.RegionType.Ellipse))

    def _tick(self) -> None:
        screen = self.geometry()
        if not screen.isEmpty():
            if self._cx + MOVE_STEP > screen.right() - PET_SIZE // 2:
                self._vx = -MOVE_STEP
            elif self._cx - MOVE_STEP < screen.left() + PET_SIZE // 2:
                self._vx = MOVE_STEP
            self._cx += self._vx
        self._update_mask()
        self.update()  # 触发重绘

    # ---- 渲染 ----

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(120, 160, 255, 200))
        painter.drawEllipse(self._pet_rect())
        painter.setPen(QColor(30, 30, 30))
        painter.drawText(
            self._pet_rect(), Qt.AlignCenter, "PET\n(click me)"
        )

    # ---- 手势 hook 预留（v0.2 填单击/双击/右键，v0.3 填拖拽） ----

    def mousePressEvent(self, event) -> None:  # noqa: N802
        self._press_pos = event.position().toPoint()
        self._press_t = time.monotonic()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if self._press_pos is None:
            return
        delta = (event.position().toPoint() - self._press_pos).manhattanLength()
        dt_ms = (time.monotonic() - self._press_t) * 1000
        print(
            f"[gesture] release: 位移={delta}px 时长={dt_ms:.0f}ms "
            f"按钮={event.button().name} → "
            f"{'单击/双击候选' if delta < 5 and dt_ms < 300 else '拖拽候选'}"
        )
        self._press_pos = None

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        # v0.3：位移≥5px 或时长≥300ms 进入拖拽
        pass


def main() -> int:
    app = QApplication(sys.argv)
    win = SpikePetWindow()
    win.show()
    print(
        "W1 Spike 运行中：圆形区域可点击（终端打印手势数据），"
        "圆形外桌面图标应可正常点选（背景穿透）。Ctrl+C 退出。"
    )
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
