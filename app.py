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
        # v0.3 拖拽（拖动直接挪窗保跟手，FSM 记录位置/速度）+ 跟随开关
        self.window.dragStarted.connect(self._on_drag_started)
        self.window.dragMoved.connect(self._on_drag_moved)
        self.window.dragReleased.connect(self._on_drag_released)
        self.window.followToggleRequested.connect(
            lambda: self._fsm_event("follow_toggle")
        )
        # v0.3 气泡跟随宠物（§2.4 头顶 20px / 靠顶翻下）
        self.window.petMoved.connect(self._on_pet_moved)

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

        # 气泡骨架自检（证明 BubbleWidget.show(text) 能显示文字，挂宠物头顶）
        QTimer.singleShot(
            1500,
            lambda: self.bubble.show("我醒啦～", anchor=self._pet_anchor()),
        )

        # 传感器慢刷新（2s），FSM 快 tick（50ms），衰减 1s（wall-clock delta），
        # 全屏检测 1s（独立于传感器缓存，缩短可拖/可见窗口期）
        self._sensor_timer = QTimer(self.app)
        self._sensor_timer.timeout.connect(self._refresh_sensors)
        self._sensor_timer.start(2000)

        self._fullscreen_timer = QTimer(self.app)
        self._fullscreen_timer.timeout.connect(self._check_fullscreen)
        self._fullscreen_timer.start(1000)

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

    def _pet_anchor(self) -> tuple:
        """气泡锚点：宠物当前 bottom_center + 窗口高。"""
        x, y = self.fsm.pos
        return (x, y, self.window.height())

    def _on_pet_moved(self, x: float, y: float, h: int) -> None:
        self.bubble.follow((x, y, h))

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
            self.bubble.show(msg, anchor=self._pet_anchor())

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

    def _check_fullscreen(self) -> None:
        """v0.3 全屏/演示检测（1s 轮询，双次确认去抖）：
        前台全屏 → 隐藏 + 暂停/收敛 FSM；退出单次确认即恢复。"""
        try:
            fs = self.adapter.is_fullscreen_active()
        except NotImplementedError:
            fs = False  # 平台未实现（mac 待补）不抑制
        if fs:
            self._fs_hits = getattr(self, "_fs_hits", 0) + 1
        else:
            self._fs_hits = 0
        was = getattr(self, "_fullscreen", False)
        # 隐藏需连续 2 次命中（防前台切换瞬间的假全屏闪烁）；
        # 恢复单次否决即触发（宁可快恢复可见）
        if not was and fs and self._fs_hits >= 2:
            self._fullscreen = True
            self.fsm.handle_event("fullscreen_on")
            self.window.hide()
            self.logger.info("全屏检测：隐藏宠物")
        elif was and not fs:
            self._fullscreen = False
            self.fsm.handle_event("fullscreen_off")
            self.window.show()
            self.logger.info("全屏检测：恢复显示")

    # ---- v0.3 拖拽 ----
    def _on_drag_started(self, x: float, y: float) -> None:
        self.fsm.begin_drag((x, y))
        self.window.move_bottom_center(x, y)

    def _on_drag_moved(self, x: float, y: float) -> None:
        self.fsm.drag_move((x, y))
        self.window.move_bottom_center(x, y)  # 直接挪窗保跟手

    def _on_drag_released(self, x: float, y: float) -> None:
        self.fsm.end_drag()

    def _fsm_event(self, event: str) -> None:
        self.fsm.handle_event(event)

    def _tick(self) -> None:
        # 可见性看门狗：非全屏被隐藏（异常/竞态）→ 立即恢复并留痕
        if not getattr(self, "_fullscreen", False) and not self.window.isVisible():
            self.window.show()
            self.logger.warning("宠物窗口异常隐藏，看门狗已恢复")
        action = self.fsm.step(self.store.get(), self.sensors, 0.05)
        if action.type in (ActionType.MOVE_TO, ActionType.FALL):
            # FALL 同样驱动窗口位移（窗口顶面走出/松手直落的掉落过程）
            x, y = action.params["pos"]
            self.window.move_bottom_center(x, y)
        elif action.type == ActionType.ANIMATE and action.params.get("name"):
            self._play_animate(action.params["name"])

    # ---- v0.3 动画 ----
    def _play_animate(self, name: str) -> None:
        """随机小动作：get_frames 3 帧循环一轮（emoji 占位，~450ms）后回静帧。"""
        state = self.store.get()
        frames = self.provider.get_frames(state, ActionType.ANIMATE)
        self.window.play_frames(frames)

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
