"""v0.7 吃鼠标 S1 Spike —— CGEventTap 抑制 + 看门狗 + 热键 + 四门禁验证。

开发前置（设计思路.md §七 / mac任务清单 v0.7 S1）：CGEventTap 能抑制
鼠标 + 看门狗能释放。本 spike 分两层：

A. **结构性 + 门禁逻辑**（自动，免 Accessibility）：符号可用性 / 看门狗
   是 daemon 线程 / 鼠标 mask 不含键盘 / 键盘 tap listen-only / _release
   幂等 / ProactiveScheduler.eat_mouse 四门禁（idle/DND/活跃内容/
   accessibility）+ 回血，用 FakeMouseLock 注入跑（不碰真 CGEventTap）。

B. **真机冒烟**（需 Accessibility 授权 ``.venv/bin/python``）：起 3s
   真抑制，pump 主 run loop，验 active True→False（看门狗按 deadline
   释放）。鼠标被抑制/恢复由人肉确认。未授权则跳过 B 并打印授权指引。

运行：.venv/bin/python spikes/test_v07_eat_mouse.py
最高风险版本——bug 可能锁死鼠标。看门狗先验；B 节用短 duration 3s 测。
"""

from __future__ import annotations

import sys
import time

sys.path.insert(0, ".")

PASS, FAIL = [], []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("  ✅ " if cond else "  ❌ ") + name)


# =====================================================================
# A. 结构性 + 门禁逻辑（自动）
# =====================================================================

def section_structural() -> None:
    # 平台守卫（v0.7 win 适配）：A1-A5 mac 专属（Quartz/mouse_lock_mac），
    # win 跳过本节——win 侧等价检查在 test_v07_eat_mouse_win.py
    import sys as _sys
    if _sys.platform != "darwin":
        print("  ⏭ A1-A5 mac 专属，win 跳过（win 侧见 test_v07_eat_mouse_win.py）")
        return
    import pet.mouse_lock_mac as m

    # A1 符号可用
    check("A1 Quartz/CoreFoundation 符号可用",
          m._HAS_QUARTZ and m._HAS_CF)

    # A2 鼠标 mask 不含键盘位（铁律1：键盘不进鼠标抑制回调）
    import Quartz
    key_bit = Quartz.CGEventMaskBit(Quartz.kCGEventKeyDown)
    check("A2 鼠标 mask 不含键盘位（铁律1）", not (m._MOUSE_MASK & key_bit))
    check("A3 键盘 mask 仅 KeyDown", m._KEY_MASK == key_bit)

    # A4 热键常量：T=17，修饰=Cmd|Option
    check("A4 吐出热键 T keycode=17", m._SPIT_KEYCODE == 17)
    want = Quartz.kCGEventFlagMaskCommand | Quartz.kCGEventFlagMaskAlternate
    check("A4 吐出热键修饰=Cmd|Option", m._SPIT_FLAGS == want)

    # A5 _release 幂等（inactive 上调不崩）
    lk = m.MouseLockMac()
    lk.force_spit(); lk.force_spit()
    check("A5 force_spit 幂等（inactive 不崩）", lk.active is False)

    # A6 duration 钳制 ≤15s（铁律2）——start 内钳制；无 Accessibility 时
    # start 直接返 False（不创建 tap），用反射验常量
    check("A6 duration 上限 15s 常量", m._MAX_DURATION == 15.0)

    # A7 看门狗是 daemon 线程（结构性：类源码含 daemon=True + threading）
    import inspect
    src = inspect.getsource(m.MouseLockMac._watchdog_loop)
    check("A7 看门狗 daemon 线程（独立于主逻辑）",
          "daemon=True" in inspect.getsource(m.MouseLockMac.start)
          and "threading.Thread" in inspect.getsource(m.MouseLockMac.start))

    # A8 平台 API 隔离：共享 proactive.py 不直 import 平台库
    import pet.proactive as p
    pro_src = inspect.getsource(p)
    leak = [n for n in ("import Quartz", "from Quartz", "import AppKit",
                        "from AppKit", "mouse_lock_mac", "CGEventTap",
                        "ctypes") if n in pro_src]
    check("A8 ProactiveScheduler/EatMouseSession 零平台库 import", not leak)


def _make(fake_lock, idle_s=999.0, cfg=None, dnd_fn=None,
          active_content_fn=None, accessibility_fn=None,
          fsm_events=None, prompt=None, fullscreen_fn=None, idle_fn=None):
    """建 ProactiveScheduler + 注入 fake mouse_lock / 各门禁 fn。

    批次D/E3：idle_fn 覆写（异常注入用；缺省仍 lambda: idle_s）。"""
    from datetime import datetime
    from pet.proactive import ProactiveScheduler
    from pet.pet_state import PetState, PetStateStore

    class FakeClock:
        def __init__(self):
            self.t = datetime.fromisoformat("2026-08-17 14:00").timestamp()

        def __call__(self):
            return self.t

    bubbles = []
    s = ProactiveScheduler(
        store=PetStateStore(PetState.default()),
        bubble_fn=bubbles.append,
        idle_fn=idle_fn if idle_fn is not None else (lambda: idle_s),
        cfg=cfg or {},
        now_fn=FakeClock(),
        mouse_lock=fake_lock,
        dnd_fn=dnd_fn,
        active_content_fn=active_content_fn,
        accessibility_fn=accessibility_fn,
        fsm_event_fn=(lambda ev: fsm_events.append(ev)) if fsm_events is not None else None,
        prompt_accessibility_fn=prompt,
        fullscreen_fn=fullscreen_fn,
    )
    return s, bubbles


class FakeMouseLock:
    """记录 start/force_spit/active，免真 CGEventTap 测门禁逻辑。"""
    def __init__(self, start_ok=True):
        self.active = False
        self._start_ok = start_ok
        self.starts = 0
        self.spits = 0

    def start(self, duration_s):
        self.starts += 1
        self.active = self._start_ok
        return self._start_ok

    def force_spit(self):
        self.spits += 1
        self.active = False


def section_gates() -> None:
    from pet.pet_state import PetState, PetStateStore

    # G1 idle<阈值 → 不抑制（铁律5）
    lock = FakeMouseLock()
    s, bubbles = _make(lock, idle_s=60.0, cfg={"idle_threshold_min": 5},
                       accessibility_fn=lambda: True)
    s.eat_mouse(10)
    check("G1 idle<阈值 不抑制（starts=0）", lock.starts == 0)

    # G2 idle≥阈值 + 全门禁过 → 抑制 + FSM eat_mouse + 回血
    lock = FakeMouseLock()
    evs = []
    s, _ = _make(lock, idle_s=30 * 60, cfg={"idle_threshold_min": 5,
                                            "eat_mouse_gain": {"fullness": 7}},
                 accessibility_fn=lambda: True, fsm_events=evs)
    store = s._store
    before = store.get().fullness
    s.eat_mouse(10)
    # v0.7.3 两段式（win 主笔，mac 同步）：门禁过 → 发 approach 事件 +
    # pending（未抑制）；FSM 奔到光标（eat_mouse_arrived）才抑制+回血
    check("G2 全过 → 先发事件暂不抑制(pending)", evs == ["eat_mouse"]
          and lock.starts == 0 and s._eat_pending is not None)
    s.eat_mouse_arrived()
    after = store.get().fullness
    check("G2 到达 → 抑制（starts=1, active）",
          lock.starts == 1 and lock.active)
    check("G2 回血 +饱食", after - before == 7)

    # G3 DND → 不抑制（铁律4）
    lock = FakeMouseLock()
    s, _ = _make(lock, idle_s=30 * 60, cfg={"idle_threshold_min": 5,
                                           "dnd": True},
                 accessibility_fn=lambda: True)
    s.eat_mouse(10)
    check("G3 DND 不抑制（starts=0）", lock.starts == 0)

    # G4 活跃内容 → 不抑制（T8）
    lock = FakeMouseLock()
    s, _ = _make(lock, idle_s=30 * 60, cfg={"idle_threshold_min": 5},
                 active_content_fn=lambda: True,
                 accessibility_fn=lambda: True)
    s.eat_mouse(10)
    check("G4 前台视频 不抑制（starts=0）", lock.starts == 0)

    # G5 accessibility 未开 → 不抑制 + 提示 + 深链（T9）
    lock = FakeMouseLock()
    prompted = []
    s, bubbles = _make(lock, idle_s=30 * 60, cfg={"idle_threshold_min": 5},
                       accessibility_fn=lambda: False,
                       prompt=lambda: prompted.append(1))
    s.eat_mouse(10)
    check("G5 accessibility 未开 不抑制（starts=0）", lock.starts == 0)
    check("G5 提示气泡含辅助功能", any("辅助功能" in b for b in bubbles))
    check("G5 深链系统设置", len(prompted) == 1)

    # G6 tap 创建失败 → 不抑制 + 提示（fail-open 气泡）
    lock = FakeMouseLock(start_ok=False)
    s, bubbles = _make(lock, idle_s=30 * 60, cfg={"idle_threshold_min": 5},
                       accessibility_fn=lambda: True)
    s.eat_mouse(10)
    check("G6 tap 创建失败 不抑制（active False）", not lock.active)
    check("G6 tap 失败提示气泡", any("没管住" in b for b in bubbles))

    # G6b M4 修（REVIEW-2026-08-27）：抑制启动失败要补发 eat_mouse_off 让
    # FSM 退出 EAT_MOUSE（旧版只发气泡，宠物冻在光标处咀嚼仅热键可解）
    lock = FakeMouseLock(start_ok=False)
    evs = []
    s, bubbles2 = _make(lock, idle_s=30 * 60,
                        cfg={"idle_threshold_min": 5},
                        accessibility_fn=lambda: True, fsm_events=evs)
    s.eat_mouse(10)          # 两段式：evs=["eat_mouse"]，pending 记账
    s.eat_mouse_arrived()    # 到达后 start 失败 → M4 回退事件
    check("G6b 启动失败补发 eat_mouse_off（FSM 退出吃鼠标态）",
          evs == ["eat_mouse", "eat_mouse_off"]
          and any("没管住" in b for b in bubbles2))

    # G6c 批次C（REVIEW-2026-08-28 H2）：第五门禁——前台全屏 → 不抑制
    lock = FakeMouseLock()
    evs = []
    s, _ = _make(lock, idle_s=30 * 60, cfg={"idle_threshold_min": 5},
                 accessibility_fn=lambda: True, fsm_events=evs,
                 fullscreen_fn=lambda: True)
    s.eat_mouse(10)
    check("G6c 前台全屏 不抑制（starts=0 且不发 approach 事件）",
          lock.starts == 0 and evs == [] and s._eat_pending is None)

    # G6d 批次C：追赶途中转全屏 → 到达复查放弃抑制 + eat_mouse_off 收场
    lock = FakeMouseLock()
    evs = []
    fs_state = {"on": False}
    s, _ = _make(lock, idle_s=30 * 60, cfg={"idle_threshold_min": 5},
                 accessibility_fn=lambda: True, fsm_events=evs,
                 fullscreen_fn=lambda: fs_state["on"])
    s.eat_mouse(10)            # 门禁时未全屏：发 approach + pending
    fs_state["on"] = True      # 用户在 6s 追赶窗口内开始放映
    s.eat_mouse_arrived()      # 到达复查 → 放弃抑制
    check("G6d 追赶途中转全屏 到达放弃抑制（starts=0, off 收场）",
          lock.starts == 0 and evs == ["eat_mouse", "eat_mouse_off"]
          and s._eat_pending is None)

    # G7 force_spit → 吐出 + FSM eat_mouse_off 回 idle
    lock = FakeMouseLock()
    evs = []
    s, _ = _make(lock, idle_s=30 * 60, cfg={"idle_threshold_min": 5},
                 accessibility_fn=lambda: True, fsm_events=evs)
    s.eat_mouse(10)        # evs=["eat_mouse"], lock.active=True
    s.force_spit()
    check("G7 force_spit 吐出（spits=1, active False）",
          lock.spits == 1 and not lock.active)
    check("G7 force_spit 发 eat_mouse_off", evs == ["eat_mouse", "eat_mouse_off"])

    # ---- 批次D/E3（REVIEW-2026-09-05）：门禁检查器异常 fail-closed 分支 ----
    def _boom():
        raise RuntimeError("boom")

    lock = FakeMouseLock()
    s, _ = _make(lock, idle_s=30 * 60, cfg={"idle_threshold_min": 5},
                 active_content_fn=_boom, accessibility_fn=lambda: True)
    s.eat_mouse(10)
    check("E3a active_content_fn 异常保守不吃", lock.starts == 0)

    lock = FakeMouseLock()
    s, _ = _make(lock, idle_s=30 * 60, cfg={"idle_threshold_min": 5},
                 fullscreen_fn=_boom, accessibility_fn=lambda: True)
    s.eat_mouse(10)
    check("E3b fullscreen_fn 异常保守不吃", lock.starts == 0)

    lock = FakeMouseLock()
    s, _ = _make(lock, idle_s=30 * 60, cfg={"idle_threshold_min": 5,
                                            "dnd": False}, dnd_fn=_boom,
                 accessibility_fn=lambda: True)
    s.eat_mouse(10)
    check("E3c dnd_fn 异常按 DND 处理（不吃）", lock.starts == 0)

    lock = FakeMouseLock()
    s, _ = _make(lock, cfg={"idle_threshold_min": 5}, idle_fn=_boom,
                 accessibility_fn=lambda: True)
    s.eat_mouse(10)
    check("E3d idle_fn 异常跳过本轮（不吃）", lock.starts == 0)

    # E3e 到达复查 fullscreen_fn 异常 → 放弃抑制（fail-closed，对齐 L11）
    # 门禁态正常过（发 approach），仅到达复查时抛异常
    lock = FakeMouseLock()
    evs = []
    _fs_mode = {"v": "off"}   # off=正常 False / boom=抛异常 / on=True

    def _fs():
        if _fs_mode["v"] == "boom":
            raise RuntimeError("boom")
        return _fs_mode["v"] == "on"

    s, _ = _make(lock, idle_s=30 * 60, cfg={"idle_threshold_min": 5},
                 accessibility_fn=lambda: True, fsm_events=evs,
                 fullscreen_fn=_fs)
    s.eat_mouse(10)
    _fs_mode["v"] = "boom"
    s.eat_mouse_arrived()
    check("E3e 到达复查异常放弃抑制（starts=0, off 收场）",
          lock.starts == 0 and "eat_mouse_off" in evs
          and s._eat_pending is None)

    # G8 EatMouseSession.on_dnd_active 返是否刚才在吃
    from pet.proactive import EatMouseSession
    lock = FakeMouseLock()
    sess = EatMouseSession(mouse_lock=lock)
    check("G8 未吃时 on_dnd_active 返 False", sess.on_dnd_active() is False)
    sess.start(10)         # lock.active=True
    check("G8 吃时 on_dnd_active 返 True 并吐出",
          sess.on_dnd_active() is True and not lock.active)

    # G9 duration 钳制（铁律2）：钳制在 MouseLockMac.start（非 fake），
    #    无 Accessibility 跑不了真 start，验源码含 min(_MAX_DURATION) 钳制 +
    #    EatMouseSession 透传 duration 给 mouse_lock（spy 记收到值）
    import inspect
    import pet.mouse_lock_mac as m
    check("G9a MouseLockMac.start 源码钳制 ≤15s（铁律2）",
          "min(_MAX_DURATION" in inspect.getsource(m.MouseLockMac.start))
    lock = FakeMouseLock()
    received = []
    orig_start = lock.start
    def spy(d):
        received.append(d)
        return orig_start(d)
    lock.start = spy
    s, _ = _make(lock, idle_s=30 * 60, cfg={"idle_threshold_min": 5},
                 accessibility_fn=lambda: True)
    s.eat_mouse(999)
    check("G9b eat_mouse 透传 duration 给 mouse_lock.start",
          received and received[0] == 999)


# =====================================================================
# B. 真机冒烟（需 Accessibility）
# =====================================================================

def section_live() -> None:
    import pet.mouse_lock_mac as m

    if not m.MouseLockMac.accessibility_trusted():
        print("  ⏭  B 跳过：Accessibility 未授权 ``.venv/bin/python``")
        print("     授权：系统设置 → 隐私与安全 → 辅助功能 → 加 .venv/bin/python")
        print("     授权后重跑本 spike 验真抑制 + 看门狗；B 节短 duration 3s 测")
        return

    from CoreFoundation import (
        CFRunLoopRunInMode, kCFRunLoopDefaultMode,
    )

    # B1 start(3s) → active True（真抑制开始）
    lk = m.MouseLockMac()
    ok = lk.start(3.0)
    check("B1 start(3s) 成功抑制（active True）", ok and lk.active)
    if not ok:
        return

    # B2 pump run loop 3.6s → 看门狗按 deadline(~3s) 强制释放
    print("  … 现在动鼠标应动不了（3s 内）；~3s 后看门狗自动释放（人肉确认）")
    end = time.monotonic() + 3.6
    while time.monotonic() < end:
        CFRunLoopRunInMode(kCFRunLoopDefaultMode, 0.2, False)
    check("B2 看门狗 deadline 释放（active False）", not lk.active)

    # B3 热键路径：再起一次，手动按 Cmd+Option+T 应吐出（listen tap 已随
    #    上一轮 release 停；这次新起会重建键盘 tap）。3s 兜底防不按。
    lk2 = m.MouseLockMac()
    lk2.start(3.0)
    print("  … 现在按 Cmd+Option+T 应立即吐出（3s 兜底）")
    end = time.monotonic() + 3.6
    while time.monotonic() < end:
        CFRunLoopRunInMode(kCFRunLoopDefaultMode, 0.2, False)
    check("B3 第二轮看门狗释放（active False）", not lk2.active)


def main() -> int:
    print("== A. 结构性 + 门禁逻辑 ==")
    section_structural()
    print("== G. 四门禁 + 回血 + force_spit（FakeMouseLock）==")
    section_gates()
    print("== B. 真机冒烟（需 Accessibility）==")
    section_live()

    print(f"\n结果：{len(PASS)} 通过 / {len(FAIL)} 失败")
    if FAIL:
        print("失败：", *FAIL, sep="\n  - ")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
