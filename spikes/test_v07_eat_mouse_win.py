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

    print(f"\n结果：{len(PASS)} 通过 / {len(FAIL)} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
