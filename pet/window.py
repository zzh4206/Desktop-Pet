"""透明置顶浮窗薄基类 —— 平台适配与分工.md §二/§五。

``WindowBase`` 纯 Qt 无平台库；mac 平台 polish（NSWindow floating level 等）
在 ``window_mac.py``（``_mac`` 文件，平台库只进 ``_mac``/``platform.py``）。

v0.2 实装交互手势消解（单击/双击/右键，纯 Qt 跨平台逻辑填在此共享文件）+
signal 交互入口（``patRequested``/``feedRequested``/``cleanRequested``/
``pokeRequested``/``settingsRequested``/``quitRequested``）+ ``set_sprite_provider``/
``on_state_change``（订阅 ``PetStateStore.on_change`` 切 emoji）。window 不 import
业务模块，保持解耦。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QLabel, QMenu, QWidget

from .asset_provider import SpriteRef

# 手势消解阈值（设计思路.md §2.3）
_CLICK_MAX_PX = 5          # 位移 < 5px 才算点击
_CLICK_MAX_MS = 300        # 时长 < 300ms 才算点击
_DOUBLE_CLICK_MS = 500     # 双击间隔 < 500ms


class WindowBase(QWidget):
    """透明置顶浮窗薄基类（纯 Qt，无平台库）。mac/win 继承后做平台 polish。"""

    # v0.2 交互入口（§2.3 手势消解）：单击摸头 / 双击喂食 / 右键菜单
    patRequested = Signal()    # 单击：位移<5px 且时长<300ms（双击延迟消歧）
    feedRequested = Signal()   # 双击：Qt 双击事件
    cleanRequested = Signal()  # 右键菜单"洗澡"
    pokeRequested = Signal()   # 右键菜单"戳一戳"
    settingsRequested = Signal()
    quitRequested = Signal()
    followToggleRequested = Signal()  # v0.3 右键菜单"跟随鼠标"开关
    petMoved = Signal(float, float, int)  # v0.3 (cx, bottom_y, height) 气泡跟随
    # v0.3 拖拽：参数为全局 bottom_center 坐标（抓取偏移已在窗内算好）
    dragStarted = Signal(float, float)
    dragMoved = Signal(float, float)
    dragReleased = Signal(float, float)

    def __init__(self, sprite: SpriteRef, parent=None):
        super().__init__(parent)
        self._sprite = sprite
        self._anchor = sprite.anchor
        self._provider = None                 # 注入 AssetProvider（on_state_change 切 emoji）
        self._press_start: tuple | None = None
        self._dragging = False
        self._grab_dx = 0.0                   # 按下点 → 宠物 bottom_center 偏移
        self._grab_dy = 0.0

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

        # v0.3 帧动画：150ms/帧，播完回当前静帧
        self._static_sprite: SpriteRef = sprite
        self._frames: list = []
        self._frame_idx = 0
        self._frame_timer = QTimer(self)
        self._frame_timer.setInterval(150)
        self._frame_timer.timeout.connect(self._advance_frame)

    # ---- 渲染 ----
    def set_sprite(self, sprite: SpriteRef) -> None:
        self._sprite = sprite
        self._label.setText(sprite.path)
        if (sprite.width, sprite.height) != (self.width(), self.height()):
            self.resize(sprite.width, sprite.height)
            self._label.resize(sprite.width, sprite.height)
            # 尺寸变时更新 emoji 字体（__init__ setPointSizeF 只初始一次，
            # set_sprite 不 setFont → 进化后 window 变大但 emoji 字体没变）
            font = QFont()
            font.setPointSizeF(sprite.width * 0.62)
            self._label.setFont(font)

    def set_sprite_provider(self, provider) -> None:
        """注入 AssetProvider；on_state_change 据此换 sprite。"""
        self._provider = provider

    def play_frames(self, frames: list) -> None:
        """v0.3 播一段帧序列（AssetProvider.get_frames），播完回静帧。"""
        if not frames:
            return
        self._static_sprite = self._sprite  # 播完恢复
        self._frames = list(frames)
        self._frame_idx = 0
        self.set_sprite(self._frames[0])
        if len(self._frames) > 1:
            self._frame_timer.start()

    def _advance_frame(self) -> None:
        self._frame_idx += 1
        if self._frame_idx >= len(self._frames):
            self._frame_timer.stop()
            self._frames = []
            self.set_sprite(getattr(self, "_static_sprite", self._sprite))
            return
        self.set_sprite(self._frames[self._frame_idx])

    def on_state_change(self, state) -> None:
        """v0.2 订阅 PetStateStore.on_change → 按 state 切 emoji。"""
        if self._provider is not None:
            self.set_sprite(self._provider.get_static(state))

    def move_bottom_center(self, x: float, y: float) -> None:
        """(x, y) = bottom_center 点 → 算 top-left 后 move。

        Qt 层防御钳制：无论 FSM 给出什么坐标，窗口整体保持在屏幕合集内
        （FSM 已钳制作区，这里兜底多屏/异常值，防偶发消失）。"""
        from PySide6.QtGui import QGuiApplication

        screens = QGuiApplication.screens()
        if screens:
            geos = [s.geometry() for s in screens]
            min_x = min(g.x() for g in geos)
            min_y = min(g.y() for g in geos)
            max_x = max(g.x() + g.width() for g in geos)
            max_y = max(g.y() + g.height() for g in geos)
            x = min(max(x, min_x + self.width() / 2), max_x - self.width() / 2)
            y = min(max(y, min_y + self.height()), max_y)
        tx = int(x - self.width() / 2)
        ty = int(y - self.height())
        self.move(tx, ty)
        self.petMoved.emit(x, y, self.height())

    # ---- v0.2/v0.3 交互入口：手势消解（§2.3）+ 拖拽 ----
    def _bottom_center_global(self) -> tuple:
        return (
            self.x() + self.width() / 2.0,
            self.y() + self.height(),
        )

    def mousePressEvent(self, event):
        self._press_start = (
            event.position().x(),
            event.position().y(),
            event.timestamp(),
        )
        # v0.3 拖拽准备：记录抓取偏移（按住哪里就从哪里拎）
        g = event.globalPosition()
        bx, by = self._bottom_center_global()
        self._grab_dx = bx - g.x()
        self._grab_dy = by - g.y()

    def mouseReleaseEvent(self, event):
        if self._press_start is None:
            return
        x, y, t0 = self._press_start
        self._press_start = None
        delta = abs(event.position().x() - x) + abs(event.position().y() - y)
        dt_ms = float(event.timestamp() - t0)
        # 位移≥5px 或时长≥300ms → 拖拽（v0.3 实装），不触发单击
        if self._dragging:
            self._dragging = False
            g = event.globalPosition()
            self.dragReleased.emit(
                g.x() + self._grab_dx, g.y() + self._grab_dy
            )
            return
        if delta >= _CLICK_MAX_PX or dt_ms >= _CLICK_MAX_MS:
            return  # 短距长按：不判单击也不算有效拖拽
        # 延迟消歧：双击事件到达前不出单击
        self._single_shot.start()

    def mouseDoubleClickEvent(self, event):
        self._single_shot.stop()  # 吞掉第一次单击，双击生效
        self.feedRequested.emit()

    def mouseMoveEvent(self, event):
        # v0.3 拖拽：按住移动（位移超阈即进入）→ 宠物 bottom_center 钉住光标
        if self._press_start is None or event.buttons() == Qt.NoButton:
            return
        px, py, _t = self._press_start
        moved = (
            abs(event.position().x() - px) + abs(event.position().y() - py)
        )
        if not self._dragging and moved >= _CLICK_MAX_PX:
            self._dragging = True
            g = event.globalPosition()
            self.dragStarted.emit(g.x() + self._grab_dx, g.y() + self._grab_dy)
        if self._dragging:
            g = event.globalPosition()
            self.dragMoved.emit(g.x() + self._grab_dx, g.y() + self._grab_dy)

    def contextMenuEvent(self, event):
        # 右键菜单（§2.3）：喂食/洗澡/戳一戳/跟随鼠标 + 设置/退出
        menu = QMenu(self)
        menu.addAction("喂食", self.feedRequested.emit)
        menu.addAction("洗澡", self.cleanRequested.emit)
        menu.addAction("戳一戳", self.pokeRequested.emit)
        menu.addAction("跟随鼠标", self.followToggleRequested.emit)
        menu.addSeparator()
        menu.addAction("设置", self.settingsRequested.emit)
        menu.addAction("退出", self.quitRequested.emit)
        menu.exec(event.globalPosition().toPoint())
