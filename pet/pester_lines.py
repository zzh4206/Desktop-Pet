"""v0.24 主动纠缠「行为② 气泡撒娇」——罐头池 + LLM 抽样策略（纯逻辑共享层）。

纯共享层（mac/win 同代码，零平台库、零 Qt、零 requests）——设计依据
wiki 设计-纠缠互动技术方案.md §4 + 资料-桌宠与虚拟陪伴全景调研.md。

设计要点：
* **三级罐头池按纠缠阶段分档**：``low_attention``（低陪伴度撒娇）/
  ``pat_failed``（求摸头未遂）/ ``ignored``（被无视委屈）。语气逐级**收敛**：
  被无视后是委屈，不是指责、更不是升级（退让铁律的文案面）。
* **LLM 抽样 ≤1/3（累计硬上限）**：不是随机抖动——保证「陪伴质感」不被
  「LLM 刷屏」淹没，比例可日志核查（验收项）。LLM 失败/离线**一律回落罐头**，
  绝不空手（拔网线不崩）。
* **隔离上下文**：LLM 走专用 ``PESTER_SYSTEM_OVERRIDE``（撒娇人设），
  调用方以 ``tools_override=None`` 不挂工具——与人设链、决策链完全隔离
  （对齐 ``proactive._DECISION_SYSTEM`` 既有工艺）。
* **事实注入而非模型编造**：``days_since`` 由结构化 ``RelationMemory.last_interact_at``
  算出「N 天没理我」。Anchor 审计结论：模型自身轨迹记忆仅 44.4% 接近随机、
  不可靠——故**事实由状态层给、模型只负责措辞**。
* 人格锚点（wiki 设计-主动纠缠互动.md）：女仆鲸鱼娘、傲娇又粘人；鲸类情绪
  器官（吐泡/甩尾/浮出/鲸歌）；锚定「吃白饭的大肥鱼」「鱼片」「拒绝被叫胖」；
  **禁用猫狗行为与真整蛊**（非直觉映射）。
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

# ---- 罐头池（默认；config ``attention.pester.lines`` 可逐类覆写）----

PESTER_LINE_CATEGORIES = ("low_attention", "pat_failed", "ignored")

_DEFAULT_LINES: dict[str, tuple] = {
    # 低陪伴度撒娇：粘人、耍赖，但仍是「想你了」不是「你欠我」
    "low_attention": (
        "喂，鱼片～你都好久没戳我了，我尾巴都甩累了…",
        "哼，我才不是想你了，只是…只是泡吐多了没人看。",
        "在忙吗？我可以安静待着哦…（尾巴悄悄绕上你手腕）",
        "你身上有外面的味道。……我是说，你是不是该摸摸我了？",
        "桌角好冷哦。你要是过来，我就勉强让你摸一下。",
    ),
    # 求摸头未遂：都已经游到光标边上了，却没被摸
    "pat_failed": (
        "我都游到你手边了，你就……看一眼？",
        "哼！谁要在你光标旁边待着，我只是刚好路过。（尾巴还在原地甩）",
        "……（浮在你指边吐了个泡，然后装作若无其事）",
        "游过来了哦。……喂，是这边啦。",
        "白跑一趟。大肥鱼你倒是摸摸我呀。",
    ),
    # 被无视委屈：退让铁律的文案面——委屈、收敛，绝不指责或升级
    "ignored": (
        "……算了，我自己玩水去。",
        "嗯，你忙。我先沉下去了。",
        "（小小声）那我等你哦。不催你的。",
        "泡吐完了。……我先安静一会儿。",
        "没关系，我本来也没很想被摸。真的。",
    ),
}

# 「N 天没理我」模板：仅当 days ≥ 1 时才候选（不编造 0 天的委屈）
_DAY_TEMPLATES: dict[str, tuple] = {
    "low_attention": (
        "都 {days} 天没理我了…鱼片是不是把我忘在屏幕角落了？",
        "{days} 天。我数着的哦。",
    ),
    "pat_failed": (
        "{days} 天了，你连手都不伸一下。",
    ),
    "ignored": (
        "……{days} 天都这样。我先不吵你了。",
    ),
}

PESTER_SYSTEM_OVERRIDE = (
    "你是一只住在桌面的女仆鲸鱼娘，傲娇又粘人，说话短、口语化、带点撒娇。"
    "你是鲸类：情绪用吐泡、甩尾、浮出水面、鲸歌表达——不要用猫狗式行为"
    "（喵/汪/摇尾巴/炸毛/蹭腿）。可以自称『大肥鱼』被叫会不高兴，"
    "称呼用户为『鱼片』。禁止任何真实的破坏、整蛊或道德绑架式索取。"
    "用户冷淡时你要**退让收敛**（委屈、安静），绝不指责、威胁或加大力度索取。"
    "只输出一句话（30 字以内），不要解释、不要加引号、不要 emoji 以外的格式。"
)


def days_since(last_interact_at: float, now: float) -> int:
    """距上次互动的整数天（<1 天 → 0）。

    ``last_interact_at`` ≤ 0（无记录）→ 0：**不编造**「好久没理我」——
    没有事实就不生成该说法（诚实护栏）。
    """
    try:
        last = float(last_interact_at)
        now = float(now)
    except (TypeError, ValueError):
        return 0
    if last <= 0.0:
        return 0
    delta = now - last
    if delta <= 0.0:
        return 0
    return int(delta // 86400.0)


@dataclass(frozen=True)
class RelationSnapshot:
    """PesterVoice 需要的最小关系事实（与 attention.RelationMemory 同构）。"""

    nickname: str = "鱼片"
    pat_count: int = 0
    feed_count: int = 0
    last_interact_at: float = 0.0


@dataclass
class PesterVoiceStats:
    """抽样计数（供日志核查「LLM 比例 ≤1/3」验收项）。"""

    total: int = 0
    llm: int = 0
    llm_failed: int = 0

    @property
    def ratio(self) -> float:
        return (self.llm / self.total) if self.total else 0.0


class PesterVoice:
    """撒娇文案合成器：罐头池 + LLM 抽样（≤1/3 累计） + 失败兜底。

    ``llm_fn`` 为注入的隔离调用：``llm_fn(user_text: str) -> str``，调用方
    需自行以 ``system_override=PESTER_SYSTEM_OVERRIDE, tools_override=None``
    装配（本模块零依赖，便于 win/纯逻辑测试）。None → 永久罐头（离线不崩）。
    """

    def __init__(
        self,
        lines: dict | None = None,
        *,
        llm_fn=None,
        rng: random.Random | None = None,
        llm_ratio_cap: float = 1.0 / 3.0,
        max_len: int = 60,
    ):
        self._lines: dict[str, tuple] = {}
        for cat in PESTER_LINE_CATEGORIES:
            override = None
            if isinstance(lines, dict):
                override = lines.get(cat)
            if isinstance(override, (list, tuple)) and override:
                self._lines[cat] = tuple(str(x) for x in override if str(x).strip())
            else:
                self._lines[cat] = _DEFAULT_LINES[cat]
        self._llm_fn = llm_fn
        self._rng = rng or random.Random()
        # 上限以「第几份」表达：llm_cap_n 份里最多 1 份走 LLM（默认 3 选 1）
        self._cap_n = max(1, int(round(1.0 / llm_ratio_cap))) if llm_ratio_cap > 0 else 1
        self._max_len = max(8, int(max_len))
        self.stats = PesterVoiceStats()

    # ---- 查询 ----
    @property
    def pool(self) -> dict:
        return {k: tuple(v) for k, v in self._lines.items()}

    def _llm_allowed(self) -> bool:
        """累计硬上限：用 LLM 后仍满足 llm/total ≤ 1/cap_n。

        **以调用前的计数判定**（total 此时尚未自增）：条件
        ``(llm+1) * cap_n <= total+1``。cap_n=3 时命中第 3/6/9… 次
        （恰好 1/3，可测、不抖动）。
        """
        return (self.stats.llm + 1) * self._cap_n <= (self.stats.total + 1)

    # ---- 合成 ----
    def compose(
        self,
        category: str,
        memory: RelationSnapshot | None = None,
        now: float | None = None,
    ) -> str:
        """合成一句撒娇文案。**永不抛错、永不返回空串**（拔网线不崩）。"""
        import time as _time

        cat = category if category in self._lines else "low_attention"
        mem = memory or RelationSnapshot()
        now_v = _time.time() if now is None else float(now)
        days = days_since(mem.last_interact_at, now_v)

        # 先判定（用调用前的计数），再计数——保证 3 选 1 的节拍不偏移
        allowed = self._llm_fn is not None and self._llm_allowed()
        self.stats.total += 1
        if allowed:
            text = self._try_llm(cat, mem, days)
            if text:
                self.stats.llm += 1
                return text
            # 失败不计入 LLM 用量（比例统计诚实），回落罐头
            self.stats.llm_failed += 1
        return self._canned(cat, days)

    def _try_llm(self, cat: str, mem: RelationSnapshot, days: int) -> str:
        """隔离调用 + 防御式兜底：任何异常/空返回 → "" 由调用方回落罐头。"""
        facts = (
            f"当前情境分类：{cat}；昵称：{mem.nickname or '鱼片'}；"
            f"累计被摸头次数：{mem.pat_count}；投喂次数：{mem.feed_count}；"
            f"距上次互动天数：{days}。"
        )
        try:
            out = self._llm_fn(facts)
        except Exception:
            return ""
        if not isinstance(out, str):
            return ""
        text = out.strip().strip('"').strip("“”").strip()
        # 多行只取首行（模型偶尔加解释）
        text = text.splitlines()[0].strip() if text else ""
        if not text:
            return ""
        return text[: self._max_len]

    def _canned(self, cat: str, days: int) -> str:
        """罐头兜底：days ≥ 1 时把「N 天」模板并入候选（事实注入）。"""
        pool = list(self._lines.get(cat) or _DEFAULT_LINES["low_attention"])
        if days >= 1:
            pool += [t.format(days=days) for t in _DAY_TEMPLATES.get(cat, ())]
        if not pool:
            pool = list(_DEFAULT_LINES["low_attention"])
        return self._rng.choice(pool)[: self._max_len]
