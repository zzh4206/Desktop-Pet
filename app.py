"""桌宠入口 —— 设计思路.md §2.5（单实例锁 + shutdown 七步序）。

**平台库-free**：本文件不 import fcntl/pyobjc/sensor_mac/window_mac，所有平台
特定（单实例锁 / dock 隐藏 / 传感器 / 浮窗创建）经 ``platform.py`` 注入。
v0.1：``--verbose`` + 单实例锁 + ``shutdown()`` 七步骨架（仅 ⑥移除托盘 /
⑦QApplication.quit 有实体，余 pass 占位）。
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from pet.asset_provider import EmojiProvider
from pet.behavior import ActionType, BehaviorFSM
from pet.bubble import BubbleWidget
from pet.config import load_config
from pet.logging_setup import setup_logging
from pet.pet_state import PetState
from pet.platform import get_platform_adapter
from pet.tray import TrayManager


class PetApp:
    def __init__(self, argv, adapter, verbose: bool):
        self.adapter = adapter
        self.logger = logging.getLogger("pet")

        self.app = QApplication.instance() or QApplication(argv)
        self.app.setQuitOnLastWindowClosed(False)
        adapter.hide_dock_icon()  # mac 特定 / win no-op

        paths = adapter.get_paths()
        self.cfg = load_config(paths["config_path"])
        self.state = PetState.default()
        self.provider = EmojiProvider()

        self.sensors = adapter.get_sensors()  # 注入式，不直 import sensor_mac
        wa = self.sensors.work_area
        self.fsm = BehaviorFSM(dict(wa), self.cfg.get("behavior", {}))

        self.window = adapter.create_pet_window(  # 注入式，不直 import window_mac
            self.provider.get_static(self.state)
        )
        cx = wa.get("x", 0) + wa.get("width", 0) / 2
        bottom = wa.get("y", 0) + wa.get("height", 0)
        self.window.move_bottom_center(cx, bottom)
        self.window.show()

        self.bubble = BubbleWidget()
        self.tray = TrayManager(on_quit=self.shutdown, parent=self.app)

        # 气泡骨架自检（证明 BubbleWidget.show(text) 能显示文字）
        QTimer.singleShot(1500, lambda: self.bubble.show("我醒啦～"))

        # 传感器慢刷新（2s），FSM 快 tick（50ms）
        self._sensor_timer = QTimer(self.app)
        self._sensor_timer.timeout.connect(self._refresh_sensors)
        self._sensor_timer.start(2000)

        self._tick_timer = QTimer(self.app)
        self._tick_timer.timeout.connect(self._tick)
        self._tick_timer.start(50)

        # 让 Python 能响应 SIGINT（开发期 Ctrl-C 干净退出）
        self._sig_timer = QTimer(self.app)
        self._sig_timer.timeout.connect(lambda: None)
        self._sig_timer.start(200)
        signal.signal(signal.SIGINT, lambda *_: self.shutdown())

    def _refresh_sensors(self) -> None:
        self.sensors = self.adapter.get_sensors()

    def _tick(self) -> None:
        action = self.fsm.step(self.state, self.sensors, 0.05)
        if action.type == ActionType.MOVE_TO:
            x, y = action.params["pos"]
            self.window.move_bottom_center(x, y)

    def shutdown(self) -> None:
        """七步序（§2.5）；v0.1 只有 ⑥⑦ 有实体，余 pass。"""
        # ① ProactiveScheduler  ② EatMouseSession  ③ 全局热键
        # ④ 保存 PetState+Memory  ⑤ 关 QML engine —— v0.1 均 pass 占位
        # ⑥ 移除托盘
        self.tray.remove()
        # ⑦ QApplication.quit()
        self.app.quit()

    def run(self) -> int:
        return self.app.exec()


def main() -> int:
    parser = argparse.ArgumentParser(description="桌宠 v0.1.0")
    parser.add_argument(
        "--verbose", action="store_true", help="详细日志到 stderr"
    )
    args = parser.parse_args()

    adapter = get_platform_adapter()
    paths = adapter.get_paths()
    logger = setup_logging(args.verbose, paths["log_dir"])
    logger.info("启动桌宠 v0.1.0（verbose=%s）", args.verbose)

    if not adapter.acquire_single_instance_lock():
        logger.info("已有实例运行，本进程退出。")
        return 0

    pet = PetApp(sys.argv, adapter, verbose=args.verbose)
    return pet.run()


if __name__ == "__main__":
    raise SystemExit(main())
