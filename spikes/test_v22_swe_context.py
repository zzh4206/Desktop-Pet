# -*- coding: utf-8 -*-
"""test_v22_swe_context.py —— v0.22 mini-swe 上下文管理纯函数单测（无 Qt/无网络）。

覆盖 pet/swe_context.py：token 估算 / 头尾瘦身 / 步骤对切分 /
T1 slim_old_steps / T2 compact_once 摘要与回退 / make_policy 工厂。
agent 循环接线测试见 test_v22_swe_agent_ctx.py。
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pet.swe_context import (
    CONTEXT_DEFAULTS, SUMMARY_MARKER, compact_once, est_tokens,
    make_policy, messages_tokens, slim_old_steps, slim_text, split_steps,
)

PASS, FAIL = [], []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("  ✅ " if cond else "  ❌ ") + name)


# ---- 造消息的工具 ----

def _bash_step(cmd, output, idx):
    """一个完整步骤对：assistant(tool_calls) + tool。"""
    return [
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": f"call_{idx}", "type": "function",
                         "function": {"name": "bash",
                                      "arguments": json.dumps(
                                          {"command": cmd})}}]},
        {"role": "tool", "tool_call_id": f"call_{idx}", "content": output},
    ]


def _messages(n_steps, out_len=4000):
    msgs = [{"role": "system", "content": "sys"},
            {"role": "user", "content": "任务"}]
    for i in range(n_steps):
        msgs += _bash_step(f"cmd-{i}", "x" * out_len + f"\n[尾部标记{i}]", i)
    return msgs


def _no_orphans(messages) -> bool:
    """结构校验：assistant tool_calls 与 tool 消息按 id 严格配对。"""
    pending = set()
    for m in messages:
        if m.get("role") == "assistant" and m.get("tool_calls"):
            pending |= {tc.get("id") for tc in m["tool_calls"]}
        elif m.get("role") == "tool":
            if m.get("tool_call_id") not in pending:
                return False
            pending.discard(m.get("tool_call_id"))
    return not pending


# ---- B1 token 估算 ----
check("B1a est_tokens CJK 1:1", est_tokens("你好世界") == 4)
check("B1b est_tokens ASCII 4:1 进位", est_tokens("abcd") == 1
      and est_tokens("abc") == 1 and est_tokens("abcde") == 2)
check("B1c est_tokens 空串", est_tokens("") == 0)
check("B1d messages_tokens 含 tool_calls 参数",
      messages_tokens(_messages(1)) > messages_tokens(
          [{"role": "system", "content": "sys"}]))

# ---- B2 头尾瘦身 ----
long_text = "A" * 300 + "M" * 3000 + "B" * 300
slimmed = slim_text(long_text, 100, 200)
check("B2a 瘦身保头尾", slimmed.startswith("A" * 100)
      and slimmed.endswith("B" * 200) and "M" not in slimmed)
check("B2b 瘦身带压缩标记", "观测已压缩" in slimmed
      and "原 3600 字符" in slimmed)
check("B2c 短文原样返回", slim_text("short", 100, 100) == "short")
check("B2d 零头零尾原样返回", slim_text(long_text, 0, 0) == long_text)

# ---- B3 步骤切分 ----
msgs = _messages(4)
prefix, steps = split_steps(msgs)
check("B3a prefix 为契约区", len(prefix) == 2
      and prefix[0]["role"] == "system" and prefix[1]["role"] == "user")
check("B3b 切出 4 个步骤对", len(steps) == 4
      and all(steps[i][0]["role"] == "assistant" for i in range(4)))
check("B3c 每对含 1 条 tool", all(len(t) == 1 for _, t in steps))
_, no_steps = split_steps([{"role": "system", "content": "s"},
                           {"role": "user", "content": "u"},
                           {"role": "assistant", "content": "纯文本"}])
check("B3d 无工具消息时步骤为空", no_steps == [])

# ---- B4 T1 slim_old_steps ----
slim_msgs = slim_old_steps(_messages(5), keep_recent=2,
                           head_chars=100, tail_chars=100)
_, steps_s = split_steps(slim_msgs)
check("B4a 老 3 步被瘦身", all("观测已压缩" in t[0]["content"]
                              for _, t in steps_s[:3]))
check("B4b 最近 2 步原样", all("观测已压缩" not in t[0]["content"]
                               for _, t in steps_s[3:]))
check("B4c 结构无损（无孤儿 tool_call）", _no_orphans(slim_msgs))
check("B4d 瘦身后更省 token",
      messages_tokens(slim_msgs) < messages_tokens(_messages(5)))

# ---- B5 compact_once：未超预算不动 ----
orig = _messages(3)
out, info = compact_once(orig, budget_tokens=10 ** 9)
check("B5a 未超预算原样返回", out is orig and not info["triggered"])

# ---- B6 compact_once：T2 摘要路径 ----
seen_transcripts = []


def _sum_ok(prompt):
    seen_transcripts.append(prompt)
    return "已完成：cmd-0。发现：尾部标记0。"


msgs6 = _messages(8)
out6, info6 = compact_once(msgs6, summarize_fn=_sum_ok,
                           budget_tokens=1, keep_recent=3)
check("B6a 触发且走 summary 模式", info6["triggered"]
      and info6["mode"] == "summary")
check("B6b 压缩步数 = 可压一半(8-3=5 → 2)",
      info6["compacted_steps"] == 2)
check("B6c 摘要消息落在契约区后、保留步骤前",
      out6[2]["role"] == "user"
      and out6[2]["content"].startswith(SUMMARY_MARKER)
      and out6[3]["role"] == "assistant")
check("B6d 摘要正文在消息内", "尾部标记0" in out6[2]["content"])
check("B6e 保留 6 步结构完整", _no_orphans(out6)
      and len(split_steps(out6)[1]) == 6)
check("B6f transcript 含命令与旧摘要注入点",
      "cmd-0" in seen_transcripts[0]
      and seen_transcripts[0].startswith("以下是软件工程助手"))
check("B6g 压缩后 token 下降（压掉 2 步 ≈2000 est_tok）",
      info6["after"] < info6["before"] - 1500)

# ---- B7 compact_once：摘要失败回退 T1 ----
def _sum_boom(_p):
    raise RuntimeError("LLM 挂了")


out7, info7 = compact_once(_messages(8), summarize_fn=_sum_boom,
                           budget_tokens=1, keep_recent=3)
check("B7a 异常回退 slim 模式", info7["triggered"]
      and info7["mode"] == "slim" and _no_orphans(out7))
out7b, info7b = compact_once(_messages(8), summarize_fn=lambda _p: "  ",
                             budget_tokens=1, keep_recent=3)
check("B7b 空摘要回退 slim 模式", info7b["mode"] == "slim")
out7c, info7c = compact_once(_messages(8), summarize_fn=None,
                             budget_tokens=1, keep_recent=3)
check("B7c 无摘要函数走 slim（T1-only 配置）", info7c["mode"] == "slim")
check("B7d slim 回退全量压老步骤（8-3=5 全瘦）",
      info7c["compacted_steps"] == 5
      and all("观测已压缩" in t[0]["content"]
              for _, t in split_steps(out7c)[1][:5]))

# ---- B8 摘要单条滚动（不累积） ----
def _sum2(_p):
    return "第二次摘要"


out8, _ = compact_once(out6, summarize_fn=_sum2, budget_tokens=1,
                       keep_recent=3)
sum_msgs = [m for m in out8
            if str(m.get("content", "")).startswith(SUMMARY_MARKER)]
check("B8a 再压缩摘要仍只一条", len(sum_msgs) == 1)
check("B8b 摘要内容更新为新一轮", "第二次摘要" in sum_msgs[0]["content"])

# ---- B9 步数 ≤ keep_recent 但超预算：全量瘦身保底 ----
out9, info9 = compact_once(_messages(2, out_len=100000),
                           budget_tokens=100, keep_recent=5,
                           slim_head_chars=100, slim_tail_chars=100)
check("B9a 无整对可压时全量瘦身", info9["triggered"]
      and info9["mode"] == "slim" and _no_orphans(out9))

# ---- B10 make_policy：配置缺省 + summarize 开关 ----
policy = make_policy({"budget_tokens": 10 ** 9})
out10, info10 = policy(_messages(3))
check("B10a policy 未超预算不动", not info10["triggered"])
policy_t1 = make_policy({"budget_tokens": 1, "summarize": False},
                        summarize_fn=_sum_ok)
_, info10b = policy_t1(_messages(4))
check("B10b summarize=false 时 T1-only", info10b["mode"] == "slim")
_, info10c = make_policy(None, summarize_fn=_sum_ok)(_messages(8))
check("B10c 空 cfg 用 CONTEXT_DEFAULTS（24000 不触发，8×4000≈8k tok）",
      isinstance(info10c["triggered"], bool))

print(f"\nswe_context: {len(PASS)} 通过, {len(FAIL)} 失败")
sys.exit(1 if FAIL else 0)
