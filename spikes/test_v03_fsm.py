"""v0.3 FSM 自动验证 —— WANDER / 拖拽抛物 / 边界 / 跟随 / 随机动作 / 全屏抑制。

纯逻辑层测试（无 Qt 事件），dt=0.05 快进模拟。
运行：python spikes/test_v03_fsm.py
"""

from __future__ import annotations

import sys

sys.path.insert(0, ".")

from pet.behavior import ActionType, BehaviorFSM, Sensors  # noqa: E402
from pet.pet_state import PetState  # noqa: E402

WA = {"x": 0, "y": 0, "width": 1920, "height": 1080}
DT = 0.05
PASS, FAIL = [], []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("  ✅ " if cond else "  ❌ ") + name)


def sensors(mouse=(960, 540), windows=()):
    return Sensors(mouse_pos=mouse, work_area=dict(WA), windows=list(windows))


def run(fsm, seconds, windows=(), mouse=(960, 540)):
    """快进 seconds 秒，返回 (actions, 最后 pos)。"""
    out = []
    for _ in range(int(seconds / DT)):
        out.append(fsm.step(PetState.default(), sensors(mouse, windows), DT))
    return out, fsm.pos


def main() -> int:
    # T1 WANDER：模拟 300s，≥3 个不同落点，全在区内，y 贴地板
    fsm = BehaviorFSM(dict(WA))
    actions, pos = run(fsm, 300)
    xs = sorted({round(a.params["pos"][0]) for a in actions
                 if a.type == ActionType.MOVE_TO})
    check("T1 WANDER 多落点(≥3 不同 x)", len(xs) >= 3)
    check("T1 不出工作区横向范围",
          all(WA["x"] <= a.params["pos"][0] <= WA["x"] + WA["width"]
              for a in actions if a.type == ActionType.MOVE_TO))
    landed_floor = fsm.pos[1] == WA["y"] + WA["height"]
    check("T1 无窗时贴地板站立", landed_floor)

    # T2 拖拽：begin → move → step 返回 MOVE_TO 光标处
    fsm = BehaviorFSM(dict(WA))
    fsm.begin_drag((300, 500))
    fsm.drag_move((450, 600))
    a = fsm.step(PetState.default(), sensors(), DT)
    check("T2 拖拽中 step 返回光标位置",
          a.type == ActionType.MOVE_TO and a.params["pos"] == (450, 600))

    # T3 抛掷：右抛 vx=800 vy=-600 → 抛物线 → 落地停止不穿屏
    fsm = BehaviorFSM(dict(WA))
    fsm.begin_drag((400, 900))
    fsm.drag_move((400, 900))          # 静止拎起
    fsm.drag_move((400 + 96, 900 - 72))  # 120ms 内 96px/72px → v=(800,-600)
    fsm.end_drag()
    ys = [fsm.pos[1]]
    for _ in range(int(3 / DT)):
        fsm.step(PetState.default(), sensors(), DT)
        ys.append(fsm.pos[1])
    floor = WA["y"] + WA["height"]
    check("T3 抛物线有上升段(min(y) < 起抛 y)", min(ys) < 900)
    check("T3 落地停在地板(y==floor)", fsm.pos[1] == floor)
    check("T3 不穿底(y 不超 floor)", all(y <= floor for y in ys))

    # T4 边界：极限初速度也不飞出横向
    for vx_sign in (1, -1):
        fsm = BehaviorFSM(dict(WA))
        fsm.begin_drag((960, 400))
        fsm.drag_move((960, 400))
        fsm.drag_move((960 + vx_sign * 300, 100))  # 巨大速度 → 钳制
        fsm.end_drag()
        run(fsm, 5)
        check(
            f"T4 {'右' if vx_sign > 0 else '左'}甩极限速度不出区",
            WA["x"] <= fsm.pos[0] <= WA["x"] + WA["width"],
        )

    # T5 跟随鼠标：光标固定远处，宠物抵达光标下方表面
    fsm = BehaviorFSM(dict(WA))
    fsm.handle_event("follow_toggle")
    _, pos = run(fsm, 30, mouse=(1700, 300))
    check("T5 跟随光标抵达(距光标 x ≤10)", abs(1700 - pos[0]) <= 10)
    check("T5 跟随站立在地板", pos[1] == WA["y"] + WA["height"])

    # T6 随机小动作：模拟 120s 至少一次具名 ANIMATE
    fsm = BehaviorFSM(dict(WA))
    actions, _ = run(fsm, 120)
    named = [a for a in actions
             if a.type == ActionType.ANIMATE and a.params.get("name")]
    check("T6 随机小动作出现(120s ≥1 次)", len(named) >= 1)

    # T7 窗口顶面行走 + 跨表面掉落：窗口 x∈[800,1200] y=700
    win = {"x": 800, "y": 700, "width": 400, "height": 300}
    fsm = BehaviorFSM(dict(WA))
    # 直接放在窗顶左端，目标在窗外(左侧地板) → 应走出边缘后 FALL 到地板
    fsm._pos = (850.0, 700.0)   # 测试注入初始位（窗顶）
    fsm._mode = "walk"
    fsm._target = (400.0, 700.0)
    actions, pos = run(fsm, 6, windows=(win,))
    floor = WA["y"] + WA["height"]
    check("T7 跨表面掉落到地板", pos[1] == floor and pos[0] < 800)

    # T8 窗壁：地板向窗走不攀爬（v0.3 留后），停在墙前
    fsm = BehaviorFSM(dict(WA))
    fsm._pos = (700.0, floor)
    fsm._mode = "walk"
    fsm._target = (1000.0, floor)
    _, pos = run(fsm, 6, windows=(win,))
    check("T8 地板→窗壁不攀爬(停在 x<800)", pos[0] < 800 and pos[1] == floor)

    # T9 全屏抑制：fullscreen_on 后 idle 到期不出新 WANDER
    fsm = BehaviorFSM(dict(WA))
    fsm.handle_event("fullscreen_on")
    actions, _ = run(fsm, 60)
    moved = [a for a in actions if a.type == ActionType.MOVE_TO]
    check("T9 全屏时不出新位移(抑制 WANDER)", len(moved) == 0)

    # T11 最大化/上贴屏窗口不构成表面（拖到其上松手不落到屏幕外）
    def _drop_and_check_landing(windows, name):
        fsm = BehaviorFSM(dict(WA))
        fsm.begin_drag((960, 300))
        fsm.drag_move((960, 300))
        fsm.drag_move((960, 400))
        fsm.end_drag()
        floor = WA["y"] + WA["height"]
        # 快进到落地（IDLE）即停，避开落地后的随机游走
        for _ in range(int(3 / DT)):
            fsm.step(PetState.default(), sensors(windows=windows), DT)
            if fsm.mode == "idle":
                break
        check(name, fsm.pos[1] == floor and 0 <= fsm.pos[0] <= 1920)

    # 最大化窗口 Win32 矩形：x=-8 y=-8 覆盖全屏（隐形 resize 边框）
    _drop_and_check_landing(
        ({"x": -8, "y": -8, "width": 1936, "height": 1096},),
        "T11 最大化窗口(负边框)上方松手落回地板不出屏",
    )
    # 上贴屏窗口（top==0）：同规则不算面
    _drop_and_check_landing(
        ({"x": 0, "y": 0, "width": 1920, "height": 540},),
        "T11 上贴屏窗口(top=0)不算站立面",
    )

    # T10 get_frames：MOVE_TO 2 帧 / ANIMATE 3 帧
    from pet.asset_provider import EmojiProvider
    p = EmojiProvider()
    f_walk = p.get_frames(PetState.default(), ActionType.MOVE_TO)
    f_act = p.get_frames(PetState.default(), ActionType.ANIMATE)
    check("T10 get_frames 行走 2 帧", len(f_walk) == 2)
    check("T10 get_frames 动作 3 帧(首尾静帧)", len(f_act) == 3
          and f_act[0].path == f_act[2].path != f_act[1].path)

    print(f"\n结果：{len(PASS)} 通过 / {len(FAIL)} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
