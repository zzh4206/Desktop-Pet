# -*- coding: utf-8 -*-
"""test_v22_swe_agent_ctx.py —— v0.22 SweAgent 接入 context_policy 单测（无 Qt/无网络）。

覆盖：policy 注入后循环正常（submit 轮不跑 policy）/ UI 回显不受压缩影响 /
策略异常 fail-open 不杀任务 / 大观测任务端到端压在预算内（sizes 有下降拐点）。
"""

import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pet.swe_context import make_policy, messages_tokens
from pet.swe_agent import SweAgent
from pet.swe_env import SweEnvironment
from pet.swe_tools import build_swe_tools
from pet.tools_schema import ToolRegistry

PASS, FAIL = [], []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("  ✅ " if cond else "  ❌ ") + name)


# ---- C1 SweAgent 接线：policy 注入后循环正常 ----
def _bash_tc(cmd, idx):
    return {"id": f"call_{idx}", "type": "function",
            "function": {"name": "bash",
                         "arguments": json.dumps({"command": cmd})}}


def _submit_tc(answer, idx):
    return {"id": f"call_{idx}", "type": "function",
            "function": {"name": "submit",
                         "arguments": json.dumps({"answer": answer})}}


class ScriptedClient:
    def __init__(self, script):
        self.script = list(script)
        self.calls = 0
        self.seen_sizes = []

    def query_once(self, messages, tools=None, on_delta=None):
        self.seen_sizes.append(messages_tokens(messages))
        i = min(self.calls, len(self.script) - 1)
        self.calls += 1
        return self.script[i]


ws = tempfile.mkdtemp(prefix="swe_ctx_test_")
env = SweEnvironment(ws, confirm_fn=lambda *a: True)
reg = ToolRegistry()
for schema, handler in build_swe_tools(env):
    reg.register(schema, handler)

policy_calls = []


def _spy_policy(messages):
    policy_calls.append(len(messages))
    return compact_once(messages, budget_tokens=10 ** 9)


client_c1 = ScriptedClient([
    ("", [_bash_tc("echo step-one", 0)], {}),
    ("", [_bash_tc("echo step-two", 1)], {}),
    ("", [_submit_tc("完成", 2)], {}),
])
agent_c1 = SweAgent(client_c1, reg, context_policy=_spy_policy)
steps_c1 = []
res_c1 = agent_c1.run("任务", on_step=lambda c, o: steps_c1.append((c, o)))
check("C1a policy 注入后任务正常提交", res_c1["exit_status"] == "Submitted")
check("C1b UI 回显不受压缩影响（全量两步）",
      len(steps_c1) == 2 and steps_c1[0][0] == "echo step-one")
check("C1c 每轮观测落定后策略各跑一次（submit 轮不跑）",
      policy_calls == [4, 6])

# ---- C2 策略抛异常不杀任务（fail-open） ----
def _boom_policy(_messages):
    raise RuntimeError("策略炸了")


client_c2 = ScriptedClient([
    ("", [_bash_tc("echo ok", 0)], {}),
    ("", [_submit_tc("完成", 1)], {}),
])
agent_c2 = SweAgent(client_c2, reg, context_policy=_boom_policy)
res_c2 = agent_c2.run("任务")
check("C2 策略异常 fail-open 任务继续",
      res_c2["exit_status"] == "Submitted" and res_c2["submission"] == "完成")

# ---- C3 大观测任务端到端：预算内运行 ----
# 贴真实形态：命令短、输出大（seq 1200 → ~6900 字符 ≈ 1700 est_tok/步）。


class BigClient:
    """前 5 步查大列表，第 6 步提交；记录每轮喂 LLM 的估算 token。"""

    def __init__(self):
        self.n = 0
        self.sizes = []

    def query_once(self, messages, tools=None, on_delta=None):
        self.sizes.append(messages_tokens(messages))
        self.n += 1
        if self.n <= 5:
            return "", [_bash_tc("seq 1200", self.n)], {}
        return "", [_submit_tc("大任务完成", self.n)], {}


client_c3 = BigClient()
agent_c3 = SweAgent(client_c3, reg, step_limit=20,
                    context_policy=make_policy(
                        {"budget_tokens": 6000, "keep_recent": 2,
                         "summarize": False}))
res_c3 = agent_c3.run("大输出任务")
check("C3a 大观测任务正常提交", res_c3["exit_status"] == "Submitted")
check("C3b 喂 LLM 的 token 被压在预算附近（≤1.2×6000）",
      max(client_c3.sizes[1:]) <= 7200)
check("C3c 压缩确实发生过（sizes 出现下降拐点）",
      any(client_c3.sizes[i + 1] < client_c3.sizes[i]
          for i in range(len(client_c3.sizes) - 1)))

shutil.rmtree(ws, ignore_errors=True)
print(f"\nswe_agent_ctx: {len(PASS)} 通过, {len(FAIL)} 失败")
sys.exit(1 if FAIL else 0)
