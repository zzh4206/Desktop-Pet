"""气泡系统（BubbleWidget）—— 接口冻结于 设计思路.md §2.4。

v0.1 骨架：能 ``show(text)`` 显示文字。v0.2 起接养成 / 主动关怀 / LLM。
本骨架暂定位在主屏工作区顶部居中（v0.2 再做“宠物头顶 20px 跟随”）。
"""

from __future__ import annotations

from enum import Enum

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget


class BubbleType(Enum):
    INFO = "info"
    WARNING = "warning"
    CHAT = "chat"


class BubbleWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)

        self._kind = BubbleType.INFO
        self._max_width = 220

        self._label = QLabel(self)
        self._label.setWordWrap(True)
        self._label.setAlignment(Qt.AlignCenter)
        self._label.setStyleSheet(
            "QLabel{background:rgba(45,45,48,232);color:#f2f2f2;"
            "border-radius:12px;padding:8px 14px;font:13px 'PingFang SC';}"
        )
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self._label)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide)
        self.resize(self._max_width, 56)
        self._anchor: tuple | None = None  # (cx, pet_bottom_y, pet_height)

    def show(
        self,
        text: str,
        kind: BubbleType = BubbleType.INFO,
        duration_ms: int = 5000,
        anchor: tuple | None = None,
    ) -> None:
        """anchor=(cx, 宠物bottom_y, 宠物height)：气泡挂宠物头顶 20px，
        靠屏幕顶自动翻到宠物下方（§2.4）。None 时回退工作区顶部居中。"""
        self._kind = kind
        self._label.setText(text)
        self.resize(self._max_width, 56)
        self._anchor = anchor
        self._reposition()
        super().show()
        self.raise_()
        if kind != BubbleType.WARNING:
            self._timer.start(duration_ms)

    def hide(self) -> None:
        super().hide()
        self._anchor = None

    def follow(self, anchor: tuple) -> None:
        """宠物移动时跟随（窗口 moved signal → app 调用）；未显示则忽略。"""
        if not self.isVisible():
            return
        self._anchor = anchor
        self._reposition()

    def _reposition(self) -> None:
        if self._anchor is not None:
            self._position_above_pet(*self._anchor)
        else:
            self._position_at_top_center()

    def _position_above_pet(self, cx: float, pet_y: float, pet_h: float) -> None:
        """宠物头顶 20px；宠物贴近屏幕顶则翻到宠物下方 20px。横向钳在屏内。"""
        from PySide6.QtGui import QGuiApplication

        screen = QGuiApplication.primaryScreen()
        if screen is None:
            return
        g = screen.availableGeometry()
        # 先按内容自适应高度（QLabel 换行后 sizeHint）
        self.adjustSize()
        bubble_x = int(min(max(cx - self.width() / 2, g.x() + 4),
                           g.x() + g.width() - self.width() - 4))
        above_y = int(pet_y - pet_h - 20 - self.height())
        if above_y >= g.y():
            self.move(bubble_x, above_y)
        else:
            self.move(bubble_x, int(pet_y + 20))
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            return
        g = screen.availableGeometry()
        x = g.x() + (g.width() - self.width()) // 2
        self.move(x, g.y() + 8)
