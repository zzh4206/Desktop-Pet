# -*- coding: utf-8 -*-
"""test_v21_swe_agent.py —— v0.21 mini-swe 集成 M2 agent 循环单测（纯 Python，无 Qt/无网络）。

用脚本化假 client（不碰真实 LLM）驱动 SweAgent 循环，覆盖：多步循环 +
submit 结束 / 无工具直接结束 / step_limit / wall_time（注入假时钟）/ 危险命令
经 SweEnvironment 拦截后循环继续。
"""

import json
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pet.swe_agent import SweAgent
from pet.swe_env import SweEnvironment
from pet.swe_tools import build_swe_tools
from pet.tools_schema import ToolRegistry

PASS, FAIL = [], []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("  ✅ " if cond else "  ❌ ") + name)


def _bash_tc(cmd, idx):
    return {"id": f"call_{idx}", "type": "function",
            "function": {"name": "bash",
                         "arguments": json.dumps({"command": cmd})}}


def _submit_tc(answer, idx):
    return {"id": f"call_{idx}", "type": "function",
            "function": {"name": "submit",
                         "arguments": json.dumps({"answer": answer})}}


class ScriptedClient:
    """按脚本逐次返回 (text, tool_calls, usage)；超出后重复最后一条。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def query_once(self, messages, tools=None, on_delta=None):
        i = min(self.calls, len(self.script) - 1)
        self.calls += 1
        return self.script[i]


class RepeatClient:
    """永远返回 bash 命令（测 step_limit）。"""

    def query_once(self, messages, tools=None, on_delta=None):
        return "", [_bash_tc("echo x", 0)], {}


class ClockClient:
    """每次调用推进假时钟（测 wall_time）。"""

    def __init__(self, clock):
        self.clock = clock
        self.calls = 0

    def query_once(self, messages, tools=None, on_delta=None):
        self.calls += 1
        self.clock[0] += 10.0
        return "", [_bash_tc("echo x", 0)], {}


def _build_agent(client, env, **kw):
    reg = ToolRegistry()
    for schema, handler in build_swe_tools(env):
        reg.register(schema, handler)
    return SweAgent(client, reg, **kw)


ws = tempfile.mkdtemp(prefix="swe_agent_test_")
env = SweEnvironment(ws, confirm_fn=lambda *a: True)

# ---- A1 多步循环 + submit ----
client1 = ScriptedClient([
    ("", [_bash_tc("echo hello", 0)], {}),
    ("", [_bash_tc("pwd", 1)], {}),
    ("收尾", [_submit_tc("任务完成", 2)], {}),
])
agent1 = _build_agent(client1, env)
steps1 = []
res1 = agent1.run("测试任务", on_step=lambda c, o: steps1.append((c, o)))
check("A1a 多步循环 + submit 结束", res1["exit_status"] == "Submitted")
check("A1b submission 正确", res1["submission"] == "任务完成")
check("A1c 步数正确", res1["steps"] == 3)
check("A1d on_step 收集两步", len(steps1) == 2)
check("A1e 第1步命令与输出",
      steps1[0][0] == "echo hello" and steps1[0][1] == "hello")
check("A1f 第2步 pwd 落在工作区",
      steps1[1][0] == "pwd" and steps1[1][1] == env.workspace)

# ---- A2 无工具调用直接结束 ----
client2 = ScriptedClient([("直接答复文本", [], {})])
agent2 = _build_agent(client2, env)
res2 = agent2.run("说句话")
check("A2 无工具调用直接结束",
      res2["exit_status"] == "Finished" and res2["submission"] == "直接答复文本")

# ---- A3 step_limit 封顶 ----
agent3 = _build_agent(RepeatClient(), env, step_limit=3)
res3 = agent3.run("无限循环")
check("A3 step_limit 封顶",
      res3["exit_status"] == "LimitsExceeded" and res3["steps"] == 3)

# ---- A4 wall_time 封顶（注入假时钟）----
clock = [0.0]
agent4 = _build_agent(ClockClient(clock), env, step_limit=100, wall_time_s=25.0)
_real_mono = time.monotonic
time.monotonic = lambda: clock[0]
try:
    res4 = agent4.run("耗时任务")
finally:
    time.monotonic = _real_mono
check("A4 wall_time 封顶",
      res4["exit_status"] == "TimeExceeded" and res4["steps"] == 3)

# ---- A5 危险命令经 env 拦截后循环继续 ----
env5 = SweEnvironment(ws, confirm_fn=lambda *a: False)
client5 = ScriptedClient([
    ("", [_bash_tc("rm foo.txt", 0)], {}),
    ("", [_submit_tc("已尝试", 1)], {}),
])
agent5 = _build_agent(client5, env5)
steps5 = []
res5 = agent5.run("危险任务", on_step=lambda c, o: steps5.append((c, o)))
check("A5a 危险命令被拦截但循环继续",
      res5["exit_status"] == "Submitted" and res5["submission"] == "已尝试")
check("A5b 拦截原因上报", len(steps5) == 1 and steps5[0][0] == "rm foo.txt"
      and "用户取消" in steps5[0][1])

shutil.rmtree(ws, ignore_errors=True)
print(f"\nswe_agent 循环: {len(PASS)} 通过, {len(FAIL)} 失败")
sys.exit(1 if FAIL else 0)
