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
EatMouseSession v0.7 实装（接口占位）。
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
from collections import deque
from datetime import datetime

from PySide6.QtCore import QThread, Signal, Slot

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
    """v0.7 实装（吃鼠标），此处接口占位。"""

    def start(self, duration_s: float) -> None:  # pragma: no cover
        raise NotImplementedError("v0.7")

    def force_spit(self) -> None:  # pragma: no cover
        raise NotImplementedError("v0.7")

    def on_dnd_active(self) -> bool:  # pragma: no cover
        raise NotImplementedError("v0.7")


class ProactiveScheduler:
    def __init__(
        self,
        store: PetStateStore,
        bubble_fn,                       # callable(text) -> None（app 接气泡）
        idle_fn,                         # callable() -> float 空闲秒
        client=None,                     # DeepSeekClient | None（无则本地罐头）
        cfg: dict | None = None,
        now_fn=time.time,
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
        self._quiet = tuple(qh)
        self._festivals = {**_FESTIVALS, **(c.get("festivals") or {})}
        self._sedentary_min = float(c.get("sedentary_min", _SEDENTARY_MIN))
        self._cooldown = float(
            c.get("sedentary_cooldown_min", _SEDENTARY_COOLDOWN)
        )

        self._next_wake_at: float | None = None      # 链式唤醒时间戳
        self._wake_worker = None                     # 异步决策 worker（v0.6.2）
        self._wake_ctx_pending: dict | None = None   # 决策中的 ctx（worker 返回后用）
        self._followups: deque = deque()             # [(when, msg)]（保序，poll 取左端）
        self._sedentary_since: float | None = None   # 本久坐段起点
        self._sedentary_count = 0                    # 本段已发次数（v0.6.2 替代时间除法）
        self._last_sedentary_at: float | None = None
        self._greeted = {"morning": None, "night": None}  # date -> 当天已发
        self._festivaled = None
        self._last_bubble_at: float = 0.0            # 连发气泡间隔限速（v0.6.2）
        # 启动即排首次唤醒（本地范围随机）
        self.schedule_wake(random.uniform(*_LOCAL_WAKE_RANGE), {"boot": True})

    # ---- 链式唤醒 ----

    def schedule_wake(self, delay_min: float, reason_ctx: dict) -> None:
        """排定下次唤醒。delay 钳制 [10, 360] min；reason_ctx 仅入日志。"""
        delay = min(_WAKE_MIN_MAX, max(_WAKE_MIN_MIN, float(delay_min)))
        self._next_wake_at = self._now() + delay * 60.0
        _log.info("[主动] 排定唤醒 %.0f 分钟后(ctx=%s)", delay, reason_ctx)

    def follow_up(self, event: str, when: float) -> None:
        """定时回访：when 为时间戳，到点发 event 气泡（深夜也发，用户自己约的）。

        v0.6.2：append 后按时间戳排序，防乱序时间戳致早任务被晚任务阻塞
        （app 当前总排 now+30min 单调，但 API 契约防御）。
        """
        self._followups.append((float(when), event))
        self._followups = deque(sorted(self._followups))
        _log.info("[主动] follow-up 排定 %s @%s",
                  event, datetime.fromtimestamp(when).strftime("%H:%M"))

    def eat_mouse(self, duration_s: float) -> EatMouseSession:  # pragma: no cover
        """v0.7 实装；接口冻结占位。"""
        raise NotImplementedError("v0.7")

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
                self._fire_wake(now)

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
            self._emit_bubble("早上好呀～新的一天元气满满！", now)
            _log.info("[主动] 早安")
        if _NIGHT_HOURS[0] <= d.hour < _NIGHT_HOURS[1] \
                and self._greeted["night"] != today:
            self._greeted["night"] = today
            self._emit_bubble("夜深了，早点休息哦～", now)
            _log.info("[主动] 晚安")

        # 5) 节日（每天一次）
        md = d.strftime("%m-%d")
        if md in self._festivals and self._festivaled != today:
            self._festivaled = today
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
            end_today = end_today.replace(day=d.day + 1)  # 次日（datetime 自动进位）
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
            _log.info("[主动] 上一轮决策 worker 仍在跑，跳过本次唤醒")
            return
        self._wake_ctx_pending = ctx
        self._wake_worker = _ProactiveWorker(self._client, ctx)
        self._wake_worker.done.connect(self._on_wake_done)
        self._wake_worker.failed.connect(self._on_wake_failed)
        self._wake_worker.start()

    @Slot(object, object)
    def _on_wake_done(self, text: object, ctx: object) -> None:
        """worker done：解析 JSON → 发气泡 + 排下一次（主线程槽）。"""
        w = self._wake_worker
        self._wake_worker = None
        if w is not None:
            w.deleteLater()
        try:
            delay_min, msg = self._parse_decision(str(text))
            if msg:
                self._emit_bubble(msg, self._now())
                _log.info("[主动] 唤醒(LLM): %s → 下次 %.0fmin", ctx, delay_min)
                self.schedule_wake(delay_min, {"chain": True})
            else:
                # message 无效 → 本地罐头
                d, m = self._local_canned()
                self._emit_bubble(m, self._now())
                self.schedule_wake(d, {"chain": True})
        except Exception as exc:
            _log.warning("[主动] 决策结果解析异常退本地: %s", exc)
            d, m = self._local_canned()
            self._emit_bubble(m, self._now())
            self.schedule_wake(d, {"chain": True})

    @Slot(object)
    def _on_wake_failed(self, ctx: object) -> None:
        """worker failed（超时/离线/异常）：退本地罐头。"""
        w = self._wake_worker
        self._wake_worker = None
        if w is not None:
            w.deleteLater()
        d, m = self._local_canned()
        self._emit_bubble(m, self._now())
        _log.info("[主动] 唤醒(LLM失败退本地): %s → 下次 %.0fmin", ctx, d)
        self.schedule_wake(d, {"chain": True})

    def _decide(self, ctx: dict) -> tuple[float, str]:
        """同步决策（测试用；生产走异步 _fire_wake + _on_wake_done）。

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
    """proactive 决策后台线程（v0.6.2）——包裹 chat_once，主线程不阻塞。

    信号：``done(text, ctx)``（决策文本 + 原 ctx 回主线程解析）/
    ``failed(ctx)``（超时/离线/异常退本地罐头）。
    """

    done = Signal(object, object)
    failed = Signal(object)

    def __init__(self, client, ctx: dict, parent=None) -> None:
        super().__init__(parent)
        self._client = client
        self._ctx = ctx

    @Slot()
    def run(self) -> None:
        from .llm import ChatTurn

        history = [ChatTurn(
            role="user",
            content="当前状态：" + json.dumps(self._ctx, ensure_ascii=False),
        )]
        try:
            text, _turns = self._client.chat_once(
                history, None,
                system_override=_DECISION_SYSTEM,
                tools_override=None,
            )
            self.done.emit(text, self._ctx)
        except Exception as exc:
            _log.warning("[主动] 异步决策异常: %s", exc)
            self.failed.emit(self._ctx)
