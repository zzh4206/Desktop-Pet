"""主动关怀（差异化缝合之一）—— 接口冻结于 设计思路.md §2.2。

v0.6（win 端主笔）：
- **链式唤醒**：醒来发一条关怀 → 由 LLM（隔离上下文，不掺聊天历史）决定
  下次唤醒间隔（钳制 10–360 min）→ 无 LLM/失败走本地罐头 + 随机 30–120 min。
- **久坐提醒**：系统空闲 ≥ 阈值（默认 45 min）→ 轮换 喝水/护眼/吃饭 气泡，
  同一久坐段冷却 `sedentary_cooldown_min`（默认 30）再发下一条。
- **早安/晚安**：窗口时段内每天一次。
- **节日祝福**：内置节日表（config 可扩展）。
- **follow-up**：`follow_up(when, msg)` 定时回访（聊天里"去吃饭"→ 30min 后问）。
- **深夜不打扰**：quiet_hours（默认 23:00–08:00）内除 follow-up 外全部静默。

v0.6.2：链式唤醒到点但处深夜静默期时**重排到 quiet 结束**（非旧版置 None 丢弃
致链条永久断裂）；LLM 决策**异步化**（ProactiveWorker QThread 包裹 chat_once，
主线程不阻塞，旧版同步调 chat_once 冻 UI 最长 120s）；决策轮用
`chat_once(system_override, tools_override=None)` 隔离人设+不挂工具（治
ctx=None 崩 + 人设冲突）；JSON 解析剥离 ```json``` 围栏容错。

平台库-free：时钟/气泡/LLM 全部注入（`now_fn`/`bubble_fn`/`client`），
app 用 QTimer 驱动 `poll()`；测试用假时钟直接调 `poll(now)`。
EatMouseSession v0.7 实装（薄包装 platform mouse_lock + FSM 事件派发）。
"""

from __future__ import annotations

import json
import logging
import os
import random
import sys
import re
import time
from collections import deque
from datetime import date, datetime, timedelta

from PySide6.QtCore import QObject, QThread, Signal, Slot

from .pet_state import PetStateStore

_log = logging.getLogger("pet")

# ---- 默认参数（进 config 可覆盖，键见 app _proactive_cfg） ----
_WAKE_MIN_MIN, _WAKE_MIN_MAX = 10.0, 360.0     # 链式唤醒钳制
_LOCAL_WAKE_RANGE = (30.0, 120.0)               # 无 LLM 时随机
_SEDENTARY_MIN = 45.0                           # 久坐阈值(空闲分钟)
_SEDENTARY_COOLDOWN = 30.0                      # 久坐段内提醒冷却
_SEDENTARY_TOPICS = ("喝口水休息一下眼睛～", "站起来活动一下吧～",
                     "该吃点东西啦～")
_MORNING_HOURS = (8, 11)                        # 早安窗口
_NIGHT_HOURS = (21, 23)                         # 晚安窗口

# ---- v0.7 吃鼠标参数（进 config.proactive 覆盖） ----
_EAT_DURATION = 10.0        # 单次锁定时长（s）——≤15s 铁律由 mouse_lock 兜底钳制
_EAT_IDLE_MIN = 5.0         # 铁律5 idle gate：idle<此值只气泡不吃
_EAT_GAIN = {"fullness": 5.0, "mood": 3.0}     # 养成回血（§七 +饱食/+心情）

_FESTIVALS = {  # MM-DD → 名称（config festivals 可扩展）
    "01-01": "元旦", "02-14": "情人节", "05-01": "劳动节",
    "06-01": "儿童节", "10-01": "国庆节", "12-25": "圣诞节",
}

_DECISION_SYSTEM = (
    "你是桌面宠物的主动关怀决策器。只输出一个 JSON 对象："
    '{"message": "一句简短关怀(≤25字)", "next_min": 数字(10-360)}。'
    "不要输出其他任何内容。"
)


class EatMouseSession:
    """吃鼠标 session（接口冻结于 设计思路.md §2.2；v0.7 实装行为）。

    薄包装：经 platform 注入的 ``mouse_lock``（mac=MouseLockMac）做系统层
    鼠标抑制，**不直 import 平台库**（补遗#7）。门禁（idle/DND/活跃内容/
    accessibility）由上层 ``ProactiveScheduler.eat_mouse`` 判；本 session 只管
    start 抑制 + force_spit 吐出 + DND 中吐出。

    FSM 事件由注入的 ``fsm_event_fn`` 派发（``eat_mouse`` 切咀嚼态 / ``eat_mouse_off``
    回 idle）——动画由 FSM 驱动（§七），session 只管系统层。
    """

    def __init__(self, mouse_lock=None, fsm_event_fn=None) -> None:
        self._lock = mouse_lock        # platform MouseLockMac | None
        self._fsm = fsm_event_fn       # callable(event: str) -> None | None
        self._was_active = False       # 释放检测（sync_release 用）

    @property
    def active(self) -> bool:
        """是否正在抑制鼠标（委托 platform mouse_lock.active）。无注入 → False。"""
        return bool(self._lock.active) if self._lock is not None else False

    def start(self, duration_s: float) -> None:
        """抑制鼠标（§2.2：``-> None``）。无 platform mouse_lock → 无操作。

        v0.7.4 两段式：``eat_mouse`` 事件由 scheduler 两段式入口发（FSM 先
        奔向光标），本方法在到达后才被调——只做系统层 start + 记
        ``_was_active`` 供 ``sync_release`` 的 active→False 释放检测；不再
        自发 FSM 事件（会在到达后重复触发追赶）。失败（权限/系统拒绝）由
        调用方读 ``sess.active`` 判定（``eat_mouse_arrived`` 的失败分支
        补发 ``eat_mouse_off`` 让 FSM 退出 EAT_MOUSE，M4）。

        批次J/L9（REVIEW-2026-08-31）：docstring 修正——旧版误写"失败返
        False"，本方法恒返 None（冻结签名）。"""
        if self._lock is None:
            return
        ok = self._lock.start(duration_s)
        if ok:
            self._was_active = True

    def force_spit(self) -> None:
        """强制吐出（热键/托盘/shutdown 调；幂等）。停抑制 + 回 idle 态。"""
        if self._lock is not None:
            self._lock.force_spit()
        self._was_active = False
        if self._fsm is not None:
            try:
                self._fsm("eat_mouse_off")
            except Exception:
                _log.warning("[吃鼠标] fsm eat_mouse_off 事件派发异常", exc_info=True)

    def sync_release(self) -> None:
        """释放检测（scheduler.eat_mouse_tick 每 tick 调）：

        看门狗超时/任何外部路径直接释放 mouse_lock 时不经过 force_spit——
        FSM 会冻在 EAT_MOUSE（宠物悬空在光标处）。此处监测锁 active→False
        的跳变，补发 eat_mouse_off 回 idle（支撑校验自然坠落）。跨平台：
        不改 mouse_lock 接口，mac 看门狗同款问题一并覆盖。
        """
        if self._was_active and not self.active:
            self._was_active = False
            if self._fsm is not None:
                try:
                    self._fsm("eat_mouse_off")
                    _log.info("[吃鼠标] 自动释放→FSM 回 idle(坠落)")
                except Exception:
                    _log.warning("[吃鼠标] 释放检测事件派发异常",
                                 exc_info=True)

    def on_dnd_active(self) -> bool:
        """DND 生效时调：若正在吃 → 立即吐出。返是否刚才在吃（铁律4）。"""
        was = self.active
        if was:
            self.force_spit()
        return was


class ProactiveScheduler:
    def __init__(
        self,
        store: PetStateStore,
        bubble_fn,                       # callable(text) -> None（app 接气泡）
        idle_fn,                         # callable() -> float 空闲秒
        client=None,                     # DeepSeekClient | None（无则本地罐头）
        cfg: dict | None = None,
        now_fn=time.time,
        # v0.7 吃鼠标平台注入（全可选；None 时 eat_mouse 退化为静默/不抑制，
        # 保持 win/纯逻辑测试向后兼容）。平台 mouse_lock + 各门禁检查器均经此
        # 注入，共享 ProactiveScheduler 零平台库（补遗#7）。
        mouse_lock=None,                 # platform MouseLockMac | None
        dnd_fn=None,                     # callable() -> bool 系统专注/勿扰
        active_content_fn=None,          # callable() -> bool 前台视频白名单命中
        accessibility_fn=None,           # callable() -> bool Accessibility 已授权
        fsm_event_fn=None,               # callable(event: str) -> None 切 EAT_MOUSE
        prompt_accessibility_fn=None,     # callable() -> None 深链系统设置
        fullscreen_fn=None,              # 批次C（REVIEW-2026-08-28 H2）：
        #   callable() -> bool 前台全屏/演示（win adapter.is_fullscreen_active）
        state_path: str | None = None,   # 批次B/P2-6（REVIEW-2026-09-05）：
        #   问候/节日/follow-up/唤醒链持久化档案路径；None=不持久化（测试）
    ) -> None:
        self._store = store
        self._bubble = bubble_fn
        self._idle = idle_fn
        self._client = client
        self._now = now_fn
        c = cfg or {}
        qh = c.get("quiet_hours", (23, 8))
        if not (isinstance(qh, (list, tuple)) and len(qh) == 2
                and all(0 <= int(h) <= 23 for h in qh)):
            _log.warning("proactive.quiet_hours 非法 %r，用默认 (23,8)", qh)
            qh = (23, 8)
        # L6（REVIEW-2026-09-04）：存整数——旧版校验用 int() 截断却存原 float，
        # _quiet_end_after 的 replace(hour=float) 在深夜重排槽里抛异常
        self._quiet = tuple(int(h) for h in qh)
        self._festivals = {**_FESTIVALS, **(c.get("festivals") or {})}
        self._sedentary_min = float(c.get("sedentary_min", _SEDENTARY_MIN))
        self._cooldown = float(
            c.get("sedentary_cooldown_min", _SEDENTARY_COOLDOWN)
        )
        # v0.7 吃鼠标参数
        self._eat_duration = float(c.get("eat_mouse_duration_s", _EAT_DURATION))
        self._eat_idle_min = float(c.get("idle_threshold_min", _EAT_IDLE_MIN))
        self._dnd_manual = bool(c.get("dnd", False))
        gain = c.get("eat_mouse_gain")
        self._eat_gain = dict(gain) if isinstance(gain, dict) else dict(_EAT_GAIN)

        # v0.7 平台注入
        self._mouse_lock = mouse_lock
        self._dnd_fn = dnd_fn
        self._active_content_fn = active_content_fn
        self._accessibility_fn = accessibility_fn
        self._fsm_event_fn = fsm_event_fn
        self._prompt_accessibility_fn = prompt_accessibility_fn
        self._fullscreen_fn = fullscreen_fn
        self._eat_session = EatMouseSession(
            mouse_lock=mouse_lock, fsm_event_fn=fsm_event_fn
        )

        self._next_wake_at: float | None = None      # 链式唤醒时间戳
        # v0.7.3 两段式吃鼠标：门禁过 → 发 approach 事件 + 记 pending；
        # FSM 到达（EAT_MOUSE action→app 调 eat_mouse_arrived）或超时
        # （eat_mouse_tick 兜底）才真正抑制+气泡+回血
        self._eat_pending: tuple | None = None   # (duration_s, deadline_ts)
        self._eat_hotkey = c.get("eat_mouse_hotkey_label") or (
            "Ctrl+Alt+T" if sys.platform == "win32" else "⌘⌥T"
        )
        self._wake_worker = None                     # 异步决策 worker（v0.6.2）
        self._wake_dying = None                      # shutdown 超时未退的 worker（保引用防 GC）
        # worker 的 QObject 属主：scheduler 非 QObject，给 QThread 挂父对象
        # 让 C++ 生命周期归它管——done 送达时线程可能仍在收尾，Python 引用
        # 先丢会触发"销毁运行中 QThread"的原生崩溃（perm worker 实测段错误）
        self._worker_owner = QObject()
        self._wake_ctx_pending: dict | None = None   # 决策中的 ctx（worker 返回后用）
        self._followups: deque = deque()             # [(when, msg)]（保序，poll 取左端）
        self._sedentary_since: float | None = None   # 本久坐段起点
        self._sedentary_count = 0                    # 本段已发次数（v0.6.2 替代时间除法）
        self._last_sedentary_at: float | None = None
        self._greeted = {"morning": None, "night": None}  # date -> 当天已发
        self._festivaled = None
        self._last_bubble_at: float = 0.0            # 连发气泡间隔限速（v0.6.2）
        # 批次B/P2-6（REVIEW-2026-09-05）：重启状态持久化——旧版全内存，
        # 重启重发早安/晚安/节日、follow-up 全部静默丢失、唤醒链重排。
        self._persist_path = state_path
        self._load_persisted()
        # 启动即排首次唤醒（本地范围随机）；持久化档案有有效的下次唤醒则保留
        if self._next_wake_at is None:
            self.schedule_wake(random.uniform(*_LOCAL_WAKE_RANGE), {"boot": True})

    # ---- 链式唤醒 ----

    def schedule_wake(self, delay_min: float, reason_ctx: dict) -> None:
        """排定下次唤醒。delay 钳制 [10, 360] min；reason_ctx 仅入日志。"""
        delay = min(_WAKE_MIN_MAX, max(_WAKE_MIN_MIN, float(delay_min)))
        self._next_wake_at = self._now() + delay * 60.0
        self._persist()
        _log.info("[主动] 排定唤醒 %.0f 分钟后(ctx=%s)", delay, reason_ctx)

    def follow_up(self, event: str, when: float) -> None:
        """定时回访：when 为时间戳，到点发 event 气泡（深夜也发，用户自己约的）。

        v0.6.2：append 后按时间戳排序，防乱序时间戳致早任务被晚任务阻塞
        （app 当前总排 now+30min 单调，但 API 契约防御）。
        """
        self._followups.append((float(when), event))
        self._followups = deque(sorted(self._followups))
        self._persist()
        _log.info("[主动] follow-up 排定 %s @%s",
                  event, datetime.fromtimestamp(when).strftime("%H:%M"))

    # ---- 批次B/P2-6：重启状态持久化（tmp+replace 原子写） ----

    def _persist(self) -> None:
        if not self._persist_path:
            return
        try:
            data = {
                "version": 1,
                "greeted": {k: (v.isoformat() if isinstance(v, date) else None)
                            for k, v in self._greeted.items()},
                "festivaled": self._festivaled,
                "next_wake_at": self._next_wake_at,
                "followups": [[float(w), str(m)] for w, m in self._followups],
            }
            tmp = self._persist_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._persist_path)
        except OSError:
            _log.warning("[主动] 状态持久化失败", exc_info=True)

    def _load_persisted(self) -> None:
        if not self._persist_path or not os.path.isfile(self._persist_path):
            return
        try:
            with open(self._persist_path, encoding="utf-8") as f:
                raw = json.load(f)
            if not isinstance(raw, dict):
                return
            g = raw.get("greeted")
            if isinstance(g, dict):
                for k, v in g.items():
                    if k in ("morning", "night") and v:
                        try:
                            self._greeted[k] = datetime.fromisoformat(
                                str(v)).date()
                        except ValueError:
                            pass
            fest = raw.get("festivaled")
            if isinstance(fest, str) and fest:
                self._festivaled = fest
            wa = raw.get("next_wake_at")
            # 12h 内的过期唤醒也接受（poll 到点即触发，休眠跨夜场景）；
            # 更陈旧的视为失效重排
            if isinstance(wa, (int, float)) and wa > self._now() - 12 * 3600:
                self._next_wake_at = float(wa)
            fups = raw.get("followups")
            now = self._now()
            if isinstance(fups, list):
                for item in fups[:20]:
                    if (isinstance(item, (list, tuple)) and len(item) == 2
                            and isinstance(item[0], (int, float))
                            and item[0] > now):
                        self._followups.append((float(item[0]), str(item[1])))
                if self._followups:
                    self._followups = deque(sorted(self._followups))
            _log.info("[主动] 恢复持久状态（followup=%d，下次唤醒=%s）",
                      len(self._followups),
                      datetime.fromtimestamp(self._next_wake_at).strftime(
                          "%m-%d %H:%M") if self._next_wake_at else "无")
        except (OSError, ValueError):
            _log.warning("[主动] 状态档案损坏，忽略", exc_info=True)

    def eat_mouse(self, duration_s: float) -> EatMouseSession:
        """v0.7 吃鼠标入口（§2.2 冻结签名）。四门禁全过 → 抑制鼠标 + 切
        EAT_MOUSE 态 + 养成回血；任一门禁不过 → 不抑制（只气泡由上层久坐
        分支已发，此处不叠加，保持 v0.6 久坐提醒行为不变）。

        门禁（铁律）：idle≥idle_threshold_min(5) / 非 DND / 非活跃内容 /
        Accessibility 已授权。无 platform mouse_lock（未注入/测试假件）→
        静默返回，久坐 topic 气泡已发，不抑制不叠加。win 端实际注入
        mouse_lock_win（platform.get_mouse_lock），mac 端注入 MouseLockMac
        ——P3-24（REVIEW-2026-09-05）：旧注释"win/测试无 mouse_lock"为
        v0.7 早期行为，已过时。
        """
        sess = self._eat_session
        # 无平台 mouse_lock → 不抑制，静默（久坐 topic 已气泡）
        if self._mouse_lock is None:
            return sess
        # 铁律5 idle gate：idle<阈值 → 不吃（温和气泡已由上层发）
        try:
            idle_s = self._idle()
        except Exception:
            _log.warning("[吃鼠标] idle_fn 异常，跳过", exc_info=True)
            idle_s = 0.0
        if idle_s < self._eat_idle_min * 60.0:
            return sess
        # 长时间挂机降级（v0.8.2，浸泡测试拍板）：空闲 ≥2h = 人已离开
        # （下班/通宵），吃鼠标退化为纯气泡（久坐 topic 已发），不再抑制
        # ——防夜间每 30min 循环咬鼠标的噪音。真久坐（5min~2h）照常吃。
        if idle_s >= 2 * 3600.0:
            _log.info("[吃鼠标] 空闲≥2h(挂机态)，降级为气泡不抑制")
            return sess
        # 铁律4 DND：专注/勿扰 → 不吃；DND 期间若已在吃 → 吐出
        if self._dnd_active():
            sess.on_dnd_active()
            return sess
        # T8 活跃内容：前台视频 → 不吃
        # L11（REVIEW-2026-09-04）：检测器异常按 fail-closed 不吃——旧版日志
        # 写"放行不抑制"但代码继续走后续门禁可能开吃，言行不一且方向不安全
        if self._active_content_fn is not None:
            try:
                if self._active_content_fn():
                    return sess
            except Exception:
                _log.warning("[吃鼠标] active_content_fn 异常，保守不吃（fail-closed）",
                             exc_info=True)
                return sess
        # 批次C/H2 第五门禁：前台全屏（演示/放映/全屏视频）→ 不吃。
        # 放映中 5min 无输入完全正常（idle 门禁必过），宠物本体虽已隐藏，
        # 吞掉演示者鼠标 10s 是事故级观感——sensor_win 注释自 v0.3 就宣称
        # "全屏禁吃"但从未接线。
        if self._fullscreen_fn is not None:
            try:
                if self._fullscreen_fn():
                    _log.info("[吃鼠标] 前台全屏（演示/播放），不抑制")
                    return sess
            except Exception:
                # L11：同 active_content——fail-closed（演示事故级观感优先）
                _log.warning("[吃鼠标] fullscreen_fn 异常，保守不吃（fail-closed）",
                             exc_info=True)
                return sess
        # T9 Accessibility：未授权 → 提示 + 深链，不抑制
        if self._accessibility_fn is not None:
            try:
                trusted = self._accessibility_fn()
            except Exception:
                _log.warning("[吃鼠标] accessibility_fn 异常，fail-closed 不抑制",
                             exc_info=True)
                trusted = False
            if not trusted:
                # 批次J/L8（REVIEW-2026-08-31）：权限引导每会话至多一次——
                # 旧版每次久坐轮（30min）都弹气泡+深链开系统设置页，用户
                # 离开时被连环打扰
                if not getattr(self, "_accessibility_nagged", False):
                    self._accessibility_nagged = True
                    self._emit_bubble(
                        "需要辅助功能权限，我才能帮你管住鼠标哦～"
                        "（系统设置→隐私与安全→辅助功能）",
                        self._now(),
                    )
                    if self._prompt_accessibility_fn is not None:
                        try:
                            self._prompt_accessibility_fn()
                        except Exception:
                            _log.warning("[吃鼠标] 引导辅助功能设置异常",
                                         exc_info=True)
                return sess
        # 全门禁通过 → 两段式：先直线奔向光标（FSM EAT_APPROACH），
        # 到达（eat_mouse_arrived）或 6s 兜底（eat_mouse_tick）才抑制
        self._eat_pending = (duration_s, self._now() + 6.0)
        if self._fsm_event_fn is not None:
            try:
                self._fsm_event_fn("eat_mouse")
            except Exception:
                _log.warning("[吃鼠标] approach 事件派发异常，直接抑制",
                             exc_info=True)
                self.eat_mouse_arrived()  # 无 FSM → 退化为立即抑制
        else:
            self.eat_mouse_arrived()
        return sess  # M12 修：冻结签名 -> EatMouseSession（旧版隐式返 None）

    def eat_mouse_arrived(self) -> None:
        """FSM 到达光标 → 真正抑制 + 气泡 + 回血（幂等：无 pending no-op）。"""
        if self._eat_pending is None:
            return
        # 批次C/H2：到达复查——门禁通过后 ≤6s 追赶窗口内可能切入全屏
        # （用户按 F5 开讲），此时仍抑制就正吞在演示者头上
        if self._fullscreen_fn is not None:
            try:
                if self._fullscreen_fn():
                    _log.info("[吃鼠标] 追赶途中前台转全屏，放弃抑制")
                    self._eat_pending = None
                    if self._fsm_event_fn is not None:
                        self._fsm_event_fn("eat_mouse_off")
                    return
            except Exception:
                # 批次D/E3（REVIEW-2026-09-05）：fail-closed 对齐 L11——
                # 旧版异常"放行（继续抑制）"与入口三处门禁语义相反；演示
                # 中途异常吞掉演示者鼠标是事故级观感，保守放弃抑制
                _log.warning("[吃鼠标] 到达复查 fullscreen_fn 异常，保守放弃抑制",
                             exc_info=True)
                self._eat_pending = None
                if self._fsm_event_fn is not None:
                    try:
                        self._fsm_event_fn("eat_mouse_off")
                    except Exception:
                        _log.warning("[吃鼠标] 到达复查退出事件派发异常",
                                     exc_info=True)
                return
        duration_s, _dl = self._eat_pending
        self._eat_pending = None
        sess = self._eat_session
        sess.start(duration_s)
        if sess.active:
            self._emit_bubble(
                "帮你管住小鼠标休息一下～吐出按 "
                + self._eat_hotkey + " 或点托盘", self._now()
            )
            if self._eat_gain:
                try:
                    self._store.update(**self._eat_gain)
                except Exception:
                    _log.warning("[吃鼠标] 养成回血异常", exc_info=True)
        else:
            # tap 创建失败（权限被拒/UIPI 系统拒绝）→ 不抑制，提示。
            # M4 修（REVIEW-2026-08-25）：补发 eat_mouse_off 让 FSM 退出
            # EAT_MOUSE——旧版只发气泡，_was_active 未置真致 sync_release
            # 的释放检测永不触发，宠物冻在光标处咀嚼，仅热键/托盘可解
            if self._fsm_event_fn is not None:
                try:
                    self._fsm_event_fn("eat_mouse_off")
                except Exception:
                    _log.warning("[吃鼠标] 抑制失败回退事件派发异常",
                                 exc_info=True)
            self._emit_bubble("没管住鼠标（权限或系统拒绝），先气泡提醒～",
                              self._now())

    def force_spit(self) -> None:
        """强制吐出（托盘菜单调；热键/shutdown 经 mouse_lock/EatMouseSession
        同样路径）。无 mouse_lock → 无操作。含清 approach pending。"""
        self._eat_pending = None  # 追赶中吐出：不再启动抑制
        self._eat_session.force_spit()

    def shutdown(self) -> None:
        """M6 修（REVIEW-2026-08-25）：收口在飞的决策 worker。

        app.shutdown 七步序调用。旧版退出不停 ``_wake_worker``（最长挂
        120s read 超时），QThread 对象随 scheduler 被 GC 会触发
        "QThread: Destroyed while thread is still running" 崩溃。断信号
        → cancel 中断流式 → wait 退出。deleteLater 由 finished→deleteLater
        连接兜底，此处不手删（wait 超时线程未退时强删即同款崩溃）。
        """
        w = self._wake_worker
        if w is None:
            return
        try:
            w.done.disconnect(self._on_wake_done)
            w.failed.disconnect(self._on_wake_failed)
        except (TypeError, RuntimeError):
            pass
        if w.isRunning():
            w.cancel()
            if not w.wait(2000):
                # 线程仍卡（罕见：connect 阶段最长 10s）——保留引用防
                # QThread wrapper 被 GC，finished→deleteLater 兜底回收
                self._wake_dying = w
                return
        self._wake_worker = None

    def eat_mouse_tick(self) -> None:
        """app 每 tick 调（廉价）：approach 超时兜底 → 到点强制开吃。

        用户持续移动光标致 FSM 5s 放弃，或 FSM 事件链路异常时，
        pending 6s deadline 到 → eat_mouse_arrived（FSM 的 _EAT_MOUSE
        态冻结在当前位置，视觉即"在光标处吃"）。
        """
        # 释放检测（恒执行：自动释放后 FSM 回 idle 坠落）
        # L11：裸吞改留痕——释放检测链路坏了无迹可查
        try:
            self._eat_session.sync_release()
        except Exception:
            _log.warning("[吃鼠标] sync_release 异常", exc_info=True)
        # 批次B/P2-5（REVIEW-2026-09-05）：会话中途 DND 生效 → 立即吐出。
        # 旧版 on_dnd_active 只有 eat_mouse 入口门禁一处调用点，勿扰在吃
        # 鼠标中途开启（config 手动开关翻转/未来系统级检测）永远不会打断
        # 进行中的抑制——铁律4"勿扰不吃"只挡入口不断中途。未在吃时
        # on_dnd_active 是 no-op（返 False）。
        try:
            if self._dnd_active():
                self._eat_session.on_dnd_active()
        except Exception:
            _log.warning("[吃鼠标] DND 中途复查异常", exc_info=True)
        if self._eat_pending is None:
            return
        if self._now() >= self._eat_pending[1]:
            _log.warning("[吃鼠标] approach 超时兜底，就地开吃")
            self.eat_mouse_arrived()

    def _dnd_active(self) -> bool:
        """勿扰/专注模式是否生效（铁律4）。config ``proactive.dnd`` 手动
        开关 + 可选系统检测 ``dnd_fn``；任一为真 → DND。"""
        if self._dnd_manual:
            return True
        if self._dnd_fn is not None:
            try:
                return bool(self._dnd_fn())
            except Exception:
                # L11：DND 检测异常按 DND 处理（fail-closed，铁律4 勿扰不吃）
                _log.warning("[吃鼠标] dnd_fn 异常，按 DND 处理（fail-closed）",
                             exc_info=True)
                return True
        return False

    # ---- 深夜判定 ----

    def _quiet_now(self, now: float) -> bool:
        h = datetime.fromtimestamp(now).hour
        start, end = self._quiet
        if start > end:  # 跨午夜（如 23→08）
            return h >= start or h < end
        return start <= h < end

    # ---- 主循环（app QTimer 驱动；测试直接传 now） ----

    def poll(self, now: float | None = None) -> None:
        now = self._now() if now is None else now

        # 1) follow-up（最高优先级；深夜照发——用户自己约的）
        try:
            while self._followups and self._followups[0][0] <= now:
                _, msg = self._followups.popleft()
                self._emit_bubble(msg, now)
                _log.info("[主动] follow-up 触发: %s", msg)
        except Exception:
            _log.warning("[主动] follow-up 处理异常", exc_info=True)

        quiet = self._quiet_now(now)

        # 2) 链式唤醒（v0.6.2：深夜重排到 quiet 结束，非置 None 丢弃致链断）
        if self._next_wake_at is not None and now >= self._next_wake_at:
            self._next_wake_at = None
            if quiet:
                # 重排到 quiet 结束时刻（次日 end 点），链条不断
                next_t = self._quiet_end_after(now)
                self._next_wake_at = next_t
                _log.info("[主动] 深夜唤醒顺延到 %s",
                          datetime.fromtimestamp(next_t).strftime("%m-%d %H:%M"))
            else:
                try:
                    self._fire_wake(now)
                except Exception:
                    # 批次C/P3-14（REVIEW-2026-09-05）：_fire_wake 异常兜底
                    # 重排——旧版异常冒出 poll（无 try）跳过后续段且链断到
                    # 重启
                    _log.warning("[主动] _fire_wake 异常，兜底重排唤醒链",
                                 exc_info=True)
                    self.schedule_wake(random.uniform(*_LOCAL_WAKE_RANGE),
                                       {"recover": True})

        if quiet:
            return  # 深夜其余全静默

        # 3) 久坐提醒（空闲 ≥ 阈值）
        try:
            idle_s = self._idle()
            idle_min = idle_s / 60.0
        except Exception:
            _log.warning("[主动] idle_fn 异常，久坐检测跳过本轮", exc_info=True)
            idle_min = 0.0
        if idle_min >= self._sedentary_min:
            if self._sedentary_since is None:
                self._sedentary_since = now
                self._sedentary_count = 0
            if (self._last_sedentary_at is None
                    or now - self._last_sedentary_at >= self._cooldown * 60):
                # v0.6.2：用计数器轮换话题（旧版时间除法跨静默期/漏 poll 跳变）
                msg = _SEDENTARY_TOPICS[
                    self._sedentary_count % len(_SEDENTARY_TOPICS)
                ]
                self._sedentary_count += 1
                self._emit_bubble(msg, now)
                _log.info("[主动] 久坐提醒(空闲%.0fmin): %s", idle_min, msg)
                self._last_sedentary_at = now
                # v0.7：休息提醒升级为吃鼠标（§七「把休息提醒+整蛊+养成缝成
                # 一件」）。eat_mouse 内部四门禁（idle/DND/活跃内容/
                # accessibility）+ mouse_lock 抑制；平台未注入 mouse_lock
                # （测试假件）时静默，topic 气泡已发不叠加，保持 v0.6 行为。
                # （P3-24：win 端实际有 mouse_lock_win，旧注释过时）
                self.eat_mouse(self._eat_duration)
        else:
            self._sedentary_since = None
            self._sedentary_count = 0
            self._last_sedentary_at = None

        d = datetime.fromtimestamp(now)
        today = d.date()
        # N12：时钟回拨守护—— greeted/festivaled 记的是 date，回拨跨天会重发；
        # 检测 now 倒退则不重置 greeted（保留未来日期标记，防回拨重发）
        # （简单实现：greeted 已按 date 去重，回拨到昨天会 != today 触发重发，
        # 此处接受该限制，因回拨罕见且影响轻微）

        # 4) 早安/晚安（每天一次，窗口时段）
        if _MORNING_HOURS[0] <= d.hour < _MORNING_HOURS[1] \
                and self._greeted["morning"] != today:
            self._greeted["morning"] = today
            self._persist()  # 批次B/P2-6：重启不重发
            self._emit_bubble("早上好呀～新的一天元气满满！", now)
            _log.info("[主动] 早安")
        if _NIGHT_HOURS[0] <= d.hour < _NIGHT_HOURS[1] \
                and self._greeted["night"] != today:
            self._greeted["night"] = today
            self._persist()
            self._emit_bubble("夜深了，早点休息哦～", now)
            _log.info("[主动] 晚安")

        # 5) 节日（每天一次）
        md = d.strftime("%m-%d")
        if md in self._festivals and self._festivaled != today:
            self._festivaled = today
            self._persist()
            self._emit_bubble(f"{self._festivals[md]}快乐！🎉", now)
            _log.info("[主动] 节日: %s", self._festivals[md])

    def _emit_bubble(self, msg: str, now: float) -> None:
        """发气泡，带连发间隔限速（v0.6.2：防单次 poll 连发多条后者覆盖前者）。"""
        try:
            # 连发限速：距上次气泡 <2s 则延后不阻塞（简单实现：直接发，间隔
            # 由调用方自然错开；真队列化留后续）。当前仅记录时间戳供未来用。
            self._bubble(msg)
            self._last_bubble_at = now
        except Exception:
            _log.warning("[主动] bubble_fn 异常", exc_info=True)

    def _quiet_end_after(self, now: float) -> float:
        """计算 now 之后的 quiet 结束时刻（次日 end 点）。用于深夜唤醒重排。"""
        d = datetime.fromtimestamp(now)
        end_h = self._quiet[1]
        # 今天的 end 点；若已过则次日
        end_today = d.replace(hour=end_h, minute=0, second=0, microsecond=0)
        if end_today.timestamp() <= now:
            end_today = end_today + timedelta(days=1)  # M5 修：replace 不进位，月末/年末 ValueError
        return end_today.timestamp()

    # ---- 唤醒决策（LLM 隔离上下文 / 本地罐头兜底） ----

    def _fire_wake(self, now: float) -> None:
        """触发唤醒：异步调 LLM 决策（v0.6.2 异步化，主线程不阻塞）。

        无 client 时直接本地罐头。有 client 启 ProactiveWorker 跑 chat_once，
        done 信号回主线程 _on_wake_done 发气泡+排下一次。
        """
        try:
            state = self._store.get()
        except Exception:
            _log.warning("[主动] store.get 异常，用默认状态", exc_info=True)
            state = None
        ctx = {
            "hour": datetime.fromtimestamp(now).strftime("%H:%M"),
            "mood": round(state.mood) if state else 80,
            "fullness": round(state.fullness) if state else 80,
            "stage": state.stage.value if state else "young",
        }
        if self._client is None:
            # 无 client：本地罐头（同步，无阻塞）
            delay_min, msg = self._local_canned()
            self._emit_bubble(msg, now)
            _log.info("[主动] 唤醒(本地罐头): %s → 下次 %.0fmin", ctx, delay_min)
            self.schedule_wake(delay_min, {"chain": True})
            return
        # 有 client：异步化（旧版同步 chat_once 阻塞主线程最长 120s）
        if self._wake_worker is not None:
            # 批次C/P3-14（REVIEW-2026-09-05）：顺延而非丢弃——旧版直接
            # return 且 _next_wake_at 已置 None，worker 万一卡死/完成路径
            # 异常则唤醒链断到重启（正常路径 worker done 会重排）
            _log.info("[主动] 上一轮决策 worker 仍在跑，本次唤醒顺延 10 分钟")
            self.schedule_wake(10, {"chain": True})
            return
        self._wake_ctx_pending = ctx
        # 批次H/M10（REVIEW-2026-08-31 F32）：worker 与测试共用同一条
        # ``_decide``（请求+解析+罐头回退全在里面）——旧版 worker 内联
        # chat_once、slot 里再解析，生产异步链与测试同步链双轨漂移
        self._wake_worker = _ProactiveWorker(self._client, ctx,
                                             parent=self._worker_owner,
                                             decide_fn=self._decide)
        self._wake_worker.done.connect(self._on_wake_done)
        self._wake_worker.failed.connect(self._on_wake_failed)
        # 删除只走 finished→deleteLater 单通道（parent 管生存期，见 __init__）
        self._wake_worker.finished.connect(self._wake_worker.deleteLater)
        self._wake_worker.start()

    @Slot(float, str, object)
    def _on_wake_done(self, delay_min: float, msg: str, ctx: object) -> None:
        """worker done：_decide 已含解析+罐头回退，这里只发气泡+排下一次。"""
        self._wake_worker = None
        # 批次C/P3-14（REVIEW-2026-09-05）：决策完成时已入安静时段 → 气泡
        # 顺延到安静结束（旧版 poll 侧深夜才挡，worker 慢一步就漏进来）
        if self._quiet_now(self._now()):
            next_t = self._quiet_end_after(self._now())
            self._next_wake_at = next_t
            self._persist()
            _log.info("[主动] 决策完成恰入安静时段，气泡顺延 %s",
                      datetime.fromtimestamp(next_t).strftime("%m-%d %H:%M"))
            return
        self._emit_bubble(msg, self._now())
        _log.info("[主动] 唤醒(决策): %s → 下次 %.0fmin", ctx, delay_min)
        self.schedule_wake(delay_min, {"chain": True})

    @Slot(object)
    def _on_wake_failed(self, ctx: object) -> None:
        """worker failed（超时/离线/异常）：退本地罐头。"""
        self._wake_worker = None
        if self._quiet_now(self._now()):  # 批次C/P3-14：同 done 顺延
            next_t = self._quiet_end_after(self._now())
            self._next_wake_at = next_t
            self._persist()
            return
        d, m = self._local_canned()
        self._emit_bubble(m, self._now())
        _log.info("[主动] 唤醒(LLM失败退本地): %s → 下次 %.0fmin", ctx, d)
        self.schedule_wake(d, {"chain": True})

    def _decide(self, ctx: dict) -> tuple[float, str]:
        """决策唯一实现（生产异步 worker 与测试共用，批次H/M10 收口双轨）。

        v0.6.2：chat_once 加 system_override=_DECISION_SYSTEM + tools_override=None
        （隔离人设 + 不挂工具，治 ctx=None 崩 + 人设冲突）；JSON 剥离 ```json```
        围栏容错；message 非 str 或空走罐头（N8，防显示"None"）。
        """
        if self._client is not None:
            try:
                from .llm import ChatTurn

                history = [ChatTurn(
                    role="user",
                    content="当前状态：" + json.dumps(ctx, ensure_ascii=False),
                )]
                # N4/N5：system_override 替换人设为决策指令；tools_override=None
                # 不挂工具（DS 不返 tool_calls，治 ctx=None 崩 + 人设冲突）
                text, _turns = self._client.chat_once(
                    history, None,
                    system_override=_DECISION_SYSTEM,
                    tools_override=None,
                )
                return self._parse_decision(text)
            except Exception as exc:
                _log.warning("[主动] LLM 决策失败退本地: %s", exc)
        return self._local_canned()

    def _parse_decision(self, text: str) -> tuple[float, str]:
        """解析 LLM 决策 JSON（容错 ```json``` 围栏）；失败 raise → 调用方走罐头。"""
        # N6：剥离 ```json``` 围栏 + 正则提取首个 {} 容错
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            raise ValueError("决策输出无 JSON 对象")
        data = json.loads(m.group(0))
        msg_raw = data.get("message", "")
        # N8：message 非 str 或空 → 走罐头（防显示 "None"/"123"/"['a']"）
        if not isinstance(msg_raw, str) or not msg_raw.strip():
            raise ValueError("决策 message 非字符串或为空")
        msg = msg_raw[:40].strip()
        delay = float(data.get("next_min", 60))
        return (min(_WAKE_MIN_MAX, max(_WAKE_MIN_MIN, delay)), msg)

    def _local_canned(self) -> tuple[float, str]:
        """本地罐头兜底。"""
        canned = (
            "在忙什么呢？记得照顾好自己呀～",
            "我一直在你桌面上陪着哦～",
            "抬起头看看远处吧，眼睛也要休息～",
        )
        return (random.uniform(*_LOCAL_WAKE_RANGE), random.choice(canned))


class _ProactiveWorker(QThread):
    """proactive 决策后台线程（v0.6.2）——包裹决策，主线程不阻塞。

    批次H/M10（REVIEW-2026-08-31 F32）：决策本体 = 注入的 ``decide_fn``
    （生产即 ``ProactiveScheduler._decide``，测试同路径）——旧版此处内联
    chat_once、解析在主线程 slot，生产/测试两套代码零交集。

    信号：``done(delay_min, msg, ctx)``（_decide 已含解析+罐头回退）/
    ``failed(ctx)``（decide_fn 自身崩溃的兜底，正常不会发生）。
    """

    done = Signal(float, str, object)
    failed = Signal(object)

    def __init__(self, client, ctx: dict, parent=None, decide_fn=None) -> None:
        super().__init__(parent)
        self._client = client
        self._ctx = ctx
        self._decide_fn = decide_fn

    def cancel(self) -> None:
        """中断在飞请求（shutdown 用）：关底层流式 socket——iter_lines 抛
        ConnectionError→OfflineError→_decide 的 except→退罐头，线程随即
        退出，wait 不必吃满 read 超时。"""
        try:
            if self._client._resp is not None:
                self._client._resp.close()
        except Exception:
            pass

    @Slot()
    def run(self) -> None:
        try:
            delay_min, msg = self._decide_fn(self._ctx)
            self.done.emit(float(delay_min), str(msg), self._ctx)
        except Exception as exc:
            _log.warning("[主动] 异步决策异常: %s", exc)
            self.failed.emit(self._ctx)
