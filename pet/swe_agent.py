"""pet/swe_agent.py —— mini-swe agent 循环（v0.21 M2）。

对应 mini-swe-agent 的 ``agents/default.py``（约 100 行核心）：**线性 history**
（每步只 append，轨迹即喂给 LLM 的 messages）+ ``run(task)`` 里循环
``step() = query() + execute_actions()`` + step/wall/cost 上限 + submit 结束。

模型侧不走 litellm：直接用本项目 ``OpenAICompatibleClient.query_once`` 单次
调用原语 + 专用 registry（bash/submit）。步骤经 ``on_step(command, content)``
回调流式上报（M4 接到聊天面板）。
"""

from __future__ import annotations

import json
import logging
import time

log = logging.getLogger("pet")

SWE_SYSTEM_PROMPT = (
    "你是一个在受限沙箱工作区里执行命令行任务的软件工程助手。\n"
    "规则：\n"
    "1. 只通过 bash 工具执行命令；命令在受限工作区内运行，禁止提权、"
    "访问工作区之外、删除或破坏系统。\n"
    "2. 每步只发一条必要的命令；严格依据上一条命令的真实输出决定下一步，"
    "不要编造输出。\n"
    "3. 任务完成时调用 submit(answer=...) 提交最终答复（answer 为用户可读的"
    "结论）。\n"
    "4. 无法继续时也在 submit 里说明原因，不要空转。"
)


def _find_submit(tool_calls):
    """在 tool_calls 里找 submit 工具调用，返回其 args（无则 None）。"""
    for tc in tool_calls or []:
        fn = tc.get("function", {})
        if fn.get("name") == "submit":
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except (json.JSONDecodeError, TypeError):
                args = {}
            return args if isinstance(args, dict) else {}
    return None


def _format_result(res) -> str:
    """ToolResult → 回灌给 LLM 的观测文本（含退出码/失败标记）。"""
    if not res.success:
        return f"[工具失败] {res.message}"
    msg = res.message or "(无输出)"
    rc = res.data.get("returncode") if isinstance(res.data, dict) else None
    if rc:
        msg = f"{msg}\n[退出码 {rc}]"
    return msg


class SweAgent:
    """mini-swe 线性历史 agent。

    ``client`` 须提供 ``query_once(messages, tools, on_delta=None)``；
    ``registry`` 为注册了 bash/submit 的 ``ToolRegistry``。
    """

    def __init__(self, client, registry, *,
                 system_prompt: str = SWE_SYSTEM_PROMPT,
                 step_limit: int = 10, wall_time_s: float = 0.0,
                 ctx=None) -> None:
        self._client = client
        self._registry = registry
        self._system = system_prompt
        self._step_limit = int(step_limit) if step_limit and step_limit > 0 else 10
        self._wall_time_s = float(wall_time_s) if wall_time_s else 0.0
        self._ctx = ctx

    def run(self, task: str, on_step=None) -> dict:
        """执行任务，返回 ``{exit_status, submission, steps}``。

        exit_status ∈ Submitted / Finished / LimitsExceeded / TimeExceeded /
        Error。``on_step(command, content)`` 每条 bash 步骤回调一次。
        """
        messages = [
            {"role": "system", "content": self._system},
            {"role": "user", "content": task},
        ]
        start = time.monotonic()
        n_calls = 0
        last_text = ""
        while n_calls < self._step_limit:
            if self._wall_time_s and time.monotonic() - start >= self._wall_time_s:
                return {"exit_status": "TimeExceeded",
                        "submission": last_text or "（时间上限，未提交）",
                        "steps": n_calls}
            n_calls += 1
            try:
                text, tool_calls, _usage = self._client.query_once(
                    messages, self._registry.schemas())
            except Exception as exc:
                log.warning("swe 模型调用异常: %s", exc)
                return {"exit_status": "Error", "submission": str(exc),
                        "steps": n_calls}
            text = text or ""
            submit_args = _find_submit(tool_calls)
            if submit_args is not None:
                ans = (submit_args.get("answer") or "").strip() or text
                return {"exit_status": "Submitted", "submission": ans,
                        "steps": n_calls}
            if not tool_calls:
                return {"exit_status": "Finished", "submission": text,
                        "steps": n_calls}

            messages.append({"role": "assistant", "content": text,
                             "tool_calls": tool_calls})
            for tc in tool_calls:
                fn = tc.get("function", {})
                name = fn.get("name")
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except (json.JSONDecodeError, TypeError):
                    args = {}
                if not isinstance(args, dict):
                    args = {}
                tc_id = tc.get("id") or f"call_synth_{n_calls}_{len(messages)}"
                if name == "bash":
                    command = (args.get("command") or "").strip()
                    res = self._registry.dispatch("bash", args, self._ctx)
                    content = _format_result(res)
                    if on_step is not None:
                        try:
                            on_step(command, content)
                        except Exception:
                            log.warning("swe on_step 回调异常", exc_info=True)
                else:
                    content = f"[工具失败] 未知工具: {name}"
                messages.append({"role": "tool",
                                 "tool_call_id": tc_id, "content": content})
            last_text = text
        return {"exit_status": "LimitsExceeded",
                "submission": last_text or "（步数上限，未提交）",
                "steps": n_calls}
