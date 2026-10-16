"""v0.24 主动纠缠互动核心 —— attention 陪伴度数值 + 纠缠触发决策（纯逻辑）。

纯共享层（mac/win 同代码，零平台库、零 Qt、零 requests）——设计依据
wiki 设计-纠缠互动技术方案.md §2.1 + 资料-桌宠与虚拟陪伴全景调研.md。

设计要点：
* attention 数值：交互↑（摸头/逗一逗/聊天）、时间↓（速率随 Stage 分档：
  YOUNG 掉得快更黏人、FINAL 更独立）。本版数值存引擎内部，0.24.1 再接入
  pet_state 持久化（migrate v1→v2 + ``_NUMERIC_FIELDS`` 加 "attention"，
  否则 apply_decay 会静默丢它的速率——资料-纠缠互动现状调查 缺口 2）。
* ``should_pester`` 五重门：静默(fullscreen/dnd/focus/video) → breakpoint
  时机 → focus 防御探测 → 阈值(attention 低) → 退避 → 频控/日频 → 选行为。
* 退让铁律（全景调研 §1.3 判据）：被无视后只退避、绝不升级力度——「升级=
  优化留存而非陪伴；退让=傲娇人格天然语法」。
* 关系记忆 ``RelationMemory``：显式结构化状态（Anchor 审计：模型轨迹记忆
  仅 44.4% 接近随机，不可靠），随交互更新，供撒娇台词引用「鱼片」「第 N 次
  摸头」——锚定「记得你」留存支柱（Bickmore & Picard 2005）。
* ``now_fn`` 注入可单测（对齐 weather.py / test_weather.py FakeClock 模式）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from pet.pet_state import Stage


def _clamp01(v: float) -> float:
    """0-100 clamp（attention 是第四独立数值，与心情同量纲）。"""
    return max(0.0, min(100.0, v))


@dataclass(frozen=True)
class AttentionConfig:
    """attention 引擎配置（默认值对齐技术方案 §2.1，均为待 A/B 初始参数）。"""

    # 衰减速率（每小时掉的 attention 分），key = Stage.name
    decay_per_hour: dict[str, float] = field(default_factory=lambda: {
        "YOUNG": 4.0, "ADULT": 2.5, "FINAL": 1.5,
    })
    # 交互增益：pet=摸头 / poke=逗一逗 / chat=聊天
    gain: dict[str, float] = field(default_factory=lambda: {
        "pet": 40.0, "poke": 25.0, "chat": 30.0,
    })
    pester_threshold: float = 35.0   # attention 低于此线才可能纠缠
    hourly_limit: int = 2            # 频控：最近 1 小时上限
    daily_limit: int = 3             # 日频：最近 24 小时上限
    backoff_s: tuple = (300.0, 1800.0, 7200.0)  # 退避阶梯（秒）：连续被无视第 1/2/3+ 次
    behaviors: tuple = ("run_to_cursor", "bubble_whine")  # 纠缠行为池
    pester_enabled: bool = True      # v0.24.5 托盘「安静模式」总闸（config
    #   ``attention.pester.enabled``）：False → ``should_pester`` 恒 None，
    #   但 decay/gain 照常演化（数值不被 gate，只 gate 行为——技术方案 §6 验收）


@dataclass(frozen=True)
class PesterContext:
    """时机门控输入（interruptibility 信号，全景调研 §2.2）。"""

    breakpoint: bool = False      # 任务边界检测（键盘停顿/窗口切换/离开焦点）
    fullscreen: bool = False
    dnd: bool = False
    focus: bool = False
    active_video: bool = False
    idle_s: float = 0.0


@dataclass(frozen=True)
class RelationMemory:
    """关系记忆最小版（显式结构化，非靠模型自然记）。"""

    nickname: str = ""            # 用户昵称（默认锚点「鱼片」）
    pet_name: str = ""            # 她给用户起的外号
    pat_count: int = 0
    feed_count: int = 0
    last_interact_at: float = 0.0  # 上次互动时间（「N 天没理我」数据源）


@dataclass(frozen=True)
class PesterDecision:
    """should_pester 命中时返回的决策。"""

    behavior: str
    attention: float


class AttentionEngine:
    """attention 数值 + 纠缠触发决策引擎（有状态，now_fn/focus_state_fn 注入）。"""

    def __init__(
        self,
        cfg: AttentionConfig | None = None,
        *,
        attention: float = 70.0,
        now_fn=time.time,
        relation: RelationMemory | None = None,
        focus_state_fn=None,
    ):
        self._cfg = cfg or AttentionConfig()
        self._attention = _clamp01(float(attention))
        self._now_fn = now_fn
        self._focus_state_fn = focus_state_fn
        self._relation = relation or RelationMemory()
        self._last_update = now_fn()
        self._ignore_count = 0
        self._next_try_at = 0.0
        self._last_behavior: str | None = None
        self._pester_times: list[float] = []

    # ---- 只读访问 ----
    @property
    def attention(self) -> float:
        return self._attention

    @property
    def relation(self) -> RelationMemory:
        return self._relation

    @property
    def now_fn(self):
        return self._now_fn

    def _now(self, now: float | None) -> float:
        return self._now_fn() if now is None else float(now)

    # ---- 交互 / 衰减 ----
    def on_interaction(self, kind: str, now: float | None = None) -> float:
        """交互 → attention 增益 + 清退避 + 更新关系记忆。返回新 attention。"""
        now = self._now(now)
        gain = self._cfg.gain.get(kind, 0.0)
        self._attention = _clamp01(self._attention + float(gain))
        self._ignore_count = 0
        self._next_try_at = 0.0
        pat = self._relation.pat_count
        feed = self._relation.feed_count
        if kind == "pet":
            pat += 1
        elif kind == "feed":
            feed += 1
        self._relation = RelationMemory(
            nickname=self._relation.nickname,
            pet_name=self._relation.pet_name,
            pat_count=pat,
            feed_count=feed,
            last_interact_at=now,
        )
        return self._attention

    def apply_decay(self, stage: Stage, now: float | None = None) -> float:
        """wall-clock 衰减（离线补衰减），速率取 stage 分档。返回新 attention。"""
        now = self._now(now)
        name = getattr(stage, "name", stage)  # 兼容 Stage enum / 字符串
        rate = self._cfg.decay_per_hour.get(name, 2.5)
        dt_hours = (now - self._last_update) / 3600.0
        if dt_hours > 0:
            self._attention = _clamp01(self._attention - float(rate) * dt_hours)
        self._last_update = now
        return self._attention

    def _focus_active(self) -> bool:
        """focus 防御探测：focus_state_fn 为 None / 抛异常 / 返回非 "FOCUS"
        都视为「非专注」，绝不崩。0.25 番茄线落地 FocusSession 后自动接通。"""
        fn = self._focus_state_fn
        if fn is None:
            return False
        try:
            return fn() == "FOCUS"
        except Exception:
            return False

    # ---- 纠缠触发 ----
    def set_pester_enabled(self, enabled: bool) -> None:
        """v0.24.5：运行时切换「安静模式」（托盘勾选即时生效）。

        用 ``dataclasses.replace`` 换掉 frozen cfg 的单个字段——**保留全部
        运行时状态**（attention 数值 / 连续被无视计数 / 退避截止 / 频控窗口
        / 上次行为），故关掉再打开是「接着算」而不是回到出厂值。这正是不
        重建引擎的理由。
        """
        from dataclasses import replace

        self._cfg = replace(self._cfg, pester_enabled=bool(enabled))

    def should_pester(
        self, now: float | None = None, ctx: PesterContext | None = None,
    ) -> PesterDecision | None:
        """五重门判定；任一不过返回 None。命中则选行为并记录触发时间。

        v0.24.5：**门 0 总闸**（托盘「安静模式」）——``pester_enabled=False``
        恒 None。注意此闸**只挡行为**：``apply_decay``/``on_interaction``
        照常演化 attention 与关系记忆，故关掉后重开能接着算（不是回到
        出厂值）。
        """
        now = self._now(now)
        ctx = ctx or PesterContext()
        # 0 总闸（安静模式）
        if not self._cfg.pester_enabled:
            return None
        # 1 静默门
        if ctx.fullscreen or ctx.dnd or ctx.focus or ctx.active_video:
            return None
        # 2 breakpoint 时机门（只在任务边界放行，全景调研 §2.2）
        if not ctx.breakpoint:
            return None
        # 3 focus 防御探测
        if self._focus_active():
            return None
        # 4 阈值门（attention 高于线 = 不饿不缠）
        if self._attention > self._cfg.pester_threshold:
            return None
        # 5 退避门
        if now < self._next_try_at:
            return None
        # 6 频控门（最近 3600s）
        hour_ago = now - 3600.0
        if sum(1 for t in self._pester_times if t > hour_ago) >= self._cfg.hourly_limit:
            return None
        # 7 日频门（最近 86400s）
        day_ago = now - 86400.0
        if sum(1 for t in self._pester_times if t > day_ago) >= self._cfg.daily_limit:
            return None
        # 选行为：排除 last_behavior（池 >1 时），确定性取首个
        behaviors = list(self._cfg.behaviors)
        if self._last_behavior in behaviors and len(behaviors) > 1:
            candidates = [b for b in behaviors if b != self._last_behavior]
            behavior = candidates[0] if candidates else behaviors[0]
        else:
            behavior = behaviors[0]
        self._pester_times.append(now)
        self._last_behavior = behavior
        return PesterDecision(behavior=behavior, attention=self._attention)

    def on_pester_ignored(self, behavior: str, now: float | None = None) -> None:
        """被无视 → 退让：只按阶梯加深退避（300→1800→7200 封顶），
        绝不升级力度（频控/日频/阈值一律不变）。"""
        now = self._now(now)
        self._ignore_count += 1
        idx = min(self._ignore_count - 1, len(self._cfg.backoff_s) - 1)
        self._next_try_at = now + float(self._cfg.backoff_s[idx])

    def on_pester_answered(self, behavior: str, now: float | None = None) -> float:
        """被回应 → attention 回满 + 退避清零。返回新 attention。"""
        now = self._now(now)
        self._attention = 100.0
        self._ignore_count = 0
        self._next_try_at = 0.0
        self._relation = RelationMemory(
            nickname=self._relation.nickname,
            pet_name=self._relation.pet_name,
            pat_count=self._relation.pat_count,
            feed_count=self._relation.feed_count,
            last_interact_at=now,
        )
        return self._attention
