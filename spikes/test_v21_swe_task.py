# -*- coding: utf-8 -*-
"""test_v21_swe_task.py —— v0.21 mini-swe 集成 M4 swe_task 委托工具单测（纯 Python，无 Qt/无网络）。

覆盖 SweTaskHandler 结果规约（Submitted/Finished/LimitsExceeded/异常/空参数）
与 SWE_TASK_SCHEMA 元信息（危险确认 + instruction 豁免黑名单）。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pet.swe_tools import SWE_TASK_SCHEMA, SweTaskHandler

PASS, FAIL = [], []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("  ✅ " if cond else "  ❌ ") + name)


def _submitted(instruction):
    return {"exit_status": "Submitted", "submission": "完成了", "steps": 3}


def _finished(instruction):
    return {"exit_status": "Finished", "submission": "直接答复", "steps": 1}


def _limits(instruction):
    return {"exit_status": "LimitsExceeded", "submission": "（步数上限，未提交）",
            "steps": 10}


def _raise(instruction):
    raise RuntimeError("boom")


check("T1 Submitted → 成功并带 submission",
      SweTaskHandler(_submitted).execute({"instruction": "统计文件"}, None)
      .success)
r = SweTaskHandler(_submitted).execute({"instruction": "统计文件"}, None)
check("T1b submission 透传", r.message == "完成了"
      and r.data.get("exit_status") == "Submitted")

check("T2 Finished → 成功",
      SweTaskHandler(_finished).execute({"instruction": "x"}, None).success)

r2 = SweTaskHandler(_limits).execute({"instruction": "x"}, None)
check("T3 LimitsExceeded → 失败回灌", (not r2.success)
      and "步数上限" in r2.message)

r3 = SweTaskHandler(_raise).execute({"instruction": "x"}, None)
check("T4 runner 异常 → 失败", (not r3.success) and "boom" in r3.message)

r4 = SweTaskHandler(_submitted).execute({}, None)
check("T5 空 instruction → 失败", (not r4.success) and "instruction" in r4.message)

check("T6 schema 危险确认 + instruction 豁免黑名单",
      SWE_TASK_SCHEMA.dangerous is True
      and SWE_TASK_SCHEMA.text_fields == ("instruction",))

print(f"\nswe_task 委托工具: {len(PASS)} 通过, {len(FAIL)} 失败")
sys.exit(1 if FAIL else 0)
