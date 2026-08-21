"""v0.2 交互入口自动验证（win 端）—— 工作表 v0.2 Must 对应项。

模拟：单击→摸头(+心情)、双击→喂食(+饱食)、poke→-心情、HUNGRY emoji 切换。
拖拽候选（位移≥5px）不触发单击也一并验证。QtTest 驱动，无需人工点击。

运行：python spikes/test_v02_interaction.py
"""

from __future__ import annotations

import dataclasses
import sys

from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

sys.path.insert(0, ".")

from pet.asset_provider import EmojiProvider  # noqa: E402
from pet.pet_state import PetState  # noqa: E402

GAINS = {"pet": 5, "feed": 20, "clean": 15, "poke": -8}
PASS, FAIL = [], []


def check(name: str, cond: bool) -> None:
    (PASS if cond else FAIL).append(name)
    print(("  ✅ " if cond else "  ❌ ") + name)


def main() -> int:
    app = QApplication(sys.argv)
    from pet.window import WindowBase

    state = PetState.default()
    provider = EmojiProvider()
    win = WindowBase(provider.get_static(state))
    win.show()
    QTest.qWaitForWindowExposed(win)

    events = []
    win.patRequested.connect(lambda: events.append("pat"))
    win.feedRequested.connect(lambda: events.append("feed"))
    win.cleanRequested.connect(lambda: events.append("clean"))
    win.pokeRequested.connect(lambda: events.append("poke"))

    # T-a 拖拽候选：按住移 ≥5px 再松 → 不触发单击
    QTest.mousePress(win, Qt.LeftButton, pos=win.rect().center())
    QTest.mouseMove(win, pos=win.rect().center() + QPoint(30, 0))
    QTest.mouseRelease(win, Qt.LeftButton, pos=win.rect().center() + QPoint(30, 0))
    QTest.qWait(500)
    check("T-a 位移≥5px 不触发单击(拖拽候选)", "pat" not in events)

    # T-b 单击 → pat（延迟消歧后触发）
    QTest.mouseClick(win, Qt.LeftButton, pos=win.rect().center())
    QTest.qWait(500)
    check("T-b 单击触发摸头", events.count("pat") == 1)

    # T-c 双击 → feed（吞掉单击）
    n_pat = events.count("pat")
    QTest.mouseDClick(win, Qt.LeftButton, pos=win.rect().center())
    QTest.qWait(500)
    check("T-c 双击触发喂食", events.count("feed") == 1)
    check("T-c 双击不触发单击", events.count("pat") == n_pat)

    # T-c2 右键菜单弹出回归（v0.2.2 勘误：Qt6 globalPos() 无 toPoint，
    # exec 抛 AttributeError 被 Qt 吞 → 菜单不弹。此断言锁 menu.exec 路径）
    from PySide6.QtCore import QTimer
    from PySide6.QtGui import QContextMenuEvent
    from PySide6.QtCore import QPoint as _QPoint  # QPoint 在 QtCore
    from PySide6.QtWidgets import QMenu

    def _close_popup():
        popup = QApplication.activePopupWidget()
        if isinstance(popup, QMenu):
            menu_actions.append([a.text() for a in popup.actions()])
            popup.close()

    menu_actions: list = []
    QTimer.singleShot(300, _close_popup)
    ev = QContextMenuEvent(
        QContextMenuEvent.Reason.Mouse, QPoint(10, 10), QPoint(100, 100)
    )
    win.contextMenuEvent(ev)  # 走 menu.exec 模态循环，由 timer 关闭
    check("T-c2 右键菜单能弹出(v0.2.2 回归)", len(menu_actions) == 1)
    check(
        "T-c2 菜单六项(喂食/洗澡/戳一戳/设置/退出)",
        len(menu_actions) == 1
        and [t for t in menu_actions[0] if t]
        == ["喂食", "洗澡", "戳一戳", "跟随鼠标", "设置", "退出"],
    )

    # T-c3 气泡跟随宠物定位（§2.4：头顶20px/靠顶翻下/无锚点回退顶中）
    from pet.bubble import BubbleWidget

    bub = BubbleWidget()
    bub.show("锚定", anchor=(500, 1080, 96))
    check("T-c3 头顶锚定(中心随宠物x, y在头上)",
          abs(bub.x() + bub.width() / 2 - 500) <= 1 and 900 <= bub.y() <= 985)
    bub.follow((300, 300, 96))
    check("T-c3 移动实时跟随", abs(bub.x() + bub.width() / 2 - 300) <= 1
          and bub.y() < 300)
    bub_top = BubbleWidget()
    bub_top.show("贴顶", anchor=(500, 60, 96))
    check("T-c3 靠屏顶翻到宠物下方", bub_top.y() >= 60)
    bub_no = BubbleWidget()
    bub_no.show("无锚点")
    check("T-c3 无锚点回退屏顶居中", bub_no.y() <= 20)

    # T-d 数值增益（模拟 app._interact 的核心逻辑）
    deltas = {"pet": "mood", "feed": "fullness",
              "clean": "cleanliness", "poke": "mood"}
    for kind, field in deltas.items():
        old = state
        state = dataclasses.replace(
            old, **{field: min(100.0, max(0.0, getattr(old, field) + GAINS[kind]))}
        )
    check("T-d 四交互数值正确 (mood=80+5-8=77, fullness=100, clean=95)",
          (state.mood, round(state.fullness), round(state.cleanliness)) == (77, 100, 95))

    # T-e HUNGRY 视觉：fullness<20 → 🙀
    hungry = dataclasses.replace(state, fullness=10.0)
    check("T-e 饱食<20 出 HUNGRY emoji 🙀",
          provider.get_static(hungry).path == "🙀")
    check("T-e mood 正常路径 emoji 随 mood 变化",
          provider.get_static(dataclasses.replace(state, mood=10.0)).path == "😿")

    print(f"\n结果：{len(PASS)} 通过 / {len(FAIL)} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
