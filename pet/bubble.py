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

    def show(
        self,
        text: str,
        kind: BubbleType = BubbleType.INFO,
        duration_ms: int = 5000,
    ) -> None:
        self._kind = kind
        self._label.setText(text)
        self.resize(self._max_width, 56)
        self._position_at_top_center()
        super().show()
        self.raise_()
        if kind != BubbleType.WARNING:
            self._timer.start(duration_ms)

    def hide(self) -> None:
        super().hide()

    def _position_at_top_center(self) -> None:
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            return
        g = screen.availableGeometry()
        x = g.x() + (g.width() - self.width()) // 2
        self.move(x, g.y() + 8)
