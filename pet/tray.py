"""系统托盘 —— 设计思路.md §2.5（退出走 shutdown）/ 平台适配与分工.md §二。

v0.1：``QSystemTrayIcon`` + “退出”菜单。退出回调走 app 层 ``shutdown()``。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QObject
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QMenu, QSystemTrayIcon


class TrayManager(QObject):
    def __init__(self, on_quit, parent=None):
        super().__init__(parent)
        self._on_quit = on_quit
        self._on_chat = None
        self._tray = QSystemTrayIcon(self._make_icon(), parent)
        self._tray.setToolTip("桌宠")

        menu = QMenu()
        act_chat = menu.addAction("聊天")
        act_chat.triggered.connect(self._emit_chat)
        menu.addSeparator()
        act_quit = menu.addAction("退出")
        act_quit.triggered.connect(self._on_quit)
        self._tray.setContextMenu(menu)
        self._tray.show()

    def set_chat_callback(self, cb) -> None:
        """v0.4：托盘'聊天'唤出聊天面板（v0.11 真全局热键占位）。"""
        self._on_chat = cb

    def _emit_chat(self) -> None:
        if self._on_chat is not None:
            self._on_chat()

    @staticmethod
    def _make_icon() -> QIcon:
        pix = QPixmap(32, 32)
        pix.fill(QColor(0, 0, 0, 0))
        p = QPainter(pix)
        p.setRenderHint(QPainter.Antialiasing)
        p.setBrush(QColor("#FFB400"))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(3, 3, 26, 26)
        p.setPen(QColor("white"))
        font = QFont()
        font.setPointSize(16)
        p.setFont(font)
        p.drawText(pix.rect(), Qt.AlignmentFlag.AlignCenter, "🐾")
        p.end()
        return QIcon(pix)

    def remove(self) -> None:
        self._tray.hide()
