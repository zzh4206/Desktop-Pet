"""v0.25 番茄线单测 —— pet/focus.py（wiki 设计-番茄模块技术方案.md §7）。

全虚拟时钟（``now_fn`` 注入假钟），零平台依赖、零真实计时。锁定契约：
状态迁移全路径（含 4 循环进 LONG / LONG 后回 FOCUS/IDLE 两分支）/ stop /
skip 语义 / DND 只静默不打断 / 睡眠大跳迁移不补算 / 当日统计跨日重置 +
streak / 重启重水化（跑着退出恢复、IDLE 退出不复活）/ 原子写往返。

后续版本在同文件追加：0.25.1 配置校验、0.25.2 工具与热键、0.25.3 FSM
事件 + 互斥、0.25.4 break_enforce 计量、0.25.5 mood 增益。
"""

from __future__ import annotations

import json
import os
from datetime import datetime

import pytest

from pet.focus import (FocusPhase, FocusSession, FocusStats, build_focus_tools,
                       _BREAK_GRACE_S, _BREAK_ACTIVITY_THRESHOLD_S)


# ---- 固件 ----

class FakeClock:
    """注入假钟：可步进、可跳秒（模拟睡眠/跨日）。"""

    def __init__(self, start=1_700_000_000.0):
        self.t = start

    def __call__(self):
        return self.t

    def advance(self, seconds: float):
        self.t += seconds


class BubbleRecorder:
    def __init__(self):
        self.calls: list[str] = []

    def __call__(self, msg: str):
        self.calls.append(msg)


def _session(clock=None, tmp_path=None, bubble=None, cfg=None, **kw):
    clock = clock or FakeClock()
    return FocusSession(
        now_fn=clock,
        bubble_fn=bubble or BubbleRecorder(),
        cfg=cfg or {},
        state_path=str(tmp_path / "focus_state.json") if tmp_path else None,
        **kw)


def _work_min(s):
    return s._work_min


# ---- 基础：start/active/IDLE 惰性 ----

def test_start_enters_focus():
    s = _session()
    assert s.state == FocusPhase.IDLE and not s.active
    s.start()
    assert s.state == FocusPhase.FOCUS and s.active


def test_idle_tick_zero_impact():
    """§1 不变量：IDLE 态 tick 零副作用（无迁移/无气泡/无落盘）。"""
    bub = BubbleRecorder()
    clock = FakeClock()
    s = _session(clock=clock, bubble=bub)
    s.tick()
    clock.advance(3600)
    s.tick()
    assert s.state == FocusPhase.IDLE
    assert bub.calls == []
    assert s.stats.completed == 0 and s.stats.focus_sec == 0


# ---- 迁移链全路径 ----

def test_focus_completes_to_short_and_counts():
    s = _session()
    clock = s._now
    s.start()
    clock.advance(25 * 60 + 1)
    s.tick()
    assert s.state == FocusPhase.SHORT
    assert s.stats.completed == 1
    assert s.stats.focus_sec == 25 * 60


def test_short_completes_back_to_focus():
    clock = FakeClock()
    s = _session(clock=clock)
    s.start()
    clock.advance(25 * 60 + 1)
    s.tick()                       # FOCUS→SHORT
    clock.advance(5 * 60 + 1)
    s.tick()                       # SHORT→FOCUS
    assert s.state == FocusPhase.FOCUS


def test_four_cycles_then_long():
    clock = FakeClock()
    s = _session(clock=clock)
    s.start()
    for _ in range(4):
        clock.advance(25 * 60 + 1)
        s.tick()                   # 完成第 N 个番茄
        if s.state == FocusPhase.SHORT:
            clock.advance(5 * 60 + 1)
            s.tick()               # 短休结束回 FOCUS
    # 第 4 个番茄完成后应进 LONG 而非 SHORT
    assert s.state == FocusPhase.LONG
    assert s.stats.completed == 4
    assert s.stats.focus_sec == 4 * 25 * 60


def test_long_completes_to_idle():
    clock = FakeClock()
    s = _session(clock=clock)
    s.start()
    # 快速跑完 4 番茄进 LONG
    for _ in range(4):
        clock.advance(25 * 60 + 1)
        s.tick()
        if s.state == FocusPhase.SHORT:
            clock.advance(5 * 60 + 1)
            s.tick()
    assert s.state == FocusPhase.LONG
    clock.advance(15 * 60 + 1)
    s.tick()
    assert s.state == FocusPhase.IDLE and not s.active


def test_long_skip_goes_focus_new_cycle():
    """§7 LONG 后回 FOCUS/IDLE 两分支之 FOCUS：skip 长休 → 新一轮 FOCUS。"""
    clock = FakeClock()
    s = _session(clock=clock)
    s.start()
    for _ in range(4):
        clock.advance(25 * 60 + 1)
        s.tick()
        if s.state == FocusPhase.SHORT:
            clock.advance(5 * 60 + 1)
            s.tick()
    assert s.state == FocusPhase.LONG
    s.skip()
    assert s.state == FocusPhase.FOCUS
    # 新一轮：再完成 4 个又应进 LONG（cycle 已归零）
    for _ in range(4):
        clock.advance(25 * 60 + 1)
        s.tick()
        if s.state == FocusPhase.SHORT:
            clock.advance(5 * 60 + 1)
            s.tick()
    assert s.state == FocusPhase.LONG


# ---- skip 语义 ----

def test_skip_focus_not_counted():
    s = _session()
    s.start()
    s.skip()
    assert s.state == FocusPhase.SHORT
    assert s.stats.completed == 0 and s.stats.focus_sec == 0


def test_skip_short_to_focus():
    clock = FakeClock()
    s = _session(clock=clock)
    s.start()
    clock.advance(25 * 60 + 1)
    s.tick()                       # FOCUS→SHORT
    s.skip()
    assert s.state == FocusPhase.FOCUS


# ---- stop / 重启 ----

def test_stop_any_state_returns_idle():
    clock = FakeClock()
    s = _session(clock=clock)
    s.start()
    clock.advance(25 * 60 + 1)
    s.tick()                       # SHORT
    s.stop()
    assert s.state == FocusPhase.IDLE
    assert s.stats.completed == 1  # 已完成的不回滚


def test_stop_idempotent():
    s = _session()
    s.stop()
    assert s.state == FocusPhase.IDLE


def test_start_while_running_reschedules():
    clock = FakeClock()
    s = _session(clock=clock)
    s.start()
    clock.advance(10 * 60)
    s.start(minutes=20)            # 改期重启：时长 20 分钟
    assert _work_min(s) == 20.0
    clock.advance(20 * 60 - 1)
    s.tick()
    assert s.state == FocusPhase.FOCUS      # 未到期
    clock.advance(2)
    s.tick()
    assert s.state == FocusPhase.SHORT


# ---- DND 只静默不打断 ----

def test_dnd_silent_but_transitions():
    bub = BubbleRecorder()
    clock = FakeClock()
    s = _session(clock=clock, bubble=bub, dnd_fn=lambda: True)
    s.start()
    bub.calls.clear()
    assert s.on_dnd_active() is True        # 正在跑
    clock.advance(25 * 60 + 1)
    s.tick()                                # 到期：节律照走，提醒静默
    assert s.state == FocusPhase.SHORT
    assert bub.calls == []


# ---- 睡眠大跳不补算 ----

def test_sleep_jump_migrates_once_no_backfill():
    clock = FakeClock()
    s = _session(clock=clock)
    s.start()
    clock.advance(2 * 3600)         # 睡 2 小时（跨过一个完整 25m 番茄）
    s.tick()
    # 只迁移一次到 SHORT，不链式补算多段
    assert s.state == FocusPhase.SHORT
    assert s.stats.completed == 1
    assert s.stats.focus_sec == 25 * 60


# ---- 当日统计：跨日重置 + streak ----

def test_cross_day_resets_daily_counters():
    clock = FakeClock()
    s = _session(clock=clock)
    s.start()
    clock.advance(25 * 60 + 1)
    s.tick()
    assert s.stats.completed == 1
    clock.advance(24 * 3600)        # 次日
    s.start()                       # 触发 rollover
    assert s.stats.date != "" and s.stats.completed == 0
    assert s.stats.focus_sec == 0 and s.stats.breaks_cut == 0


def test_streak_continues_across_days():
    clock = FakeClock()
    s = _session(clock=clock)
    s.start()
    clock.advance(25 * 60 + 1)
    s.tick()
    assert s.stats.streak_days == 1
    clock.advance(24 * 3600)
    s.start()
    clock.advance(25 * 60 + 1)
    s.tick()
    assert s.stats.streak_days == 2


def test_streak_resets_on_gap():
    clock = FakeClock()
    s = _session(clock=clock)
    s.start()
    clock.advance(25 * 60 + 1)
    s.tick()
    assert s.stats.streak_days == 1
    clock.advance(2 * 24 * 3600)    # 隔一天
    s.start()
    clock.advance(25 * 60 + 1)
    s.tick()
    assert s.stats.streak_days == 1


# ---- 持久化重水化 ----

def test_persist_and_resume_running(tmp_path):
    clock = FakeClock()
    s1 = _session(clock=clock, tmp_path=tmp_path)
    s1.start()
    clock.advance(10 * 60)
    s1.tick()                       # 运行中（未到期）→ 落盘进行中段
    # 新实例（同档案、同时钟）→ 恢复节律
    s2 = FocusSession(now_fn=clock, cfg={},
                      state_path=str(tmp_path / "focus_state.json"))
    assert s2.state == FocusPhase.FOCUS
    assert s2.active


def test_persist_idle_does_not_resurrect(tmp_path):
    clock = FakeClock()
    s1 = _session(clock=clock, tmp_path=tmp_path)
    s1.start()
    s1.stop()                       # IDLE 持久化为「无进行中段」
    s2 = FocusSession(now_fn=clock, cfg={},
                      state_path=str(tmp_path / "focus_state.json"))
    assert s2.state == FocusPhase.IDLE


def test_persist_overdue_drops_phase(tmp_path):
    clock = FakeClock()
    s1 = _session(clock=clock, tmp_path=tmp_path)
    s1.start()
    clock.advance(24 * 3600)        # 大幅过期（且跨日）
    s2 = FocusSession(now_fn=clock, cfg={},
                      state_path=str(tmp_path / "focus_state.json"))
    assert s2.state == FocusPhase.IDLE


def test_persist_writes_atomic_and_roundtrip(tmp_path):
    clock = FakeClock()
    s = _session(clock=clock, tmp_path=tmp_path)
    s.start()
    clock.advance(25 * 60 + 1)
    s.tick()
    p = tmp_path / "focus_state.json"
    assert os.path.isfile(p)
    assert not os.path.exists(str(p) + ".tmp")   # tmp 已被 replace
    raw = json.loads(p.read_text(encoding="utf-8"))
    assert raw["stats"]["completed"] == 1
    assert raw["phase"] == "short"
    assert isinstance(raw["deadline"], (int, float))


def test_stats_dataclass_defaults():
    st = FocusStats()
    assert st.date == "" and st.completed == 0 and st.focus_sec == 0
    assert st.breaks_cut == 0 and st.streak_days == 0


# ---- LLM 工具（0.25.2 起接线；工厂此处随核心入库） ----

def test_build_focus_tools_unconfigured():
    from pet.tools_schema import ToolContext
    schemas_handlers = build_focus_tools(None)
    assert len(schemas_handlers) == 2
    names = [sh.name for sh, _ in schemas_handlers]
    assert names == ["start_pomodoro", "stop_pomodoro"]
    ctx = ToolContext(pet_state=None, user_name="t", config={})
    r = schemas_handlers[0][1].execute({}, ctx)
    assert not r.success and "未启用" in r.message


def test_build_focus_tools_start_stop():
    from pet.tools_schema import ToolContext
    s = _session()
    start_h, stop_h = [h for _, h in build_focus_tools(s)]
    ctx = ToolContext(pet_state=None, user_name="t", config={})
    r = start_h.execute({}, ctx)
    assert r.success and s.state == FocusPhase.FOCUS
    r2 = stop_h.execute({}, ctx)
    assert r2.success and s.state == FocusPhase.IDLE
