"""透明置顶浮窗薄基类 —— 平台适配与分工.md §二/§五。

v0.1 按“共享 + 薄基类 ``WindowBase``（透明/置顶/工作区/拖拽位移复用），mac 继承”
写。``WindowBase`` 纯 Qt 无平台库；mac 平台 polish（NSWindow floating level 等）
在 ``window_mac.py``（``_mac`` 文件，平台库只进 ``_mac``/``platform.py``）。

v0.1 不接 ``Renderer2D.draw``——直接用 ``QLabel`` 把 emoji 当文字画在透明窗上；
sprite-blit / get_frames 出帧是 v0.3 wire。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QLabel, QMenu, QWidget

from .asset_provider import SpriteRef


class WindowBase(QWidget):
    """透明置顶浮窗薄基类（纯 Qt，无平台库）。mac/win 继承后做平台 polish。"""

    # v0.2 交互入口（§2.3 手势消解）：单击摸头 / 双击喂食 / 右键菜单
    patRequested = Signal()    # 单击：位移<5px 且时长<300ms（双击延迟消歧）
    feedRequested = Signal()   # 双击：间隔<500ms（Qt 双击事件，含系统双击阈值）
    cleanRequested = Signal()  # 右键菜单"洗澡"
    pokeRequested = Signal()   # 右键菜单"戳一戳"
    settingsRequested = Signal()
    quitRequested = Signal()

    def __init__(self, sprite: SpriteRef, parent=None):
        super().__init__(parent)
        self._sprite = sprite
        self._anchor = sprite.anchor
        self._press_start: tuple | None = None
        self._drag_candidate = False

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

        # 单击消歧：release 后延迟触发，期间来了双击则取消
        self._single_shot = QTimer(self)
        self._single_shot.setSingleShot(True)
        self._single_shot.setInterval(400)  # <500ms 双击窗口
        self._single_shot.timeout.connect(self.patRequested.emit)

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

    # v0.2 交互入口：手势消解（§2.3）。位移/时长阈值判定单击/拖拽候选。
    def mousePressEvent(self, event):
        self._press_start = (
            event.position().x(),
            event.position().y(),
            event.timestamp(),
        )
        self._drag_candidate = False

    def mouseReleaseEvent(self, event):
        if self._press_start is None:
            return
        x, y, t0 = self._press_start
        self._press_start = None
        delta = (
            abs(event.position().x() - x) + abs(event.position().y() - y)
        )
        dt_ms = float(event.timestamp() - t0)
        # 位移≥5px 或时长≥300ms → 拖拽候选（v0.3 填拖拽，不触发单击）
        if delta >= 5 or dt_ms >= 300:
            self._drag_candidate = True
            return
        # 延迟消歧：双击事件到达前不出单击
        self._single_shot.start()

    def mouseDoubleClickEvent(self, event):
        self._single_shot.stop()  # 吞掉第一次单击，双击生效
        self.feedRequested.emit()

    def mouseMoveEvent(self, event):
        # v0.3 填拖拽：_drag_candidate 为真时进入拖拽
        pass

    def contextMenuEvent(self, event):
        # 右键菜单（§2.3，v0.2 起）：喂食/洗澡/戳一戳 + 设置/退出
        menu = QMenu(self)
        menu.addAction("喂食", self.feedRequested.emit)
        menu.addAction("洗澡", self.cleanRequested.emit)
        menu.addAction("戳一戳", self.pokeRequested.emit)
        menu.addSeparator()
        menu.addAction("设置", self.settingsRequested.emit)
        menu.addAction("退出", self.quitRequested.emit)
        menu.exec(event.globalPos().toPoint())
