"""透明置顶浮窗薄基类 —— 平台适配与分工.md §二/§五。

v0.1 按“共享 + 薄基类 ``WindowBase``（透明/置顶/工作区/拖拽位移复用），mac 继承”
写。``WindowBase`` 纯 Qt 无平台库；mac 平台 polish（NSWindow floating level 等）
在 ``window_mac.py``（``_mac`` 文件，平台库只进 ``_mac``/``platform.py``）。

v0.1 不接 ``Renderer2D.draw``——直接用 ``QLabel`` 把 emoji 当文字画在透明窗上；
sprite-blit / get_frames 出帧是 v0.3 wire。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QLabel, QWidget

from .asset_provider import SpriteRef


class WindowBase(QWidget):
    """透明置顶浮窗薄基类（纯 Qt，无平台库）。mac/win 继承后做平台 polish。"""

    def __init__(self, sprite: SpriteRef, parent=None):
        super().__init__(parent)
        self._sprite = sprite
        self._anchor = sprite.anchor
        self._press_start: tuple | None = None

        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)

        self.resize(sprite.width, sprite.height)
        self._label = QLabel(self)
        self._label.resize(sprite.width, sprite.height)
        self._label.setAlignment(Qt.AlignCenter)
        self._label.setStyleSheet("background:transparent;")
        font = QFont()
        font.setPointSizeF(sprite.width * 0.62)
        self._label.setFont(font)
        self._label.setText(sprite.path)  # emoji 字符串

    def set_sprite(self, sprite: SpriteRef) -> None:
        self._sprite = sprite
        self._label.setText(sprite.path)
        if (sprite.width, sprite.height) != (self.width(), self.height()):
            self.resize(sprite.width, sprite.height)
            self._label.resize(sprite.width, sprite.height)

    def move_bottom_center(self, x: float, y: float) -> None:
        """(x, y) = bottom_center 点 → 算 top-left 后 move。"""
        tx = int(x - self.width() / 2)
        ty = int(y - self.height())
        self.move(tx, ty)

    # 交互 hook 预留（空实现，v0.2 填单击/双击/右键，v0.3 填拖拽）
    def mousePressEvent(self, event):
        self._press_start = (
            event.position().x(),
            event.position().y(),
            event.timestamp(),
        )

    def mouseReleaseEvent(self, event):
        self._press_start = None

    def mouseMoveEvent(self, event):
        pass
