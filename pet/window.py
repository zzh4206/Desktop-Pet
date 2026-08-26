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

from collections import OrderedDict

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QActionGroup, QFont
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
    motionModeRequested = Signal(str)  # "follow" / "free" / "edge"
    petMoved = Signal(float, float, int)  # v0.3 (cx, bottom_y, height) 气泡跟随
    # v0.3 拖拽：参数为全局 bottom_center 坐标（抓取偏移已在窗内算好）
    dragStarted = Signal(float, float)
    fileDropped = Signal(str)      # v0.9 拖拽文件/文件夹（快捷启动器）
    dragMoved = Signal(float, float)
    dragReleased = Signal(float, float)

    def __init__(self, sprite: SpriteRef, parent=None):
        super().__init__(parent)
        self._sprite = sprite
        self._anchor = sprite.anchor
        self._provider = None                 # 注入 AssetProvider（on_state_change 切 emoji）
        self._press_start: tuple | None = None
        self._dragging = False
        self._motion_mode = "free"
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
        # H3 修（REVIEW-2026-08-25）：OrderedDict 真 LRU——命中 move_to_end、
        # 逐出 popitem(last=False)。旧版普通 dict 命中不重排，"LRU"实为 FIFO。
        self._pix_cache: OrderedDict = OrderedDict()
        self._facing = 1  # v0.10.16 朝向（1=右/-1=左，帧素材面朝右）
        self.set_sprite(sprite)
        # v0.10：构造期走统一 set_sprite（图片/emoji 分支），
        # 此后不得再 setText("")——会清掉刚显示的表情/位图（启动空屏回归）

        # 单击消歧：release 后延迟触发，期间来了双击则取消
        self._single_shot = QTimer(self)
        self._single_shot.setSingleShot(True)
        self._single_shot.setInterval(400)  # <500ms 双击窗口
        self._single_shot.timeout.connect(self.patRequested.emit)
        self.setAcceptDrops(True)   # v0.9 拖放文件给它打开

        # v0.3 帧动画：150ms/帧，播完回当前静帧
        self._static_sprite: SpriteRef = sprite
        self._frames: list = []
        self._frame_idx = 0
        self._frame_loop = False
        self._frame_timer = QTimer(self)
        self._frame_timer.setInterval(150)
        self._frame_timer.timeout.connect(self._advance_frame)

    # ---- 渲染 ----
    def set_sprite(self, sprite: SpriteRef) -> None:
        """v0.10：path 为文件路径（os.path.exists）→ QPixmap 图片渲染；
        否则 emoji 文本（降级路径，v0.1 起行为不变）。v0.10.15 加 pix 缓存：
        1024 帧图每 tick 重载代价高。H3 修（v0.10.18）：缓存**显示档缩放后**
        的小图（旧版存 1024×1536 全分辨率镜像图，64 条≈400MiB；且每次换帧
        都从全图 SmoothTransformation 缩放，CPU 持续消耗）——显示档单条
        <0.6MiB，键含尺寸防不同档混用。"""
        import os

        self._sprite = sprite
        if os.path.isfile(sprite.path):
            from PySide6.QtGui import QPixmap

            # v0.10.18：key 含 mtime——帧文件被热替换时缓存自动失效
            key = (sprite.path, self._facing, sprite.width, sprite.height,
                   os.path.getmtime(sprite.path))
            pm = self._pix_cache.get(key)
            if pm is not None:
                self._pix_cache.move_to_end(key)   # 真 LRU：命中刷新热度
            else:
                raw = QPixmap(sprite.path)
                if not raw.isNull():
                    if self._facing < 0:
                        from PySide6.QtGui import QTransform

                        raw = raw.transformed(
                            QTransform().scale(-1, 1))
                    pm = raw.scaled(
                        sprite.width, sprite.height,
                        Qt.KeepAspectRatio, Qt.SmoothTransformation,
                    )
                    self._pix_cache[key] = pm
                    if len(self._pix_cache) > 64:
                        self._pix_cache.popitem(last=False)
                else:
                    # 加载失败也入缓存（null 哨兵）——防动画每帧 interval
                    # 重复 isfile+解码的主线程 IO；label 保持上一帧画面
                    self._pix_cache[key] = QPixmap()
            if pm is not None and not pm.isNull():
                self._label.setPixmap(pm)
        else:
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

    def set_facing(self, d: int) -> None:
        """v0.10.16 移动朝向：d=-1 左、1 右（0/未知忽略）；变化即镜像重绘。
        （帧素材统一面朝右，向左移动时水平翻转显示，行走方向与朝向一致）"""
        import os

        if d not in (-1, 1) or d == getattr(self, "_facing", 1):
            return
        self._facing = d
        if self._provider is not None or os.path.isfile(self._sprite.path):
            self.set_sprite(self._sprite)

    def play_frames(self, frames: list, loop: bool = False,
                    interval_ms: int = 150) -> None:
        """v0.3 播一段帧序列（AssetProvider.get_frames），播完回静帧。

        v0.10.15：loop=True 循环播放（咀嚼/行走等），stop_frames() 或
        set_sprite 打断；interval_ms 帧间隔。
        """
        if not frames:
            return
        self._static_sprite = self._sprite  # 播完/打断恢复
        self._frames = list(frames)
        self._frame_idx = 0
        self._frame_loop = bool(loop)
        self._frame_timer.setInterval(interval_ms)
        self.set_sprite(self._frames[0])
        if len(self._frames) > 1:
            self._frame_timer.start()

    def stop_frames(self) -> None:
        """打断帧序列，恢复进入播放前的静帧。"""
        if self._frames:
            self._frames = []
            self._frame_timer.stop()
            self.set_sprite(getattr(self, "_static_sprite", self._sprite))

    def _advance_frame(self) -> None:
        self._frame_idx += 1
        if self._frame_idx >= len(self._frames):
            if getattr(self, "_frame_loop", False):
                self._frame_idx = 0
            else:
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
            # M10 修：横向用 availableGeometry（排除任务栏，与 FSM _clamp_x
            # 语义一致——旧版用 geometry 差一个任务栏宽度，任务栏停靠
            # 左/右时宠物到不了工作区边缘）。纵向保留整屏范围。
            avail = [s.availableGeometry() for s in screens]
            full = [s.geometry() for s in screens]
            min_x = min(g.x() for g in avail)
            max_x = max(g.x() + g.width() for g in avail)
            min_y = min(g.y() for g in full)
            max_y = max(g.y() + g.height() for g in full)
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

    # ---- v0.9 拖放文件（快捷启动器，§v0.9 win/mac 同手势） ----
    def dragEnterEvent(self, event):
        """接受本地文件/文件夹拖入。"""
        from PySide6.QtCore import QUrl

        urls = event.mimeData().urls() if event.mimeData() else []
        if urls and all(u.isLocalFile() for u in urls):
            event.acceptProposedAction()

    def dragMoveEvent(self, event):
        event.acceptProposedAction()

    def dropEvent(self, event):
        """取第一个本地路径 → fileDropped 信号（app 接平台 open）。"""
        from PySide6.QtCore import QUrl

        urls = event.mimeData().urls() if event.mimeData() else []
        if urls:
            self.fileDropped.emit(urls[0].toLocalFile())
            event.acceptProposedAction()

    def contextMenuEvent(self, event):
        # 右键菜单：互动 + 三种互斥移动模式 + 设置/退出
        menu = QMenu(self)
        menu.addAction("喂食", self.feedRequested.emit)
        menu.addAction("洗澡", self.cleanRequested.emit)
        menu.addAction("戳一戳", self.pokeRequested.emit)
        modes = menu.addMenu("移动状态")
        group = QActionGroup(modes)
        group.setExclusive(True)
        for key, label in (
            ("follow", "跟随鼠标"),
            ("free", "自由动（默认）"),
            ("edge", "边缘吸附静止"),
        ):
            action = modes.addAction(label)
            action.setCheckable(True)
            action.setChecked(key == self._motion_mode)
            group.addAction(action)
            action.triggered.connect(
                lambda checked=False, mode=key: self.motionModeRequested.emit(mode)
            )
        menu.addSeparator()
        menu.addAction("设置", self.settingsRequested.emit)
        menu.addAction("退出", self.quitRequested.emit)
        # QContextMenuEvent 非 QSinglePointEvent 子类，无 globalPosition()
        # （v0.2.2 勘误回归修复：该调用 AttributeError 被 Qt 吞→菜单不弹）
        menu.exec(event.globalPos())

    def set_motion_mode(self, mode: str) -> None:
        """同步菜单选中状态；FSM 仍是移动行为的唯一状态源。"""
        if mode in {"follow", "free", "edge"}:
            self._motion_mode = mode
