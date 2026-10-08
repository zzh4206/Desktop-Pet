"""pet/swe_tools.py —— mini-swe 专用工具（bash / submit）（v0.21 M2）。

注册在 **SWE 专用 registry**（非宠物主聊天 registry，见 ``swe_agent.py``）。
``bash`` 包装 ``SweEnvironment`` 沙箱执行（危险判定/确认由 env 内部完成，
故本工具在 ToolRegistry 层标 ``dangerous=False`` 且把 ``command`` 列为
``text_fields`` 豁免——避免被 registry 的路径/命令黑名单二次误伤，沙箱才是
唯一权威）；``submit`` 让模型显式结束任务（用 function-calling 而非 mini-swe
原版的 echo 哨兵）。
"""

from __future__ import annotations

from .tools_schema import ToolContext, ToolHandler, ToolResult, ToolSchema

BASH_SCHEMA = ToolSchema(
    name="bash",
    description=(
        "在受限沙箱工作区内执行一条 shell 命令，返回标准输出/错误与退出码。"
        "命令不得越出工作区、不得提权或破坏系统；根据上一步输出决定下一步。"
        "每次只提交一条命令。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "command": {"type": "string",
                        "description": "要执行的 shell 命令（单条）"},
        },
        "required": ["command"],
        "additionalProperties": False,
    },
    dangerous=False,
    # 命令文本交由 SweEnvironment 统一扫描（黑名单/穿越/危险确认），
    # 豁免 ToolRegistry 的通用路径黑名单，避免双权威冲突。
    text_fields=("command",),
)

SUBMIT_SCHEMA = ToolSchema(
    name="submit",
    description=(
        "任务完成时调用：提交最终答复（用户可读的结论）。调用后任务即结束。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "answer": {"type": "string", "description": "最终答复正文"},
        },
        "required": ["answer"],
        "additionalProperties": False,
    },
    dangerous=False,
)


class BashHandler:
    """``bash`` 工具：委托 ``SweEnvironment.execute`` 沙箱执行。"""

    def __init__(self, env) -> None:
        self._env = env

    def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        command = (args.get("command") or "").strip()
        if not command:
            return ToolResult(False, "需要 command 参数。")
        r = self._env.execute(command)
        if r.get("blocked"):
            return ToolResult(False, r.get("reason") or "命令被拦截")
        out = (r.get("output") or "").strip()
        if r.get("exception_info"):
            out = f"{out}\n{r['exception_info']}".strip()
        if not out:
            out = "(无输出)"
        return ToolResult(True, out, data={
            "returncode": r.get("returncode"),
            "truncated": r.get("truncated", False),
        })


class SubmitHandler:
    """``submit`` 工具：返回提交内容（SweAgent 循环会在 dispatch 前拦截
    submit 工具调用，此 handler 仅作兜底，正常不触发）。"""

    def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        answer = (args.get("answer") or "").strip()
        return ToolResult(True, answer or "（已提交）",
                          data={"submitted": True, "answer": answer})


def build_swe_tools(env) -> list:
    """SWE 专用工具注册清单：[(schema, handler), ...]。"""
    return [
        (BASH_SCHEMA, BashHandler(env)),
        (SUBMIT_SCHEMA, SubmitHandler()),
    ]


# ---- 主聊天 registry 的委托工具：swe_task（v0.21 M4）----
SWE_TASK_SCHEMA = ToolSchema(
    name="swe_task",
    description=(
        "委托一个软件工程助手在沙箱工作区执行命令行任务（写脚本、查文件、"
        "跑命令、统计目录、操作文件等），并把最终结果返回给你转述给用户。"
        "用于用户提出需要操作终端/命令行/代码的请求。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "instruction": {"type": "string",
                            "description": "要完成的任务描述（越具体越好）"},
        },
        "required": ["instruction"],
        "additionalProperties": False,
    },
    dangerous=True,
    # instruction 是自由任务描述（可含路径等），豁免 ToolRegistry 通用黑名单；
    # 真正的命令安全由子代理的 SweEnvironment 统一把关。
    text_fields=("instruction",),
)


class SweTaskHandler:
    """主聊天 registry 的 ``swe_task`` 工具：委托 mini-swe agent 跑任务。

    ``runner(instruction) -> dict``（SweAgent.run 的返回）；本 handler 只做
    结果规约（exit_status → ToolResult），不 import Qt / LLM。
    """

    def __init__(self, runner) -> None:
        self._runner = runner

    def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        instruction = (args.get("instruction") or "").strip()
        if not instruction:
            return ToolResult(False, "需要 instruction 参数。")
        try:
            result = self._runner(instruction)
        except Exception as exc:
            return ToolResult(False, f"任务执行异常: {exc}")
        if not isinstance(result, dict):
            return ToolResult(False, "任务返回格式异常")
        status = result.get("exit_status", "Error")
        submission = (result.get("submission") or "").strip()
        if status in ("Submitted", "Finished"):
            return ToolResult(True, submission or "（任务完成，无额外说明）",
                              data={"exit_status": status})
        # 上限/超时/错误：标记失败回灌主 LLM，让它向用户如实转述
        return ToolResult(False, submission or f"任务未完成（{status}）",
                          data={"exit_status": status})
