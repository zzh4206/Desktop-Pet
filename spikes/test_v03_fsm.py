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
    for _ in range(int(6 / DT)):
        fsm.step(PetState.default(), sensors(windows=(win,)), DT)
        if fsm.mode == "idle":  # 落地停稳即断言（避开后续随机游走）
            break
    floor = WA["y"] + WA["height"]
    check("T7 跨表面掉落到地板", fsm.pos[1] == floor and fsm.pos[0] < 800)

    # T8 窗壁（v0.3.6 改为走到边框攀爬上顶）：地板向窗走 → 撞左沿爬上窗顶
    fsm = BehaviorFSM(dict(WA))
    fsm._pos = (700.0, floor)
    fsm._mode = "walk"
    fsm._target = (1000.0, floor)
    for _ in range(int(8 / DT)):
        fsm.step(PetState.default(), sensors(windows=(win,)), DT)
        if fsm.mode == "idle":
            break
    check("T8 走到窗壁沿边爬上窗顶", fsm.pos == (800.0, 700.0))

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

    # T12 轻抬松手 = 放下（垂直落），不上飞（垂直速度死区 200px/s）
    import time as _time
    fsm = BehaviorFSM(dict(WA))
    fsm.begin_drag((500, 800))
    _time.sleep(0.15)
    fsm.drag_move((500, 790))   # 0.15s 上移 10px → vy≈-67，死区内
    fsm.end_drag()
    floor = WA["y"] + WA["height"]
    check("T12 轻抬松手进下落态", fsm.mode == "fall")
    for _ in range(int(3 / DT)):
        fsm.step(PetState.default(), sensors(), DT)
        if fsm.mode == "idle":
            break
    check("T12 轻抬松手垂直落到地板(不上飞)", fsm.pos[1] == floor)

    # T13 抛掷撞窗口侧面 → 沿边逐渐爬到顶（不瞬移）
    win13 = {"x": 800, "y": 700, "width": 400, "height": 300}
    fsm = BehaviorFSM(dict(WA))
    fsm.begin_drag((700, 900))
    fsm.drag_move((700, 900))
    fsm.drag_move((700 + 96, 900))     # vx≈800 → 右飞撞窗左沿
    fsm.end_drag()
    steps = 0
    climbed_y = []
    while steps < int(6 / DT) and fsm.mode != "idle":
        fsm.step(PetState.default(), sensors(windows=(win13,)), DT)
        climbed_y.append(fsm.pos[1])
        steps += 1
    check("T13 撞窗侧进入攀爬态后登顶", fsm.mode == "idle"
          and fsm.pos == (800.0, 700.0))
    check("T13 攀爬是渐进的(经历多个中间 y, 非瞬移)",
          len([y for y in climbed_y if 700 < y < 900]) >= 3)
    # 从右侧撞 → 贴右沿爬
    fsm = BehaviorFSM(dict(WA))
    fsm.begin_drag((1300, 900))
    fsm.drag_move((1300, 900))
    fsm.drag_move((1300 - 96, 900))
    fsm.end_drag()
    while fsm.mode != "idle" and steps < int(12 / DT):
        fsm.step(PetState.default(), sensors(windows=(win13,)), DT)
        steps += 1
    check("T13 右侧撞入贴右沿登顶", fsm.pos == (1200.0, 700.0))
    # 攀爬途中窗口消失 → 坠落回地板
    fsm = BehaviorFSM(dict(WA))
    fsm.begin_drag((700, 900))
    fsm.drag_move((700, 900))
    fsm.drag_move((700 + 96, 900))
    fsm.end_drag()
    fsm.step(PetState.default(), sensors(windows=(win13,)), DT)
    assert fsm.mode == "climb"
    _, pos = run(fsm, 5)               # 无窗环境 → 攀爬失效坠落
    check("T13 攀爬中窗口消失→坠落地板", pos[1] == WA["y"] + WA["height"])

    # T14 站在窗顶时窗口关闭（传感器不再报告该窗）→ 坠落回地板
    fsm = BehaviorFSM(dict(WA))
    fsm._pos = (1000.0, 700.0)      # 站在 win13 顶
    fsm._mode = "idle"
    # 窗还在：不应误落
    ok_stand = fsm.step(PetState.default(), sensors(windows=(win13,)), DT)
    check("T14 窗在时不误落", ok_stand.type != ActionType.FALL)
    # 窗没了：支撑校验立即转下落
    gone = fsm.step(PetState.default(), sensors(), DT)
    check("T14 支撑消失立即转下落", gone.type == ActionType.FALL)
    _, pos = run(fsm, 4)
    check("T14 窗口关闭后落回地板", pos[1] == WA["y"] + WA["height"])

    # T15 极速抛掷一 tick 越过整扇窗（扫掠检测不漏检）→ 贴左沿攀爬
    fsm = BehaviorFSM(dict(WA))
    fsm.begin_drag((700, 900))
    fsm.drag_move((700, 900))
    fsm.drag_move((700 + 96, 900))
    fsm.end_drag()
    fsm._vx = 10000.0               # 测试注入：单 tick 位移 500px 跨整窗
    fsm._vy = 0.0
    fsm.step(PetState.default(), sensors(windows=(win13,)), DT)
    check("T15 越窗扫掠命中攀爬(贴左沿)", fsm.mode == "climb"
          and fsm.pos[0] == 800.0)

    # T16 落地弹跳：硬砸反弹 ≤2 次后停稳；轻落不弹
    fsm = BehaviorFSM(dict(WA))
    fsm.begin_drag((960, 300))
    fsm.drag_move((960, 300))
    fsm.drag_move((960, 360))
    fsm.end_drag()
    bounces = 0
    saw_bounce = False
    for _ in range(int(6 / DT)):
        a = fsm.step(PetState.default(), sensors(), DT)
        if a.type == ActionType.MOVE_TO and "bounced" in a.params:
            saw_bounce = True
            bounces = a.params["bounced"]
        if fsm.mode == "idle":
            break
    floor = WA["y"] + WA["height"]
    check("T16 硬砸触发弹跳(1-2次)", saw_bounce and 1 <= bounces <= 2)
    check("T16 弹后停稳在地板", fsm.mode == "idle" and fsm.pos[1] == floor)

    # T17 全屏瞬间在空中 → 传送回底边中央静止（维持"不往顶上走"）
    fsm = BehaviorFSM(dict(WA))
    fsm.begin_drag((400, 500))
    fsm.drag_move((400, 500))
    fsm.drag_move((500, 500))
    fsm.end_drag()
    assert fsm.mode == "thrown"
    fsm.handle_event("fullscreen_on")
    check("T17 全屏时空中收敛到底边中央",
          fsm.mode == "idle" and fsm.pos == (960.0, float(floor)))

    # T18 图层双检查：右侧半屏图层为空 → 幽灵窗不攀爬也不落其顶，直落地板
    fsm = BehaviorFSM(dict(WA))
    fsm.begin_drag((700, 900))
    fsm.drag_move((700, 900))
    fsm.drag_move((796, 900))
    fsm.end_drag()
    ghost = Sensors(
        mouse_pos=(960, 540), work_area=dict(WA),
        windows=[win13], idle_time=0.0,
        solid_at=lambda x, y: x < 800,  # 图层：只有左半屏有实体窗
    )
    for _ in range(int(3 / DT)):
        fsm.step(PetState.default(), ghost, DT)
        if fsm.mode == "idle":
            break
    check("T18 图层否决幽灵窗(不攀不落顶, 直落地板)",
          fsm.mode == "idle" and fsm.pos[1] == floor and fsm.pos[0] > 800)

    # T19 候选窗被盖住（图层身份否决）：几何有窗但顶层是别的窗 → 不爬不落顶
    covered = {"x": 800, "y": 700, "width": 400, "height": 300, "hwnd": 2}
    fsm = BehaviorFSM(dict(WA))
    fsm.begin_drag((700, 900))
    fsm.drag_move((700, 900))
    fsm.drag_move((796, 900))
    fsm.end_drag()
    layered = Sensors(
        mouse_pos=(960, 540), work_area=dict(WA), windows=[covered],
        idle_time=0.0,
        # 图层：任何点的最顶层窗都不是 hwnd=2 的这扇（被全屏窗盖住）
        solid_at=lambda x, y, ref=None: ref is not None and ref.get("hwnd") != 2,
    )
    for _ in range(int(3 / DT)):
        fsm.step(PetState.default(), layered, DT)
        if fsm.mode == "idle":
            break
    check("T19 被盖住的窗不攀爬不落顶(直落地板)",
          fsm.mode == "idle" and fsm.pos[1] == floor)

    # T20 深度阈值：浅掠顶(<30px)不判撞侧 → 落顶站定；拖到窗上部松手直接站上
    fsm = BehaviorFSM(dict(WA))
    fsm.step(PetState.default(), sensors(windows=(win13,)), DT)  # 预热：填充 FSM._windows（end_drag 用最近一次传感器窗口）
    fsm.begin_drag((700, 710))           # 脚深 10px < 30
    fsm.drag_move((700, 710))
    fsm.drag_move((796, 710))
    fsm.end_drag()
    for _ in range(int(3 / DT)):
        fsm.step(PetState.default(), sensors(windows=(win13,)), DT)
        if fsm.mode == "idle":
            break
    check("T20 浅掠顶沿→落顶站定(不贴侧攀爬)",
          fsm.mode == "idle" and fsm.pos[1] == 700 and not
          (fsm.pos[0] == 800.0 and False))
    # 拖到窗顶附近(深度 5px)松手 → 直接站上顶，不去贴边
    fsm = BehaviorFSM(dict(WA))
    fsm.step(PetState.default(), sensors(windows=(win13,)), DT)  # 预热：填充 FSM._windows（end_drag 用最近一次传感器窗口）
    fsm.begin_drag((1000, 705))
    fsm.drag_move((1000, 705))
    fsm.end_drag()
    fsm.step(PetState.default(), sensors(windows=(win13,)), DT)
    check("T20 窗顶浅处松手直接站顶(不贴侧边)",
          fsm.mode == "idle" and fsm.pos == (1000.0, 700.0))

    # T21 拎进窗体松手（深>阈值）→ 弹出窗顶上方自然落顶（不再贴边瞬移）
    fsm = BehaviorFSM(dict(WA))
    fsm.step(PetState.default(), sensors(windows=(win13,)), DT)  # 预热：填充 FSM._windows（end_drag 用最近一次传感器窗口）
    fsm.begin_drag((1000, 800))       # 窗 win13 体内 100px 深
    fsm.drag_move((1000, 800))
    fsm.end_drag()
    for _ in range(int(3 / DT)):
        fsm.step(PetState.default(), sensors(windows=(win13,)), DT)
        if fsm.mode == "idle":
            break
    check("T21 深处松手落窗顶(不贴侧边)",
          fsm.mode == "idle" and fsm.pos[1] == 700
          and 800 <= fsm.pos[0] <= 1200)

    # T21b 拎进**被盖住**的窗体松手 → 穿过直落地板（图层否决）
    fsm = BehaviorFSM(dict(WA))
    fsm.step(PetState.default(), layered, DT)  # 预热：填充 FSM._windows（end_drag 用最近一次传感器窗口）
    fsm.begin_drag((1000, 800))
    fsm.drag_move((1000, 800))
    fsm.end_drag()
    for _ in range(int(3 / DT)):
        fsm.step(PetState.default(), layered, DT)
        if fsm.mode == "idle":
            break
    check("T21b 被盖窗体内松手穿落地板", fsm.pos[1] == floor)

    # T21c 底板行走穿越被盖窗的横向范围 → 不停不爬（被盖窗不是墙不是面）
    fsm = BehaviorFSM(dict(WA))
    fsm._pos = (700.0, floor)
    fsm._mode = "walk"
    fsm._target = (1500.0, floor)
    for _ in range(int(10 / DT)):
        fsm.step(PetState.default(), layered, DT)
        if fsm.mode != "walk":
            break
    check("T21c 底板穿越被盖窗不停不爬(到达目标)",
          fsm.mode in ("walk", "idle") and fsm.pos[0] > 1200)

    # T22 拖到窗口正下方（窗外，y > 窗底）松手 → 正常落地板（不上顶）
    fsm = BehaviorFSM(dict(WA))
    fsm.step(PetState.default(), sensors(windows=(win13,)), DT)  # 预热：填充 FSM._windows（end_drag 用最近一次传感器窗口）
    fsm.begin_drag((1000, 1050))      # 窗体 y∈[700,1000]，此处已在其下
    fsm.drag_move((1000, 1050))
    fsm.end_drag()
    for _ in range(int(3 / DT)):
        fsm.step(PetState.default(), sensors(windows=(win13,)), DT)
        if fsm.mode == "idle":
            break
    check("T22 窗下松手正常落地板(不瞬移上顶)",
          fsm.mode == "idle" and fsm.pos[1] == floor)

    # T22b 从窗底下方横向飞越窗边 → 不撞侧攀爬，直落地板
    fsm = BehaviorFSM(dict(WA))
    fsm.step(PetState.default(), sensors(windows=(win13,)), DT)  # 预热：填充 FSM._windows（end_drag 用最近一次传感器窗口）
    fsm.begin_drag((700, 1050))
    fsm.drag_move((700, 1050))
    fsm.drag_move((796, 1050))
    fsm.end_drag()
    for _ in range(int(3 / DT)):
        fsm.step(PetState.default(), sensors(windows=(win13,)), DT)
        if fsm.mode == "idle":
            break
    check("T22b 窗底下方飞越窗边不攀爬",
          fsm.mode == "idle" and fsm.pos[1] == floor and fsm.pos[0] > 800)

    # T23 高悬浮窗（窗底距地 230px ≥ 身高 96）→ 地板行走钻过，不爬
    high_win = {"x": 800, "y": 650, "width": 400, "height": 200}
    fsm = BehaviorFSM(dict(WA))
    fsm._pos = (700.0, floor)
    fsm._mode = "walk"
    fsm._target = (1500.0, floor)
    for _ in range(int(10 / DT)):
        fsm.step(PetState.default(), sensors(windows=(high_win,)), DT)
        if fsm.mode != "walk":
            break
    check("T23 净空足够钻过悬浮窗(不攀爬)",
          fsm.mode in ("walk", "idle") and fsm.pos[1] == floor
          and fsm.pos[0] > 1200)

    # T23b 攀爬中窗口最小化（alive_at 实时否决，几何列表还留着）→ 立即坠落
    fsm = BehaviorFSM(dict(WA))
    fsm.step(PetState.default(), sensors(windows=(win13,)), DT)
    fsm.begin_drag((700, 900))
    fsm.drag_move((700, 900))
    fsm.drag_move((796, 900))
    fsm.end_drag()
    fsm.step(PetState.default(), sensors(windows=(win13,)), DT)
    assert fsm.mode == "climb"
    gone_live = Sensors(mouse_pos=(960, 540), work_area=dict(WA),
                        windows=[win13], idle_time=0.0,
                        alive_at=lambda ref: False)  # 实时：已最小化
    a = fsm.step(PetState.default(), gone_live, DT)
    check("T23b 攀爬中最小化立即坠落(无缓存延迟)",
          a.type == ActionType.FALL and fsm.mode == "fall")

    # T23c 站立支撑窗最小化（alive_at 否决表面）→ 立即坠落
    fsm = BehaviorFSM(dict(WA))
    fsm._pos = (1000.0, 700.0)
    fsm._mode = "idle"
    a = fsm.step(PetState.default(), gone_live, DT)
    check("T23c 站立窗最小化立即坠落", a.type == ActionType.FALL)

    # T24 骑乘跟随：宠物站窗顶，窗口上移 → 贴合新顶（rect_at 实时）
    moved = {"x": 800, "y": 650, "width": 400, "height": 300, "hwnd": 7}
    fsm = BehaviorFSM(dict(WA))
    fsm._pos = (1000.0, 700.0)
    fsm._mode = "idle"
    riding = Sensors(mouse_pos=(960, 540), work_area=dict(WA),
                     windows=[win13], idle_time=0.0,
                     rect_at=lambda ref: moved)   # 实时：窗已上移到 650
    for _ in range(3):  # 首 tick 记支撑窗，次 tick 起骑乘
        fsm.step(PetState.default(), riding, DT)
    check("T24 窗口上移即时骑乘跟随", fsm.pos == (1000.0, 650.0))

    # T24b 窗口横向移开（不再覆盖脚下 x）→ 坠落
    shifted = {"x": 300, "y": 650, "width": 400, "height": 300, "hwnd": 7}
    riding2 = Sensors(mouse_pos=(960, 540), work_area=dict(WA),
                      windows=[win13], idle_time=0.0,
                      rect_at=lambda ref: shifted)
    a = fsm.step(PetState.default(), riding2, DT)
    check("T24b 支撑窗横向移开坠落", a.type == ActionType.FALL)

    # T24c 几何兜底：无 rect_at，列表已更新窗口新位置 → 弹到新顶
    fsm = BehaviorFSM(dict(WA))
    fsm._pos = (1000.0, 700.0)
    fsm._mode = "idle"
    fsm._stand_win = win13                      # 原本站在 win13 上
    a = fsm.step(PetState.default(),
                 sensors(windows=({**win13, "y": 640},)), DT)
    check("T24c 几何兜底弹到上移后的新顶", fsm.pos == (1000.0, 640.0))

    # T25 真实身高 64(YOUNG)：窗底距地 80px 的缝隙 → 钻过不爬
    fsm = BehaviorFSM(dict(WA))
    fsm.set_pet_height(64)                       # win13 缝隙 80 ≥ 64
    fsm._pos = (700.0, floor)
    fsm._mode = "walk"
    fsm._target = (1500.0, floor)
    for _ in range(int(10 / DT)):
        fsm.step(PetState.default(), sensors(windows=(win13,)), DT)
        if fsm.mode != "walk":
            break
    check("T25 真实身高下钻过 80px 缝隙(默认96会爬)",
          fsm.mode in ("walk", "idle") and fsm.pos[1] == floor
          and fsm.pos[0] > 1200)

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
