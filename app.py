"""桌宠入口 —— 设计思路.md §2.5（单实例锁 + shutdown 七步序）。

**平台库-free**：本文件不 import fcntl/pyobjc/sensor_mac/window_mac，所有平台
特定（单实例锁 / dock 隐藏 / 传感器 / 浮窗创建）经 ``platform.py`` 注入。

v0.2：接 ``PetStateStore``（load 启动 / save debounce+定时+shutdown）+ 1s
衰减 QTimer + 点击交互（window signal ``patRequested``/``feedRequested``/...
→ ``store.update`` + 气泡）+ ``on_change`` 订阅 window（切 emoji）/ behavior
（调制）/ save 各一次。**v0.2 共享交互入口取 win 端 signal 版**，store 接线
（mac 主笔 PetStateStore）嫁接其上（``_interact`` 用 ``store.update`` 而非
静态 ``dataclasses.replace``）。
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from pet.asset_provider import EmojiProvider
from pet.behavior import ActionType, BehaviorFSM
from pet.bubble import BubbleWidget
from pet.config import load_config
from pet.logging_setup import setup_logging
from pet.pet_state import PetStateStore
from pet.platform import get_platform_adapter
from pet.tray import TrayManager

_SAVE_DEBOUNCE_MS = 500       # 变更后 500ms 内多次只存一次
_SAVE_PERIODIC_MS = 30_000    # 定时存档
_DECAY_INTERVAL_MS = 1000     # 衰减 1s 一次（wall-clock delta）

# 交互：kind → (数值字段, 气泡文案)
_INTERACT_FIELD = {
    "pet": "mood",
    "feed": "fullness",
    "clean": "cleanliness",
    "poke": "mood",
}
_INTERACT_MSG = {
    "pet": "摸摸头～",
    "feed": "吃饱啦！",
    "clean": "洗得香香的～",
    "poke": "别戳啦…",
}


class PetApp:
    def __init__(self, argv, adapter, verbose: bool):
        self.adapter = adapter
        self.logger = logging.getLogger("pet")

        self.app = QApplication.instance() or QApplication(argv)
        self.app.setQuitOnLastWindowClosed(False)
        adapter.hide_dock_icon()  # mac 特定 / win no-op

        paths = adapter.get_paths()
        self.cfg = load_config(paths["config_path"])
        self._state_path = os.path.join(paths["data_dir"], "pet_state.json")

        # 养成 store：启动 load（无存档→default）；重启数值一致靠此
        self.store = PetStateStore.load(self._state_path)
        self.provider = EmojiProvider()
        self._gains = dict(self.cfg.get("interaction_gain", {}))

        self.sensors = adapter.get_sensors()  # 注入式，不直 import sensor_mac
        wa = self.sensors.work_area
        self.fsm = BehaviorFSM(dict(wa), self.cfg.get("behavior", {}))

        self.window = adapter.create_pet_window(  # 注入式，不直 import window_mac
            self.provider.get_static(self.store.get())
        )
        self.window.set_sprite_provider(self.provider)

        cx = wa.get("x", 0) + wa.get("width", 0) / 2
        bottom = wa.get("y", 0) + wa.get("height", 0)
        self.window.move_bottom_center(cx, bottom)
        self.window.show()

        # v0.2 交互入口（win signal 版，§2.3 手势消解在共享 WindowBase）
        self.window.patRequested.connect(lambda: self._interact("pet"))
        self.window.feedRequested.connect(lambda: self._interact("feed"))
        self.window.cleanRequested.connect(lambda: self._interact("clean"))
        self.window.pokeRequested.connect(lambda: self._interact("poke"))
        self.window.quitRequested.connect(self.shutdown)

        self.bubble = BubbleWidget()
        self.tray = TrayManager(on_quit=self.shutdown, parent=self.app)

        # save：debounce（变更后 500ms）+ 定时 30s + shutdown
        self._save_timer = QTimer(self.app)
        self._save_timer.setSingleShot(True)
        self._save_timer.timeout.connect(self._save_now)
        self._periodic_save_timer = QTimer(self.app)
        self._periodic_save_timer.timeout.connect(self._save_now)
        self._periodic_save_timer.start(_SAVE_PERIODIC_MS)

        # on_change 订阅：window（切 emoji）/ behavior（数值调制）/ save（debounce）
        self.store.on_change(self.window.on_state_change)
        self.store.on_change(self.fsm.on_state_change)
        self.store.on_change(self._on_state_changed_persist)
        # 用当前 state 调制一次（启动即对齐数值，不等首次衰减）
        self.fsm.on_state_change(self.store.get())

        # 气泡骨架自检（证明 BubbleWidget.show(text) 能显示文字）
        QTimer.singleShot(1500, lambda: self.bubble.show("我醒啦～"))

        # 传感器慢刷新（2s），FSM 快 tick（50ms），衰减 1s（wall-clock delta）
        self._sensor_timer = QTimer(self.app)
        self._sensor_timer.timeout.connect(self._refresh_sensors)
        self._sensor_timer.start(2000)

        self._tick_timer = QTimer(self.app)
        self._tick_timer.timeout.connect(self._tick)
        self._tick_timer.start(50)

        self._decay_timer = QTimer(self.app)
        self._decay_timer.timeout.connect(self._apply_decay)
        self._decay_timer.start(_DECAY_INTERVAL_MS)
        # 启动即补一次离线期间的衰减（load 读回 last_update → now 的 wall-clock）
        self._apply_decay()

        # 让 Python 能响应 SIGINT（开发期 Ctrl-C 干净退出）
        self._sig_timer = QTimer(self.app)
        self._sig_timer.timeout.connect(lambda: None)
        self._sig_timer.start(200)
        signal.signal(signal.SIGINT, lambda *_: self.shutdown())

    def _interact(self, kind: str) -> None:
        """v0.2 养成交互：window signal 触发 → store.update + 气泡。

        emoji 切换由 on_change 订阅（window.on_state_change）自动处理，不需
        主动 _refresh_sprite。衰减导致的 mood 变化也经 on_change 切 emoji。
        """
        field = _INTERACT_FIELD.get(kind)
        if field is None:
            return
        delta = float(self._gains.get(kind, 0))
        self.store.update(**{field: delta})
        msg = _INTERACT_MSG.get(kind)
        if msg:
            self.bubble.show(msg)

    # ---- 衰减 / 持久化 ----
    def _apply_decay(self) -> None:
        self.store.apply_decay(self.cfg.get("decay_per_hour", {}))

    def _on_state_changed_persist(self, _state) -> None:
        # debounce：500ms 内多次变更只存一次
        if not self._save_timer.isActive():
            self._save_timer.start(_SAVE_DEBOUNCE_MS)

    def _save_now(self) -> None:
        try:
            self.store.save(self._state_path)
        except Exception:
            self.logger.exception("存档失败")

    def _refresh_sensors(self) -> None:
        self.sensors = self.adapter.get_sensors()

    def _tick(self) -> None:
        action = self.fsm.step(self.store.get(), self.sensors, 0.05)
        if action.type == ActionType.MOVE_TO:
            x, y = action.params["pos"]
            self.window.move_bottom_center(x, y)

    def shutdown(self) -> None:
        """七步序（§2.5）；v0.2 起 ④保存 PetState 有实体。"""
        # ① ProactiveScheduler  ② EatMouseSession  ③ 全局热键 —— v0.x 均 pass
        # ④ 保存 PetState+Memory
        self._save_now()
        # ⑤ 关 QML engine —— v0.x pass 占位
        # ⑥ 移除托盘
        self.tray.remove()
        # ⑦ QApplication.quit()
        self.app.quit()

    def run(self) -> int:
        return self.app.exec()


def main() -> int:
    parser = argparse.ArgumentParser(description="桌宠 v0.2.0")
    parser.add_argument(
        "--verbose", action="store_true", help="详细日志到 stderr"
    )
    args = parser.parse_args()

    adapter = get_platform_adapter()
    paths = adapter.get_paths()
    logger = setup_logging(args.verbose, paths["log_dir"])
    logger.info("启动桌宠 v0.2.0（verbose=%s）", args.verbose)

    if not adapter.acquire_single_instance_lock():
        logger.info("已有实例运行，本进程退出。")
        return 0

    pet = PetApp(sys.argv, adapter, verbose=args.verbose)
    return pet.run()


if __name__ == "__main__":
    raise SystemExit(main())
