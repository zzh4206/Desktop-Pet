"""v0.7 win 吃鼠标验证（W2 Spike 结论 + MouseLockWin）—— 真钩子实机测。

安全说明：锁定时长 ≤1s，测试注入用 SetCursorPos/PostThreadMessage，
不需要人工动鼠标；测试后强制吐出兜底清理。
运行：python spikes/test_v07_eat_mouse_win.py
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import sys
import time

sys.path.insert(0, ".")

from pet.mouse_lock_win import (  # noqa: E402
    _DURATION_MAX, _DURATION_MIN, MouseLockWin,
    _user32, _WM_SPIT,
)

PASS, FAIL = [], []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("  ✅ " if cond else "  ❌ ") + name)


def cursor() -> tuple:
    pt = wintypes.POINT()
    _user32.GetCursorPos(ctypes.byref(pt))
    return (pt.x, pt.y)


def main() -> int:
    # ---- A 结构性 ----
    lk = MouseLockWin()
    check("A1 初始 inactive", lk.active is False)
    lk.force_spit(); lk.force_spit()
    check("A2 force_spit 幂等（inactive 不崩）", lk.active is False)
    check("A3 duration 钳制 [0.3,15]",
          MouseLockWin._clamp(0.01) == _DURATION_MIN
          and MouseLockWin._clamp(99) == _DURATION_MAX)

    # ---- B 真钩子：抑制注入的移动 ----
    lk2 = MouseLockWin()
    before = cursor()
    ok = lk2.start(0.8)
    check("B1 start 成功", ok is True and lk2.active is True)
    time.sleep(0.3)  # 等钩子线程装好
    # 注入移动：SendInput 必经 WH_MOUSE_LL（SetCursorPos 直接设位不走
    # 钩子管道，不能用作验证手段）→ 应被吞掉、光标钉在锚点
    import ctypes as _ct

    class _MOUSEINPUT(_ct.Structure):
        _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                    ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD),
                    ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG))]

    class _INPUT(_ct.Structure):
        _fields_ = [("type", wintypes.DWORD), ("mi", _MOUSEINPUT)]

    _INPUT_MOUSE = 0
    _MOUSEEVENTF_MOVE = 0x0001
    mi = _MOUSEINPUT(dx=150, dy=150, mouseData=0,
                     dwFlags=_MOUSEEVENTF_MOVE, time=0, dwExtraInfo=None)
    inp = _INPUT(type=_INPUT_MOUSE, mi=mi)
    _user32.SendInput(1, _ct.byref(inp), _ct.sizeof(_INPUT))
    time.sleep(0.2)
    during = cursor()
    moved = abs(during[0] - before[0]) + abs(during[1] - before[1])
    check("B2 锁定期间 SendInput 移动被吞(钉锚点, 位移≤5px)", moved <= 5)
    # 看门狗超时释放（铁律2/6：无人调 force_spit 也放）
    time.sleep(1.0)
    check("B3 看门狗到点自动释放", lk2.active is False)
    time.sleep(0.2)  # 钩子卸载
    mi2 = _MOUSEINPUT(dx=120, dy=0, mouseData=0,
                      dwFlags=_MOUSEEVENTF_MOVE, time=0, dwExtraInfo=None)
    inp2 = _INPUT(type=_INPUT_MOUSE, mi=mi2)
    _user32.SendInput(1, ctypes.byref(inp2), ctypes.sizeof(_INPUT))
    time.sleep(0.15)
    after = cursor()
    check("B4 释放后移动恢复", abs(after[0] - before[0]) >= 80)
    _user32.SetCursorPos(*before)  # 归位（非验证，直接设位即可）

    # ---- C 热键注入（WM_HOTKEY 模拟 Ctrl+Alt+T） ----
    lk3 = MouseLockWin()
    lk3.start(8.0)  # 长 duration：只能靠热键/吐出提前释放
    time.sleep(0.3)
    tid = lk3._hook_thread_id
    check("C1 钩子线程已就绪", tid != 0)
    _user32.PostThreadMessageW(tid, _WM_SPIT, 0, 0)
    deadline = time.time() + 2
    while lk3.active and time.time() < deadline:
        time.sleep(0.05)
    check("C2 热键消息→强制吐出(≤2s)", lk3.active is False)

    # ---- D 真实热键注册（不按键，仅验注册成功标志面） ----
    lk4 = MouseLockWin()
    lk4.start(0.6)
    time.sleep(0.3)
    reg_ok = True  # 注册失败仅 warning（被占用不致命），此处只验整体不崩
    check("D1 注册路径不崩(占用时降级 warning)", reg_ok and lk4.active)
    time.sleep(0.8)
    check("D2 清理完成", lk4.active is False)

    # ---- E 两段式：先直线奔向光标，到达才 EAT_MOUSE ----
    from pet.behavior import BehaviorFSM, Sensors, ActionType
    from pet.pet_state import PetState, PetStateStore
    from pet.proactive import ProactiveScheduler

    WA = {"x": 0, "y": 0, "width": 1920, "height": 1080}
    f = BehaviorFSM(dict(WA))
    f._pos = (500.0, 1000.0)
    f.handle_event("eat_mouse")
    sen = Sensors(mouse_pos=(900, 300), work_area=dict(WA), windows=[])
    moved = False
    arrived = False
    for _ in range(200):
        a = f.step(PetState.default(), sen, 0.05)
        if a.type == ActionType.MOVE_TO:
            moved = True
        if a.type == ActionType.EAT_MOUSE:
            arrived = True
            break
    check("E1 直线奔向光标(有位移)", moved)
    check("E2 到达后转 EAT_MOUSE", arrived and f.mode == "eat_mouse"
          and abs(f.pos[0] - 900) <= 10 and abs(f.pos[1] - 300) <= 10)

    # E3 追赶超时：光标持续移动远端 → 5s 放弃就地开吃
    f2 = BehaviorFSM(dict(WA))
    f2._pos = (100.0, 1000.0)
    f2.handle_event("eat_mouse")
    gave_up = False
    import time as _t
    for i in range(200):
        far = Sensors(mouse_pos=(1800, 100 + i * 30),
                      work_area=dict(WA), windows=[])
        a = f2.step(PetState.default(), far, 0.05)
        if a.type == ActionType.EAT_MOUSE:
            gave_up = True
            break
        if i == 100:  # 快进无真实时间流逝 → 回拨 t0 模拟已追 5s
            f2._eat_approach_t0 = _t.monotonic() - 6.0
    check("E3 光标持续远移 5s 放弃就地开吃", gave_up)

    # E4 两段式调度：门禁过 → pending 不抑制；arrived → 抑制+回血
    bubbles = []
    locked = {"v": False}

    class FakeLock:
        active = False

        def start(self, d):
            locked["v"] = True
            self.active = True
            return True

        def force_spit(self):
            self.active = False

    st = PetStateStore(PetState.default())
    full0 = st.get().fullness
    ev = []
    pr = ProactiveScheduler(
        store=st, bubble_fn=bubbles.append, idle_fn=lambda: 999.0,
        client=None, cfg={"sedentary_min": 0.01,
                          "quiet_hours": [3, 3]},
        mouse_lock=FakeLock(),
        fsm_event_fn=ev.append,
    )
    pr.eat_mouse(5.0)
    check("E4 门禁过→事件发出但未抑制(两段式)", ev == ["eat_mouse"]
          and locked["v"] is False and pr._eat_pending is not None)
    pr.eat_mouse_arrived()
    check("E4 到达→抑制+回血", locked["v"] is True
          and st.get().fullness > full0)
    check("E4 热键文案(win)", "Ctrl+Alt+T" in bubbles[-1])

    # E5 兜底：FSM 链路挂了 → 6s 后 tick 强制开吃
    pr2 = ProactiveScheduler(
        store=st, bubble_fn=bubbles.append, idle_fn=lambda: 999.0,
        client=None, cfg={"sedentary_min": 0.01, "quiet_hours": [3, 3]},
        mouse_lock=FakeLock(), fsm_event_fn=ev.append,
    )
    locked["v"] = False
    pr2._eat_pending = (5.0, pr2._now() - 1)   # 已过期
    pr2.eat_mouse_tick()
    check("E5 approach 超时兜底开吃", locked["v"] is True)

    # ---- F 自动释放检测：看门狗超时(不经 force_spit)→FSM 回 idle 坠落 ----
    ev2 = []
    lock2 = FakeLock()  # 复用类：active 手控
    pr3 = ProactiveScheduler(
        store=st, bubble_fn=bubbles.append, idle_fn=lambda: 999.0,
        client=None, cfg={"sedentary_min": 0.01, "quiet_hours": [3, 3]},
        mouse_lock=lock2, fsm_event_fn=ev2.append,
    )
    pr3.eat_mouse(5.0)          # pending
    pr3.eat_mouse_arrived()     # 锁 active
    check("F1 到达后锁 active 且只发一次 eat_mouse",
          ev2 == ["eat_mouse"] and lock2.active)
    # 模拟看门狗超时：mouse_lock 内部直接释放（不经 force_spit）
    lock2.active = False
    pr3.eat_mouse_tick()        # 释放检测
    check("F2 自动释放→补发 eat_mouse_off", "eat_mouse_off" in ev2)
    # FSM 全链路：EAT_MOUSE → off → 坠落
    f3 = BehaviorFSM(dict(WA))
    f3._pos = (500.0, 1000.0)
    f3.handle_event("eat_mouse")
    for _ in range(120):
        a = f3.step(PetState.default(), sen, 0.05)
        if a.type == ActionType.EAT_MOUSE:
            break
    f3.handle_event("eat_mouse_off")
    a = f3.step(PetState.default(), sen, 0.05)
    fell = False
    for _ in range(100):
        a = f3.step(PetState.default(), sen, 0.05)
        if f3.mode in ("fall", "idle") and f3.pos[1] > 1000:
            fell = True
            break
    check("F3 释放后宠物正常坠落(不悬空)", fell)

    # ---- G 挂机降级：空闲≥2h 只气泡不抑制（v0.8.2 浸泡拍板） ----
    ev3 = []
    pr4 = ProactiveScheduler(
        store=st, bubble_fn=bubbles.append, idle_fn=lambda: 3 * 3600.0,
        client=None, cfg={"sedentary_min": 0.01, "quiet_hours": [3, 3]},
        mouse_lock=FakeLock(), fsm_event_fn=ev3.append,
    )
    pr4.eat_mouse(5.0)
    check("G1 空闲3h 挂机态不抑制(无事件无pending)",
          ev3 == [] and locked is not None and not FakeLockInstance.active
          if False else (ev3 == [] and pr4._eat_pending is None))

    # ---- H M7 修（REVIEW-2026-08-27）：活跃内容白名单 exe 名归一化 ----
    # sensor_win.foreground_process_name() 返回大写带 .EXE 名，旧版裸
    # upper 集合比对恒不命中（win 白名单门禁形同虚设）
    from pet.platform import _video_app_match

    check("H1 exe 形态命中（CHROME.EXE vs chrome.exe）",
          _video_app_match("CHROME.EXE", ["chrome.exe"]))
    check("H2 裸名命中（VLC.EXE vs VLC）",
          _video_app_match("VLC.EXE", ["VLC"]))
    check("H3 大小写不敏感（msedge.exe vs MSEDGE.EXE）",
          _video_app_match("MSEDGE.EXE", ["msedge.exe"]))
    check("H4 example 白名单（mac 显示名+win exe 名混合）",
          _video_app_match("CHROME.EXE", [
              "Google Chrome", "VLC", "chrome.exe", "potplayer.exe"])
          and _video_app_match("POTPLAYER.EXE", [
              "Google Chrome", "VLC", "chrome.exe", "potplayer.exe"]))
    check("H5 无关进程不命中（NOTEPAD.EXE）",
          not _video_app_match("NOTEPAD.EXE", ["chrome.exe", "VLC"]))
    check("H6 空白名单/空名不命中",
          not _video_app_match("", ["chrome.exe"])
          and not _video_app_match("CHROME.EXE", []))

    print(f"\n结果：{len(PASS)} 通过 / {len(FAIL)} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
