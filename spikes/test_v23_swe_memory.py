# -*- coding: utf-8 -*-
"""test_v23_swe_memory.py —— v0.22.1 mini-swe 本地任务记忆单测（无 Qt/无网络）。

覆盖 pet/swe_memory.py：档案 append/上限/坏行容错、IDF 相似召回与
零相关不注入、前缀拼装截断、NOTES.md 头部读取、compose_task 装配
与 disabled 开关、记忆前缀不回写档案（防自噬）。
"""

import json
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pet.swe_memory import (
    NOTES_FILENAME, TaskArchive, build_memory_prefix, build_notes_prefix,
    compose_task, read_notes_head,
)

PASS, FAIL = [], []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("  ✅ " if cond else "  ❌ ") + name)


ws = tempfile.mkdtemp(prefix="swe_mem_test_")

# ---- D1 档案 append / all ----
arc = TaskArchive(ws, history_cap=3)
arc.append("统计目录文件数", "Submitted", "共 42 个文件", steps=2)
arc.append("写一个重命名脚本", "Submitted", "脚本已写入 rename.py", steps=5)
check("D1a 留痕两条", len(arc.all()) == 2)
check("D1b 字段完整", arc.all()[0]["exit_status"] == "Submitted"
      and arc.all()[0]["steps"] == 2
      and isinstance(arc.all()[0]["at"], float))

# ---- D2 上限丢最老 ----
arc.append("第三个任务", "Finished", "结果三", steps=1)
arc.append("第四个任务", "Error", "挂了", steps=9)
check("D2 上限 3 丢最老", len(arc.all()) == 3
      and arc.all()[0]["instruction"] == "写一个重命名脚本")

# ---- D3 坏行容错 ----
with open(arc.path, "a", encoding="utf-8") as f:
    f.write("{这不是json\n\n")
check("D3 坏行跳过不炸", len(arc.all()) == 3)

# ---- D4 相似召回 ----
arc2 = TaskArchive(tempfile.mkdtemp(prefix="swe_mem_test2_"))
arc2.append("统计目录里的文件数量", "Submitted",
            "共找到 42 个文件，其中 3 个是脚本，统计口径按扩展名过滤")
arc2.append("重命名一批图片文件", "Submitted", "完成")
arc2.append("查询天气接口", "Error", "无网络")
hits = arc2.recall("帮我统计一下目录里有多少文件", k=2)
check("D4a 相似任务排第一", len(hits) >= 1
      and "统计" in hits[0]["instruction"])
hits_zero = arc2.recall("讲个笑话", k=2)
check("D4b 零相关不返回记录", hits_zero == [])
check("D4c k 截断", len(arc2.recall("文件 任务 目录 统计 重命名", k=1)) == 1)

# ---- D5 近期加分 ----
old_ws = tempfile.mkdtemp(prefix="swe_mem_test3_")
arc3 = TaskArchive(old_ws)
now = time.time()
arc3.append("文件统计任务A", "Submitted", "A", at=now - 40 * 86400)
arc3.append("文件统计任务B", "Submitted", "B", at=now)
hits3 = arc3.recall("文件统计", k=2)
check("D5 同相似度近期优先", hits3[0]["submission"] == "B")

# ---- D6 前缀拼装 ----
recs = arc2.recall("统计目录文件数量", k=5)
prefix = build_memory_prefix(recs, recall_chars=10)
check("D6a 前缀含标记与序号", prefix.startswith("[工作区近期任务记录]")
      and "\n1. [Submitted]" in prefix)
check("D6b submission 截断带省略号", "…" in prefix)
check("D6c 空记录不注入", build_memory_prefix([]) == "")

# ---- D7 NOTES.md ----
check("D7a 缺失返回空", read_notes_head(ws) == "")
notes_path = os.path.join(ws, NOTES_FILENAME)
with open(notes_path, "w", encoding="utf-8") as f:
    f.write("# 工作区笔记\n- python3 在 .venv\n" + "x" * 5000)
head = read_notes_head(ws, max_chars=100)
check("D7b 头部截断", len(head) <= 101 and head.startswith("# 工作区笔记"))
np = build_notes_prefix(ws, max_chars=100)
check("D7c 前缀带标记", np.startswith(f"[工作区笔记 {NOTES_FILENAME} 头部]"))

# ---- D8 compose_task 装配 ----
task = compose_task("统计目录文件数量", arc2.workspace, {"recall_k": 2,
                                                        "recall_chars": 50})
check("D8a 任务文本含原始指令", task.startswith("统计目录文件数量"))
check("D8b 注入记忆前缀", "[工作区近期任务记录]" in task
      and "42 个" in task)
task_off = compose_task("统计目录文件数量", arc2.workspace,
                        {"enabled": False})
check("D8c disabled 不注入", task_off == "统计目录文件数量")
task_no_hit = compose_task("完全无关的新鲜任务词汇", arc2.workspace, {})
check("D8d 零相关时仅剩任务文本（无空标记）",
      "[工作区近期任务记录]" not in task_no_hit)

# ---- D9 归档契约：存原始 instruction 而非 compose 后文本（防自噬） ----
ws9 = tempfile.mkdtemp(prefix="swe_mem_test4_")
arc9 = TaskArchive(ws9)
arc9.append("原始任务一", "Submitted", "结论一")
instruction = "原始任务一"
t9 = compose_task(instruction, ws9, {"recall_chars": 50})
check("D9a compose 文本确实带注入（前因）",
      "[工作区近期任务记录]" in t9)
arc9.append(instruction, "Submitted", "结论二")  # app 契约：存原始
recs9 = arc9.all()
check("D9b 档案条目无注入膨胀文本",
      len(recs9) == 2
      and "[工作区近期任务记录]" not in recs9[1]["instruction"])

# ---- D10 jsonl 人类可读（ensure_ascii=False 单行一条） ----
with open(arc2.path, encoding="utf-8") as f:
    lines = [l for l in f.read().splitlines() if l.strip()]
check("D10 每行一条合法 JSON", len(lines) == 3
      and all(json.loads(l) for l in lines))

for d in (ws, arc2.workspace, old_ws, ws9):
    shutil.rmtree(d, ignore_errors=True)
# ---- E1 端到端闭环：跑任务 → 归档 → 下次任务带记忆起跑（模拟 app._run）----
from pet.swe_agent import SweAgent
from pet.swe_env import SweEnvironment
from pet.swe_tools import build_swe_tools
from pet.tools_schema import ToolRegistry


def _bash_tc(cmd, idx):
    return {"id": f"call_{idx}", "type": "function",
            "function": {"name": "bash",
                         "arguments": json.dumps({"command": cmd})}}


def _submit_tc(answer, idx):
    return {"id": f"call_{idx}", "type": "function",
            "function": {"name": "submit",
                         "arguments": json.dumps({"answer": answer})}}


class TaskClient:
    """记录收到的任务文本；bash 一步后 submit。"""

    def __init__(self, bash_cmd="echo hi"):
        self.cmd = bash_cmd
        self.seen_tasks = []
        self.n = 0

    def query_once(self, messages, tools=None, on_delta=None):
        if self.n == 0:
            self.seen_tasks.append(messages[1]["content"])
        self.n += 1
        if self.n == 1:
            return "", [_bash_tc(self.cmd, 0)], {}
        return "", [_submit_tc("任务完成结论", 1)], {}


ws_e = tempfile.mkdtemp(prefix="swe_mem_e2e_")
env_e = SweEnvironment(ws_e, confirm_fn=lambda *a: True)
reg_e = ToolRegistry()
for _sch, _h in build_swe_tools(env_e):
    reg_e.register(_sch, _h)
agent_e = SweAgent(TaskClient(), reg_e)


def _run_like_app(agent, archive, instruction):
    """复刻 app._run 契约：compose 起跑 / 归档原始 instruction。"""
    task = compose_task(instruction, archive.workspace, {})
    result = agent.run(task)
    archive.append(instruction, result["exit_status"],
                    result["submission"], steps=result["steps"])
    return result


arc_e = TaskArchive(ws_e)
client_e = TaskClient("seq 5")
agent_e = SweAgent(client_e, reg_e)
_run_like_app(agent_e, arc_e, "统计目录里的文件数量")   # 第一次：无记忆
check("E1a 首次任务无记忆前缀",
      "[工作区近期任务记录]" not in client_e.seen_tasks[0])

client_e2 = TaskClient("echo again")
agent_e2 = SweAgent(client_e2, reg_e)
_run_like_app(agent_e2, arc_e, "再统计一次目录文件数量")  # 第二次：应带记忆
check("E1b 二次相似任务带记忆前缀",
      "[工作区近期任务记录]" in client_e2.seen_tasks[0]
      and "任务完成结论" in client_e2.seen_tasks[0])
check("E1c 前缀含上次结论与状态",
      "[Submitted]" in client_e2.seen_tasks[0])
recs_e = arc_e.all()
check("E1d 档案两条均为原始 instruction（无自噬）",
      len(recs_e) == 2
      and all("[工作区近期任务记录]" not in r["instruction"]
              for r in recs_e))

shutil.rmtree(ws_e, ignore_errors=True)
print(f"\nswe_memory: {len(PASS)} 通过, {len(FAIL)} 失败")
sys.exit(1 if FAIL else 0)
