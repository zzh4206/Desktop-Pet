"""系统托盘 —— 设计思路.md §2.5（退出走 shutdown）/ 平台适配与分工.md §二。

v0.1：``QSystemTrayIcon`` + “退出”菜单。退出回调走 app 层 ``shutdown()``。
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt, QObject
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QMenu, QSystemTrayIcon


class TrayManager(QObject):
    def __init__(self, on_quit, parent=None):
        super().__init__(parent)
        self._on_quit = on_quit
        self._on_chat = None
        self._on_reset = None
        self._on_spit = None
        self._on_mem = None
        self._on_chat_emotion = None
        self._autostart_state = False
        self._on_autostart = None  # v0.11 自启切换回调（L14 修：注释原误标"强制吐出"）
        self._tray = QSystemTrayIcon(self._make_icon(), parent)
        self._tray.setToolTip("桌宠")

        menu = QMenu()
        act_chat = menu.addAction("聊天")
        act_chat.triggered.connect(self._emit_chat)
        act_reset = menu.addAction("重新开始")
        act_mem = menu.addAction("记忆管理")
        act_emotion = menu.addAction("聊天情绪设置")
        self._act_auto = act_auto = menu.addAction("开机自启")
        act_auto.setCheckable(True)
        act_auto.setChecked(self._autostart_state)
        act_auto.toggled.connect(self._emit_autostart)
        act_reset.triggered.connect(self._emit_reset)
        act_mem.triggered.connect(self._emit_mem)
        act_emotion.triggered.connect(self._emit_chat_emotion)
        # v0.7 强制吐出：吃鼠标期间鼠标被抑制点不到菜单，故此菜单主要服务于
        # 非锁定态的残留释放 + 键盘可达用户（Tab/方向键导航菜单）。主逃生口
        # 仍是热键 Cmd+Option+T + 看门狗。
        act_spit = menu.addAction("强制吐出")
        act_spit.triggered.connect(self._emit_spit)
        menu.addSeparator()
        act_quit = menu.addAction("退出")
        act_quit.triggered.connect(self._on_quit)
        self._tray.setContextMenu(menu)
        self._menu = menu  # 保留引用，remove() 时 deleteLater 释放
        self._tray.show()

    def _emit_mem(self) -> None:
        """v0.9 记忆管理菜单。"""
        if self._on_mem:
            self._on_mem()

    def set_autostart_state(self, enabled: bool) -> None:
        """v0.11：同步自启菜单勾选态（app 启动时调）。

        M9 修：旧版只写标志不动 QAction——已启用时重启 app 托盘仍显示
        未勾选。blockSignals 防 setChecked 触发 toggled 回环调 setter。
        """
        self._autostart_state = enabled
        act = getattr(self, "_act_auto", None)
        if act is not None:
            act.blockSignals(True)
            act.setChecked(enabled)
            act.blockSignals(False)

    def set_autostart_callback(self, cb) -> None:
        self._on_autostart = cb

    def _emit_autostart(self, checked: bool) -> None:
        if self._on_autostart:
            self._on_autostart(checked)

    def set_mem_callback(self, cb) -> None:
        self._on_mem = cb

    def set_chat_emotion_callback(self, cb) -> None:
        self._on_chat_emotion = cb

    def _emit_chat_emotion(self) -> None:
        if self._on_chat_emotion:
            self._on_chat_emotion()

    def set_chat_callback(self, cb) -> None:
        """v0.4：托盘'聊天'唤出聊天面板（v0.11 真全局热键占位）。"""
        self._on_chat = cb

    def set_reset_callback(self, cb) -> None:
        """v0.5：托盘'重新开始'→app 经 platform.confirm_dangerous 二次确认后清档复位。"""
        self._on_reset = cb

    def set_spit_callback(self, cb) -> None:
        """v0.7：托盘'强制吐出'→EatMouseSession.force_spit（停 CGEventTap + 回 idle）。"""
        self._on_spit = cb

    def _emit_chat(self) -> None:
        if self._on_chat is not None:
            self._on_chat()
        else:
            # 未设 chat callback（正常应在 _build_chat_panel 里注册 fallback）；
            # tray 无 bubble 引用，记日志便于排查托盘聊天点击无反应
            logging.getLogger("pet").warning(
                "托盘'聊天'点击但未注册 callback（聊天面板未初始化？）"
            )

    def _emit_reset(self) -> None:
        if self._on_reset is not None:
            self._on_reset()

    def _emit_spit(self) -> None:
        if self._on_spit is not None:
            self._on_spit()

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
        # hide + deleteLater 释放托盘与菜单（旧版只 hide，QSystemTrayIcon 留到
        # app.quit 才回收；显式 deleteLater 让退出更干净，防二次 shutdown 重入）
        self._tray.hide()
        self._tray.deleteLater()
        if getattr(self, "_menu", None) is not None:
            self._menu.deleteLater()
