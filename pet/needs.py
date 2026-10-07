"""养成机制引擎（v0.19.8）——数值系统规则单一出处。

用户实测反馈（2026-10-07）：心情/清洁/饱食度机制"不成系统"。此前规则散在
四处互不通气：衰减率 config.decay_per_hour、交互收益 config.interaction_gain、
触线阈值 proactive.need_bubble（app._need_thresholds/托盘/菜单 ⚠ 各自消费）、
状态措辞 app._status_rows 与 tray 各写各的；且三数值是三条互不相干的直线
（只各自单调衰减），没有养成系统该有的联动。

本模块收口（PetStateStore 仍是数值的唯一真相源与持久化层，本引擎只提供
规则层）：

- 区段 zones：三数值的分段命名（饿透了/有点饿/…），状态板/托盘/状态行
  共用同一措辞，替代各处手搓的 "value<20 → warn" 魔数；
- 联动 interlock：饿肚子/脏兮兮给心情**加压**（额外衰减），吃饱+干净给
  心情**抵扣**衰减（默认抵满=停摆）。联动只做加压与抵扣、不做自动回升——
  心情的回升永远来自交互，避免挂机白嫖，也绕开 store.apply_decay 的
  非负速率守卫（负速率会被静默丢弃）；
- 衰减 tick：base decay + 联动修正 → store.apply_decay（wall-clock 语义
  原样保留，离线补衰减自动继承）；
- 交互应用 interact：决策（interaction.decide_interaction 纯函数）+ 数值
  落账 + 疲劳记账收口于此，app._interact 只做四通道反馈装配。

纯 Python 无 Qt，规则全部可由 config.needs 覆盖。
"""

from __future__ import annotations

import logging
from typing import Callable

from .interaction import (INTERACT_FIELD_LABEL, InteractionOutcome,
                          decide_interaction)
from .pet_state import PetState, PetStateStore

log = logging.getLogger("pet")

# 数值字段（与 pet_state._NUMERIC_FIELDS 一致；不 import 私有名）
NEED_FIELDS = ("mood", "fullness", "cleanliness")

# 托盘/状态行固定顺序（饱食→心情→清洁，沿用 0.19.2 F9 措辞顺序）
LINE_ORDER = ("fullness", "mood", "cleanliness")

# ---- 区段表：value 从低到高，(上限, 名称)；value≥末档上限取末名 ----
# 阈值与既有观感锚点对齐：mood 20/50 与 mood_bucket 一致，饱食/清洁 20
# （状态板旧 warn 线）并入区段，35/65 为新增中间档。
ZONE_TABLE: dict[str, tuple[tuple[float, str], ...]] = {
    "fullness": ((15.0, "饿透了"), (35.0, "有点饿"), (65.0, "不饿"),
                 (100.0, "吃饱")),
    "cleanliness": ((15.0, "脏透了"), (35.0, "有点脏"), (100.0, "干净")),
    "mood": ((20.0, "低落"), (50.0, "平静"), (75.0, "开心"), (100.0, "雀跃")),
}

# ---- 联动系数（点/小时；config.needs 同名键覆盖） ----
DEFAULT_INTERLOCK: dict[str, float] = {
    "hunger_mood_drag": 1.5,    # fullness < drag_fullness → 心情额外衰减
    "dirt_mood_drag": 1.0,      # cleanliness < drag_cleanliness → 同上
    "content_mood_gain": 2.0,   # 双高时抵扣心情衰减（≥基础衰减 2.0 即停摆）
    "drag_fullness": 30.0,
    "drag_cleanliness": 25.0,
    "content_fullness": 80.0,
    "content_cleanliness": 65.0,
}

_INTERLOCK_KEYS = tuple(DEFAULT_INTERLOCK)
_INTERLOCK_MAX = {  # schema 同界（config._SECTION_SCHEMAS.needs）
    "hunger_mood_drag": 50.0, "dirt_mood_drag": 50.0,
    "content_mood_gain": 50.0, "drag_fullness": 100.0,
    "drag_cleanliness": 100.0, "content_fullness": 100.0,
    "content_cleanliness": 100.0,
}


def zone(field: str, value: float) -> str:
    """单数值 → 区段名。未知字段/非法值回退空串（调用方兜底）。"""
    table = ZONE_TABLE.get(field)
    if not table:
        return ""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return table[-1][1]
    for hi, label in table:
        if v < hi:
            return label
    return table[-1][1]


def status_line(state, thresholds: dict) -> tuple[str, bool]:
    """托盘状态行（0.19.2 F9 同格式）：`饱食28⚠ 心情65 清洁90` + 是否触线。

    返回 (line, alert)；state 缺字段按 0 算（触线告警，绝不静默满分）。"""
    parts: list[str] = []
    alert = False
    for field in LINE_ORDER:
        try:
            v = float(getattr(state, field, 0.0))
        except (TypeError, ValueError):
            v = 0.0
        low = v < float(thresholds.get(field, 0.0))
        alert = alert or low
        parts.append(f"{INTERACT_FIELD_LABEL.get(field, field)}{v:.0f}"
                     + ("⚠" if low else ""))
    return "  ".join(parts), alert


class NeedsEngine:
    """养成规则引擎：区段/联动/衰减 tick/交互应用。无 Qt。"""

    def __init__(self, store: PetStateStore,
                 needs_cfg: dict | None = None,
                 gains: dict | None = None,
                 interaction_cfg: dict | None = None,
                 msg_overrides: dict | None = None,
                 on_event: Callable[[str, dict], None] | None = None):
        self._store = store
        self._interlock = self._normalize_interlock(needs_cfg or {})
        self._gains = dict(gains or {})
        icfg = interaction_cfg or {}
        self._icfg = {
            "reject_fullness": float(icfg.get("reject_fullness", 92.0)),
            "fatigue_times": int(icfg.get("fatigue_times", 5)),
            "fatigue_window_min": float(icfg.get("fatigue_window_min", 10.0)),
        }
        self._msg_overrides = dict(msg_overrides or {})
        self._interact_log: dict[str, list[float]] = {}
        # 区段跃迁事件（预留呈现层订阅；on_event(name, payload)）
        self._on_event = on_event
        self._last_zones: dict[str, str] | None = None

    # ---- 构造辅助 ----
    @staticmethod
    def _normalize_interlock(cfg: dict) -> dict:
        out = dict(DEFAULT_INTERLOCK)
        for key in _INTERLOCK_KEYS:
            if key not in cfg:
                continue
            try:
                v = float(cfg[key])
            except (TypeError, ValueError):
                log.warning("needs.%s 非法 %r，用默认 %s", key, cfg[key],
                            out[key])
                continue
            out[key] = max(0.0, min(_INTERLOCK_MAX[key], v))
        return out

    # ---- 区段 ----
    def zones(self, state: PetState) -> dict[str, str]:
        return {f: zone(f, float(getattr(state, f, 0.0)))
                for f in NEED_FIELDS}

    def alerts(self, state: PetState, thresholds: dict) -> list[str]:
        """触线字段列表（LINE_ORDER 序）。阈值由调用方注入
        （proactive.need_bubble 仍是阈值的配置出处）。"""
        out: list[str] = []
        for field in LINE_ORDER:
            try:
                v = float(getattr(state, field, 0.0))
            except (TypeError, ValueError):
                v = 0.0
            if v < float(thresholds.get(field, 0.0)):
                out.append(field)
        return out

    def check_zone_transitions(self, state: PetState) -> None:
        """衰减后调：区段跃迁 → on_event("zone_change", …)。首拍只记基线。"""
        if self._on_event is None:
            self._last_zones = self.zones(state)
            return
        cur = self.zones(state)
        prev = self._last_zones
        self._last_zones = cur
        if prev is None:
            return
        for field in NEED_FIELDS:
            if prev.get(field) != cur.get(field):
                try:
                    self._on_event("zone_change", {
                        "field": field, "from": prev.get(field),
                        "to": cur.get(field),
                    })
                except Exception:  # noqa: BLE001 — 呈现层回调永不外抛
                    log.exception("zone_change 回调异常")

    # ---- 联动 ----
    def effective_decay(self, state: PetState, base_decay: dict) -> dict:
        """基础衰减率 + 联动修正（点/小时，速率非负）。

        联动取**当前态**系数作用于整段 wall-clock delta——离线补衰减时
        用回档时点状态近似，误差可接受（离线期间状态只降不升，回档时点
        即最饿/最脏，加压取最强档，宁紧勿松）。"""
        eff: dict[str, float] = {}
        for f in NEED_FIELDS:
            try:
                eff[f] = max(0.0, float(base_decay.get(f, 0.0)))
            except (TypeError, ValueError):
                eff[f] = 0.0
        p = self._interlock
        mood = eff["mood"]
        if float(state.fullness) < p["drag_fullness"]:
            mood += p["hunger_mood_drag"]
        if float(state.cleanliness) < p["drag_cleanliness"]:
            mood += p["dirt_mood_drag"]
        if (float(state.fullness) >= p["content_fullness"]
                and float(state.cleanliness) >= p["content_cleanliness"]):
            mood = max(0.0, mood - p["content_mood_gain"])
        eff["mood"] = mood
        return eff

    def tick_decay(self, base_decay: dict,
                   age_speed_multiplier: float = 1.0) -> None:
        """衰减 tick（app 1s QTimer 调）：联动修正后交 store（wall-clock）。"""
        self._store.apply_decay(
            self.effective_decay(self._store.get(), base_decay),
            age_speed_multiplier)

    # ---- 状态读取 ----
    def get_state(self) -> PetState:
        """当前养成状态（store 透传；呈现层少一次私有访问）。"""
        return self._store.get()

    # ---- 交互应用 ----
    def interact(self, kind: str, *, now: float) -> InteractionOutcome:
        """决策 + 数值落账 + 疲劳记账。out.field=None（未知 kind）时不动账。"""
        state = self._store.get()
        out = decide_interaction(
            kind,
            gain=float(self._gains.get(kind, 0.0)),
            mood=state.mood,
            fullness=state.fullness,
            reject_fullness=self._icfg["reject_fullness"],
            fatigue_times=self._icfg["fatigue_times"],
            fatigue_window_s=self._icfg["fatigue_window_min"] * 60.0,
            recent=self._interact_log.get(kind, ()),
            now=now,
            msg_overrides=self._msg_overrides,
        )
        if out.field is None:
            return out
        if out.delta:
            self._store.update(**{out.field: out.delta})
            # 仅生效的交互计数；窗口外时间戳顺手清理（防列表无界增长）
            window_s = self._icfg["fatigue_window_min"] * 60.0
            entries = [t for t in self._interact_log.get(kind, ())
                       if now - t <= window_s]
            entries.append(now)
            self._interact_log[kind] = entries
        return out

    # 测试观察
    def interact_log(self, kind: str) -> tuple[float, ...]:
        return tuple(self._interact_log.get(kind, ()))
