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

import math
import time
from dataclasses import dataclass, field

from pet.pet_state import Stage


def _clamp01(v: float) -> float:
    """0-100 clamp（attention 是第四独立数值，与心情同量纲）。"""
    return max(0.0, min(100.0, v))


@dataclass(frozen=True)
class Band:
    """半连续陪伴度的一档：名字 + 动作集 + 每小时上限 + 文案语气。

    v0.24.8：取代「单阈值 35 + 断崖触发」。``up_to`` 为**上沿（不含）**，
    按 ``bands`` 顺序自上而下取首个 ``attention < up_to`` 的档——所以请按
    ``up_to`` 升序声明（默认表即升序）。
    """

    key: str
    up_to: float
    name: str
    behaviors: tuple = ()
    per_hour: int = 0
    tone: str = ""


# 出厂五档（依据 wiki 设计-陪伴度分档与动作.md §2）：
# L0 委屈（动作最勤但语气收敛，对应 protest→despair 的转向）
# L1 很想很想你（奔光标 + 白跑一趟语气） L2 很想你（奔光标） L3 有点想你（只冒泡）
# L4 安好（不打扰）。全局 hourly/daily 仍是硬顶，档位上限只能收紧。
DEFAULT_BANDS: tuple = (
    Band("very_low", 30.0, "委屈", ("run_to_cursor", "bubble_whine"), 2, "ignored"),
    Band("low", 50.0, "很想很想你", ("run_to_cursor",), 1, "pat_failed"),
    Band("mid", 70.0, "很想你", ("run_to_cursor",), 1, "low_attention"),
    Band("soft", 85.0, "有点想你", ("bubble_whine",), 1, "low_attention"),
    Band("ok", 100.0, "安好", (), 0, ""),
)


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
    pester_threshold: float = 35.0   # @deprecated v0.24.8：改为分档后仅供
    #   旧配置/状态行低线兜底；纠缠判定看 ``bands``（见 should_pester）
    hourly_limit: int = 2            # 频控：最近 1 小时上限
    daily_limit: int = 3             # 日频：最近 24 小时上限
    backoff_s: tuple = (300.0, 1800.0, 7200.0)  # 退避阶梯（秒）：连续被无视第 1/2/3+ 次
    behaviors: tuple = ("run_to_cursor", "bubble_whine")  # 纠缠行为池
    pester_enabled: bool = True      # v0.24.5 托盘「安静模式」总闸（config
    #   ``attention.pester.enabled``）：False → ``should_pester`` 恒 None，
    #   但 decay/gain 照常演化（数值不被 gate，只 gate 行为——技术方案 §6 验收）

    # ---- v0.24.8 双时间常数衰减（PM 拍板：短的模拟情绪、长的模拟情感） ----
    # attention 不再匀速线性漏掉，而是跟随一条**基准曲线**：
    #   baseline(T) = 100 − fast_drop·(1 − e^(−T/τf)) − slow_drop·(1 − e^(−T/τs))
    # T = 距上次互动的小时数（按 Stage 相对速度缩放）。前置「情绪」掉得快、
    # 后置「情感」慢且长尾，且**带地板值** 100−fast_drop−slow_drop（默认 25，
    # 即永远不会「不在意了」，只是收声）——这正是依赖与依恋分离的形状。
    fast_drop: float = 25.0          # 情绪分量最终掉多少分
    tau_fast_h: float = 2.5          # 情绪时间常数（小时）
    slow_drop: float = 50.0          # 情感分量最终掉多少分
    tau_slow_h: float = 24.0         # 情感时间常数（小时）
    bands: tuple = DEFAULT_BANDS     # 五档（空 → 回退 DEFAULT_BANDS）
    decay_model: str = "biexp"       # "linear" = 旧匀速衰减（保留供测试/兼容）

    # ---- v0.24.8 交互 = 钟形输入（PM：「给一个小的钟形输入，改变微分方程的边界」）----
    # 交互不再是阶跃 +gain，而是把 gain 拆成「即时一小份 + 钟形一份」：
    #   dA/dt = −decay(T) + Σ u_i(t)，u_i 为高斯速率（∫u_i = gain·bell_share）
    # 于是 attention 是**平滑灌入**的（钟形速率 → S 形抬升），整体仍增强。
    bell_share: float = 0.4          # 走钟形的那一份占比（0 = 纯阶跃，旧行为）
    bell_sigma_min: float = 15.0     # 钟形宽度 σ（分钟）；脉冲跨度约 ±2σ


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
    """should_pester 命中时返回的决策。

    v0.24.8：附 ``band_key`` / ``tone``——文案语气不再写死 ``low_attention``，
    由**档位**决定（委屈档用 ``ignored`` 池、白跑一趟档用 ``pat_failed`` 池）。
    """

    behavior: str
    attention: float
    band_key: str = ""
    tone: str = ""


def _normalize_bands(bands) -> tuple:
    """config 里的档位（可能是 dict 列表）→ ``Band`` 元组；空 → 出厂五档。"""
    out = []
    for b in bands or ():
        if isinstance(b, Band):
            out.append(b)
            continue
        try:
            out.append(Band(
                key=str(b.get("key") or ""),
                up_to=float(b.get("up_to", 100.0)),
                name=str(b.get("name") or ""),
                behaviors=tuple(b.get("behaviors") or ()),
                per_hour=int(b.get("per_hour", 0) or 0),
                tone=str(b.get("tone") or ""),
            ))
        except Exception:  # 坏条目跳过（fail-open 到出厂档）
            continue
    return tuple(out) or DEFAULT_BANDS


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
        # v0.24.8：T 的锚点 = max(会话起点, 上次互动)。这样**离线时段不计衰减**
        # （与旧行为一致，也避免"刚开就委屈"）；一旦有交互，T 归零重新起算。
        self._t0 = self._last_update
        self._hours_seen = 0.0
        self._bands = _normalize_bands(self._cfg.bands)
        self._pending_pulses: list = []   # v0.24.8 未灌完的钟形输入 [(中心, 幅度, σ秒)]
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
        """交互 → attention 增益 + 清退避 + 更新关系记忆。返回新 attention。

        v0.24.8：增益拆两份——``1−bell_share`` 即时生效，其余以**钟形速率**
        灌入（见 ``_settle_pulses``）。所以本方法返回的是「即时那一份 + 已灌
        完的历史脉冲」，完整抬升要等到脉冲窗口走完。
        """
        now = self._now(now)
        gain = float(self._cfg.gain.get(kind, 0.0))
        self._settle_pulses(now)          # 先把之前的钟形结清，避免混算
        share = min(1.0, max(0.0, float(self._cfg.bell_share)))
        self._attention = _clamp01(self._attention + gain * (1.0 - share))
        if share > 0.0 and gain != 0.0:
            sigma = max(1.0, float(self._cfg.bell_sigma_min) * 60.0)
            # 中心 now+2σ：t≈now 处≈0 起步，约 now+4σ 灌完（高斯 CDF 的形状）
            self._pending_pulses.append(
                (now + 2.0 * sigma, gain * share, sigma, 0.0))
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
        # v0.24.8：交互把「距离」归零——双指数基准曲线从头起算（下一次
        # apply_decay 的损失量按 baseline(0)→baseline(ΔT) 取，恒为 0 起步）
        self._hours_seen = 0.0
        return self._attention

    def apply_decay(self, stage: Stage, now: float | None = None) -> float:
        """wall-clock 衰减（离线补衰减），速率取 stage 分档。返回新 attention。

        v0.24.8 双时间常数模型（PM 拍板）：两个不同时间常数的指数项相加——
        **短的模拟情绪**（τ≈2.5h，掉得快、先来）、**长的模拟情感**（τ≈24h，
        慢、长尾）。做法不是「按当前值乘速率」，而是把 attention 拉向一条随
        距离 T 变化的**基准曲线**：

            baseline(T) = 100 − fast_drop·(1−e^(−T/τf)) − slow_drop·(1−e^(−T/τs))

        每次只扣掉「曲线在这一步的下降量」，所以：
        * 交互增益把数值抬到曲线**之上**后，会随曲线回落，不会被强行拉回；
        * 曲线自身饱和 → 长期不理她衰减趋于 0，停在地板
          100−fast_drop−slow_drop（默认 25，即「不会不在意，只是收声」）；
        * Stage 只改相对速度（decay_per_hour / FINAL 的比值），不改曲线形状。
        ``decay_model="linear"`` 保留旧的匀速实现（测试/回退用）。
        """
        now = self._now(now)
        name = getattr(stage, "name", stage)  # 兼容 Stage enum / 字符串
        self._settle_pulses(now)   # 先把钟形输入灌入，再按曲线扣衰减
        if str(self._cfg.decay_model).lower() == "linear":
            rate = self._cfg.decay_per_hour.get(name, 2.5)
            dt_hours = (now - self._last_update) / 3600.0
            if dt_hours > 0:
                self._attention = _clamp01(self._attention - float(rate) * dt_hours)
            self._last_update = now
            return self._attention
        h_new = self._hours_at(now)
        lost = self._baseline(self._hours_seen, name) - self._baseline(h_new, name)
        if lost > 0:
            self._attention = _clamp01(self._attention - float(lost))
        self._hours_seen = h_new
        self._last_update = now
        return self._attention

    # ---- v0.24.8 基准曲线 / 档位 / 钟形输入 ----

    def _settle_pulses(self, now: float) -> float:
        """把钟形输入在本时刻**应已灌入**的量加到 attention，返回本步增量。

        每个脉冲是高斯**速率** u(t)=amp/(σ√2π)·e^{−(t−c)²/2σ²}，累计灌入
        = amp·Φ((t−c)/σ)（Φ 为正态 CDF，用 ``math.erf`` 算）。按「已灌比例」
        记账，故重复调用不会重复计账；比例到 99.9% 即出列。
        """
        if not self._pending_pulses:
            return 0.0
        added = 0.0
        keep = []
        for center, amp, sigma, done in self._pending_pulses:
            frac = 0.5 * (1.0 + math.erf((float(now) - center)
                                        / (sigma * math.sqrt(2.0))))
            frac = min(1.0, max(0.0, frac))
            step = amp * max(0.0, frac - done)
            if step:
                added += step
            if frac < 0.999:
                keep.append((center, amp, sigma, max(done, frac)))
        self._pending_pulses = keep
        if added:
            self._attention = _clamp01(self._attention + added)
        return added

    def _hours_at(self, now: float) -> float:
        """距「上次互动（或本会话起点）」的小时数，恒 ≥ 0。"""
        anchor = max(self._t0, float(self._relation.last_interact_at or 0.0))
        return max(0.0, (float(now) - anchor) / 3600.0)

    def _stage_speed(self, name: str) -> float:
        """Stage 相对速度（FINAL 为 1.0）——沿用既有 decay_per_hour 的口径。"""
        ref = float(self._cfg.decay_per_hour.get("FINAL", 1.5) or 0.0)
        if ref <= 0:
            return 1.0
        return max(0.1, float(self._cfg.decay_per_hour.get(name, 2.5) or 0.0) / ref)

    def _baseline(self, hours: float, stage_name: str = "FINAL") -> float:
        """基准曲线：给定「距离」小时数应处的 attention。"""
        h = max(0.0, float(hours)) * self._stage_speed(stage_name)
        tau_f = max(1e-6, float(self._cfg.tau_fast_h))
        tau_s = max(1e-6, float(self._cfg.tau_slow_h))
        fast = 1.0 - math.exp(-h / tau_f)
        slow = 1.0 - math.exp(-h / tau_s)
        return _clamp01(100.0 - float(self._cfg.fast_drop) * fast
                        - float(self._cfg.slow_drop) * slow)

    def band_for(self, attention: float | None = None) -> Band:
        """attention → 所属档（首个 ``att < up_to``；都不过 → 最后一档）。"""
        a = self._attention if attention is None else float(attention)
        for b in self._bands:
            if a < b.up_to:
                return b
        return self._bands[-1]

    @property
    def bands(self) -> tuple:
        return self._bands

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
        # 4 档位门（v0.24.8 取代单阈值）：落档决定动作集/上限/语气；
        #   当前档没有动作 = 这一档不打扰（L4 安好）。
        band = self.band_for(self._attention)
        if not band.behaviors:
            return None
        # 5 退避门
        if now < self._next_try_at:
            return None
        # 6 频控门（最近 3600s）：全局上限与档位上限取小——档位只能收紧
        hour_ago = now - 3600.0
        in_hour = sum(1 for t in self._pester_times if t > hour_ago)
        cap_h = self._cfg.hourly_limit
        if band.per_hour > 0:
            cap_h = min(cap_h, band.per_hour)
        if in_hour >= cap_h:
            return None
        # 7 日频门（最近 86400s）
        day_ago = now - 86400.0
        if sum(1 for t in self._pester_times if t > day_ago) >= self._cfg.daily_limit:
            return None
        # 选行为：优先本档动作集（空则回退全局池）；排除 last_behavior，确定性取首个
        behaviors = list(band.behaviors) or list(self._cfg.behaviors)
        if not behaviors:
            return None
        if self._last_behavior in behaviors and len(behaviors) > 1:
            candidates = [b for b in behaviors if b != self._last_behavior]
            behavior = candidates[0] if candidates else behaviors[0]
        else:
            behavior = behaviors[0]
        self._pester_times.append(now)
        self._last_behavior = behavior
        return PesterDecision(behavior=behavior, attention=self._attention,
                              band_key=band.key,
                              tone=band.tone or "low_attention")

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
