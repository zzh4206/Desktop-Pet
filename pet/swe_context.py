"""pet/swe_context.py —— mini-swe 上下文管理（v0.22）。

线性历史的 token 膨胀治理，两层手段（廉价优先）：

- **T1 观测瘦身**（确定性、零成本）：最近 ``keep_recent`` 步之外的
  tool 消息改写为「输出头/尾摘录 + 压缩标记」——旧命令输出很少需要
  逐字回看，头（命令回显上下文）尾（报错/退出码常在尾）最有价值。
- **T2 轨迹滚屏摘要**（LLM、超预算时才触发）：估算 token 超预算 →
  最老一半可压步骤经 ``summarize_fn`` 压成一条 ``[此前步骤摘要]``
  user 消息；失败/空摘要回退 T1 瘦身（fail-open 有界，压缩故障
  绝不杀死任务）。

设计约束（与项目既有模式对齐）：

- **摘要解耦哲学复用**（同 v0.17.1 主聊天）：压缩只影响喂 LLM 的
  ``messages``；``on_step`` UI 流式回显始终全量，用户终端块不缩水。
- **纯函数可测**：本模块不 import LLM/Qt；``summarize_fn`` 由调用方
  注入（app 侧闭包 ``query_once``）。
- **压缩单位 = 步骤对**：OpenAI 格式里 assistant 的 ``tool_calls`` 与
  tool 消息靠 ``tool_call_id`` 配对，只动 tool 消息会留孤儿 tool_call
  → API 400。T2 必须整对替换；T1 只改 tool content 不动结构，安全。
- **契约区永不压缩**：system + user(task)（首个带 tool_calls 的
  assistant 之前的消息）原样保留。
- 摘要消息**单条滚动**：再次触发压缩时，旧 ``[此前步骤摘要]`` 并入
  transcript 重摘，不无限累积。

token 估算启发式（无本地 DS tokenizer）：CJK 字符 ≈1 token、ASCII ≈4
字符/token——宁可高估，压缩早触发比晚触发安全。
"""

from __future__ import annotations

import json
import logging

log = logging.getLogger("pet")

# config.swe.context 缺省值（单一来源，app/config 侧从这里取）
CONTEXT_DEFAULTS = {
    "budget_tokens": 24000,
    "keep_recent": 3,
    "slim_head_chars": 600,
    "slim_tail_chars": 1200,
    "summarize": True,
}

SUMMARY_MARKER = "[此前步骤摘要]"

SWE_SUM_SYSTEM = "你是对话摘要助手。把对话压缩为要点，只输出摘要正文。"
SWE_SUM_PROMPT = (
    "以下是软件工程助手此前执行过的命令行步骤（命令与输出，部分输出已被"
    "压缩）。请压缩为不超过 300 字的要点，必须保留：已完成的事项、已排除的"
    "假设、关键发现（路径/数值/报错原文）、已修改的文件、当前进行到哪一步。"
    "只输出摘要正文。\n\n"
)


def est_tokens(text: str) -> int:
    """粗估 token 数：非 ASCII 按 1 token/字符，ASCII 按 4 字符/token。

    高估无害（压缩早触发），低估会爆窗——所以不用任何折半系数。
    """
    text = text or ""
    ascii_n = sum(1 for c in text if ord(c) < 128)
    return (len(text) - ascii_n) + (ascii_n + 3) // 4


def messages_tokens(messages: list) -> int:
    """整份 messages 的 token 估算（content + tool_calls 参数）。"""
    total = 0
    for m in messages:
        total += est_tokens(str(m.get("content") or ""))
        for tc in m.get("tool_calls") or []:
            total += est_tokens(str(tc.get("function", {}).get("arguments") or ""))
    return total


def slim_text(text: str, head_chars: int, tail_chars: int) -> str:
    """头尾摘录式瘦身：短文原样返回，长文保头保尾、中段以标记替代。"""
    text = text or ""
    head_chars = max(0, int(head_chars))
    tail_chars = max(0, int(tail_chars))
    if head_chars + tail_chars <= 0 or len(text) <= head_chars + tail_chars:
        return text
    return (text[:head_chars]
            + f"\n[…观测已压缩：原 {len(text)} 字符，中段省略…]\n"
            + text[-tail_chars:])


# ---- 步骤切分（压缩的单位） ----

def split_steps(messages: list) -> tuple[list, list]:
    """切为 ``(prefix, steps)``。

    ``prefix`` = 契约区（首个带 tool_calls 的 assistant 之前的消息，
    即 system + user(task)，可能含此前的摘要消息）；``steps`` =
    ``[(assistant_msg, [tool_msgs...]), ...]``——swe_agent 产出的
    线性序列恒为 assistant(tool_calls) 后跟同批 tool 消息。
    """
    cut = len(messages)
    for i, m in enumerate(messages):
        if m.get("role") == "assistant" and m.get("tool_calls"):
            cut = i
            break
    prefix = list(messages[:cut])
    steps: list = []
    cur = None
    for m in messages[cut:]:
        if m.get("role") == "assistant" and m.get("tool_calls"):
            cur = (m, [])
            steps.append(cur)
        elif cur is not None:
            cur[1].append(m)
        # tool 出现在任何 assistant 之前：归 prefix 语义（不该发生，
        # 线性历史下首个带 tool_calls 的 assistant 已是切点，防御即可）
    return prefix, steps


def _rebuild(prefix: list, steps: list) -> list:
    """prefix + 步骤对 → 扁平 messages（浅拷贝，不共享原 dict）。"""
    out = [dict(m) for m in prefix]
    for a, tools in steps:
        out.append(dict(a))
        out.extend(dict(t) for t in tools)
    return out


def _slim_steps(steps: list, head_chars: int, tail_chars: int) -> list:
    """对步骤对里的 tool 消息做瘦身（结构不动——tool_call_id 配对保持）。"""
    out = []
    for a, tools in steps:
        slimmed = []
        for t in tools:
            t2 = dict(t)
            t2["content"] = slim_text(str(t.get("content") or ""),
                                      head_chars, tail_chars)
            slimmed.append(t2)
        out.append((a, slimmed))
    return out


def slim_old_steps(messages: list, keep_recent: int,
                   head_chars: int, tail_chars: int) -> list:
    """T1：最近 ``keep_recent`` 步之外全部瘦身。"""
    prefix, steps = split_steps(messages)
    n_slim = max(0, len(steps) - max(0, int(keep_recent)))
    new_steps = (_slim_steps(steps[:n_slim], head_chars, tail_chars)
                 + [(a, list(tools)) for a, tools in steps[n_slim:]])
    return _rebuild(prefix, new_steps)


def _transcript(steps: list) -> str:
    """步骤对 → 摘要用 transcript（``$ 命令\\n输出`` 逐段）。"""
    parts = []
    for a, tools in steps:
        commands = []
        for tc in a.get("tool_calls") or []:
            fn = tc.get("function", {})
            if fn.get("name") != "bash":
                commands.append(f"({fn.get('name')})")
                continue
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except (json.JSONDecodeError, TypeError):
                args = {}
            commands.append(str(args.get("command") or "?") if isinstance(args, dict) else "?")
        for i, t in enumerate(tools):
            cmd = commands[i] if i < len(commands) else "?"
            parts.append(f"$ {cmd}\n{t.get('content') or ''}")
    return "\n\n".join(parts)


def _pop_old_summary(prefix: list) -> tuple[list, str]:
    """摘出 prefix 里既有的摘要消息（单条滚动：重摘而非累积）。"""
    rest = []
    old = ""
    for m in prefix:
        if (m.get("role") == "user"
                and str(m.get("content") or "").startswith(SUMMARY_MARKER)):
            old = str(m.get("content"))
            continue
        rest.append(m)
    return rest, old


def compact_once(messages: list, *, summarize_fn=None,
                 budget_tokens=None, keep_recent=None,
                 slim_head_chars=None, slim_tail_chars=None) -> tuple[list, dict]:
    """上下文策略单次应用（SweAgent 每轮 query 前调）。

    未超预算原样返回；超预算时压最老一半可压步骤（保最近 keep_recent
    步不动）——优先 LLM 摘要，``summarize_fn`` 缺失/异常/空返回一律回退
    T1 瘦身。返回 ``(messages, info)``，info 供日志与测试断言。
    """
    budget = CONTEXT_DEFAULTS["budget_tokens"] if budget_tokens is None else int(budget_tokens)
    keep = CONTEXT_DEFAULTS["keep_recent"] if keep_recent is None else int(keep_recent)
    head = CONTEXT_DEFAULTS["slim_head_chars"] if slim_head_chars is None else int(slim_head_chars)
    tail = CONTEXT_DEFAULTS["slim_tail_chars"] if slim_tail_chars is None else int(slim_tail_chars)

    before = messages_tokens(messages)
    info = {"triggered": False, "mode": "none", "before": before,
            "after": before, "compacted_steps": 0}
    if before <= budget:
        return messages, info

    prefix, steps = split_steps(messages)
    prefix, old_summary = _pop_old_summary(prefix)
    n_old = len(steps) - keep
    if n_old <= 0:
        # 无整对可压（步数 ≤ keep_recent 但观测超长）：全量瘦身保底
        new_msgs = slim_old_steps(_rebuild(prefix, steps), keep_recent=0,
                                  head_chars=head, tail_chars=tail)
        info.update(triggered=True, mode="slim",
                    after=messages_tokens(new_msgs), compacted_steps=len(steps))
        return new_msgs, info

    old, kept = steps[:n_old], steps[n_old:]

    summary = None
    if summarize_fn is not None:
        n_take = max(1, n_old // 2)   # 摘要保守取半（控单次 LLM 成本）
        taken, kept = old[:n_take], old[n_take:] + kept
        try:
            summary = (summarize_fn(SWE_SUM_PROMPT + old_summary + "\n\n"
                                    + _transcript(taken)) or "").strip() or None
        except Exception as exc:
            log.warning("swe 轨迹摘要失败，回退观测瘦身: %s", exc)
            summary = None

    if summary:
        sum_msg = {"role": "user",
                   "content": f"{SUMMARY_MARKER}\n{summary}"}
        new_msgs = list(prefix) + [sum_msg] + _rebuild([], kept)
        info.update(triggered=True, mode="summary", compacted_steps=n_take)
    else:
        # 瘦身回退：全量压所有老步骤——slim 幂等（对已瘦身文本近乎无操作），
        # 取半会在重复触发时对已压步骤空转、压不平每步增长。仍超预算则
        # 连最近窗口一起压到全量瘦身为止（T1 地板；命令参数不动——真实
        # 场景命令短、输出大，观测瘦身即可回到预算内）。
        new_msgs = _rebuild(prefix, _slim_steps(old, head, tail) + kept)
        if messages_tokens(new_msgs) > budget and n_old < len(steps):
            new_msgs = _rebuild(prefix, _slim_steps(steps, head, tail))
        info.update(triggered=True, mode="slim", compacted_steps=n_old)
    info["after"] = messages_tokens(new_msgs)
    return new_msgs, info


def make_policy(cfg: dict | None, summarize_fn=None):
    """工厂：``cfg``（config.swe.context 段，缺省全 CONTEXT_DEFAULTS）
    → ``messages -> (messages, info)`` 闭包，供 SweAgent(context_policy=)
    注入。``summarize_fn=None`` 即 T1-only。"""
    cfg = cfg or {}
    budget = cfg.get("budget_tokens", CONTEXT_DEFAULTS["budget_tokens"])
    keep = cfg.get("keep_recent", CONTEXT_DEFAULTS["keep_recent"])
    head = cfg.get("slim_head_chars", CONTEXT_DEFAULTS["slim_head_chars"])
    tail = cfg.get("slim_tail_chars", CONTEXT_DEFAULTS["slim_tail_chars"])
    use_sum = cfg.get("summarize", CONTEXT_DEFAULTS["summarize"])

    def policy(messages: list) -> tuple[list, dict]:
        return compact_once(
            messages,
            summarize_fn=summarize_fn if use_sum else None,
            budget_tokens=budget, keep_recent=keep,
            slim_head_chars=head, slim_tail_chars=tail)

    return policy
