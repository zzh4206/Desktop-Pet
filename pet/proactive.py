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

平台库-free：时钟/气泡/LLM 全部注入（`now_fn`/`bubble_fn`/`client`），
app 用 QTimer 驱动 `poll()`；测试用假时钟直接调 `poll(now)`。
EatMouseSession v0.7 实装（接口占位）。
"""

from __future__ import annotations

import json
import logging
import random
import time
from collections import deque
from datetime import datetime

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
        self._quiet = tuple(c.get("quiet_hours", (23, 8)))  # (start_h, end_h)
        self._festivals = {**_FESTIVALS, **(c.get("festivals") or {})}
        self._sedentary_min = float(c.get("sedentary_min", _SEDENTARY_MIN))
        self._cooldown = float(
            c.get("sedentary_cooldown_min", _SEDENTARY_COOLDOWN)
        )

        self._next_wake_at: float | None = None      # 链式唤醒时间戳
        self._followups: deque = deque()             # [(when, msg)]
        self._sedentary_since: float | None = None   # 本久坐段起点
        self._last_sedentary_at: float | None = None
        self._greeted = {"morning": None, "night": None}  # date -> 当天已发
        self._festivaled = None
        # 启动即排首次唤醒（本地范围随机）
        self.schedule_wake(random.uniform(*_LOCAL_WAKE_RANGE), {"boot": True})

    # ---- 链式唤醒 ----

    def schedule_wake(self, delay_min: float, reason_ctx: dict) -> None:
        """排定下次唤醒。delay 钳制 [10, 360] min；reason_ctx 仅入日志。"""
        delay = min(_WAKE_MIN_MAX, max(_WAKE_MIN_MIN, float(delay_min)))
        self._next_wake_at = self._now() + delay * 60.0
        _log.info("[主动] 排定唤醒 %.0f 分钟后(ctx=%s)", delay, reason_ctx)

    def follow_up(self, event: str, when: float) -> None:
        """定时回访：when 为时间戳，到点发 event 气泡（深夜也发，用户自己约的）。"""
        self._followups.append((float(when), event))
        _log.info("[主动] follow-up 排定 %s @%s",
                  event, datetime.fromtimestamp(when).strftime("%H:%M"))

    def eat_mouse(self, duration_s: float) -> EatMouseSession:  # pragma: no cover
        """v0.7 实装；接口冻结占位。"""
        raise NotImplementedError("v0.7")

    # ---- 深夜判定 ----

    def _quiet_now(self, now: float) -> bool:
        h = datetime.fromtimestamp(now).hour
        start, end = self._quiet
        return start <= 24 and (h >= start or h < end) if start > end \
            else (start <= h < end)

    # ---- 主循环（app QTimer 驱动；测试直接传 now） ----

    def poll(self, now: float | None = None) -> None:
        now = self._now() if now is None else now

        # 1) follow-up（最高优先级；深夜照发——用户自己约的）
        while self._followups and self._followups[0][0] <= now:
            _, msg = self._followups.popleft()
            self._bubble(msg)
            _log.info("[主动] follow-up 触发: %s", msg)

        quiet = self._quiet_now(now)

        # 2) 链式唤醒（深夜顺延到安静期结束，不丢）
        if self._next_wake_at is not None and now >= self._next_wake_at:
            self._next_wake_at = None
            if quiet:
                _log.info("[主动] 深夜，唤醒顺延")
            else:
                self._fire_wake(now)

        if quiet:
            return  # 深夜其余全静默

        # 3) 久坐提醒（空闲 ≥ 阈值）
        idle_min = self._idle() / 60.0
        if idle_min >= self._sedentary_min:
            if self._sedentary_since is None:
                self._sedentary_since = now
            if (self._last_sedentary_at is None
                    or now - self._last_sedentary_at >= self._cooldown * 60):
                # 轮换：按本段进入的冷却周期数推进话题
                idx = int((now - self._sedentary_since)
                          // (self._cooldown * 60)) % len(_SEDENTARY_TOPICS)
                msg = _SEDENTARY_TOPICS[idx]
                self._bubble(msg)
                _log.info("[主动] 久坐提醒(空闲%.0fmin): %s", idle_min, msg)
                self._last_sedentary_at = now
        else:
            self._sedentary_since = None
            self._last_sedentary_at = None

        d = datetime.fromtimestamp(now)
        today = d.date()

        # 4) 早安/晚安（每天一次，窗口时段）
        if _MORNING_HOURS[0] <= d.hour < _MORNING_HOURS[1] \
                and self._greeted["morning"] != today:
            self._greeted["morning"] = today
            self._bubble("早上好呀～新的一天元气满满！")
            _log.info("[主动] 早安")
        if _NIGHT_HOURS[0] <= d.hour < _NIGHT_HOURS[1] \
                and self._greeted["night"] != today:
            self._greeted["night"] = today
            self._bubble("夜深了，早点休息哦～")
            _log.info("[主动] 晚安")

        # 5) 节日（每天一次）
        md = d.strftime("%m-%d")
        if md in self._festivals and self._festivaled != today:
            self._festivaled = today
            self._bubble(f"{self._festivals[md]}快乐！🎉")
            _log.info("[主动] 节日: %s", self._festivals[md])

    # ---- 唤醒决策（LLM 隔离上下文 / 本地罐头兜底） ----

    def _fire_wake(self, now: float) -> None:
        state = self._store.get()
        ctx = {
            "hour": datetime.fromtimestamp(now).strftime("%H:%M"),
            "mood": round(state.mood), "fullness": round(state.fullness),
            "stage": state.stage.value,
        }
        delay_min, msg = self._decide(ctx)
        self._bubble(msg)
        _log.info("[主动] 唤醒(隔离上下文): %s → 下次 %.0fmin", ctx, delay_min)
        self.schedule_wake(delay_min, {"chain": True})

    def _decide(self, ctx: dict) -> tuple[float, str]:
        """LLM 隔离决策（独立 history，不掺聊天上下文）；失败/无客户端 →
        本地罐头。决策指令并入 user 轮（client.chat_once 用宠物人设 system，
        独立 history 即"隔离上下文"，日志可验）。"""
        if self._client is not None:
            try:
                from .llm import ChatTurn

                history = [ChatTurn(
                    role="user",
                    content=_DECISION_SYSTEM + "\n当前状态："
                            + json.dumps(ctx, ensure_ascii=False),
                )]
                text, _turns = self._client.chat_once(history, None)
                data = json.loads(text)
                msg = str(data.get("message", ""))[:40].strip()
                delay = float(data.get("next_min", 60))
                if msg:
                    return (min(_WAKE_MIN_MAX, max(_WAKE_MIN_MIN, delay)), msg)
            except Exception as exc:
                _log.warning("[主动] LLM 决策失败退本地: %s", exc)
        canned = (
            "在忙什么呢？记得照顾好自己呀～",
            "我一直在你桌面上陪着哦～",
            "抬起头看看远处吧，眼睛也要休息～",
        )
        return (random.uniform(*_LOCAL_WAKE_RANGE), random.choice(canned))
