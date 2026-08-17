"""DeepSeek 客户端 + function calling + 流式 + 降级 —— 设计思路.md §五。

**平台库-free**：本模块只 import ``requests``（网络）+ ``PySide6.QtCore``
（ChatWorker 信号）。**不 import keyring/AppKit/Quartz**——DS key 经
``app.py`` 注入（``api_key`` 参数），危险确认经 ``ToolRegistry.confirm_fn``
注入，工具经 ``ToolRegistry`` 路由。

降级链（§五）：网络/超时/4xx/5xx/额度 → 预设回复 + 日志，不崩；
离线（``requests.ConnectionError``）→ 抛 ``OfflineError``，app 显示气泡
"当前离线"且宠物仍跑。

ChatWorker：DS 请求在 QThread 跑，流式 SSE delta 经 ``delta`` 信号逐字
回主线程，**不阻塞 _tick**（v0.3 物理 50ms）。``cancel()`` 中断流式
（关闭聊天面板时调，不泄漏线程，见 v0.4 测试 T15）。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Optional

import requests
from PySide6.QtCore import QThread, Signal, Slot

from .prompts import SYSTEM_PROMPT

log = logging.getLogger("pet")

DS_BASE_URL = "https://api.deepseek.com"
DS_MODEL = "deepseek-chat"
_TIMEOUT = (10, 120)  # (connect, read)；read 长，流式 chunk 间隔短

FALLBACK_REPLY = "我开小差了～一会儿再问我吧。"
OFFLINE_REPLY = "当前离线，聊天暂时不可用。"


class OfflineError(Exception):
    """无网络连接——app 应气泡提示且不阻塞宠物。"""


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    def add(self, u: dict) -> None:
        self.prompt_tokens += int(u.get("prompt_tokens", 0))
        self.completion_tokens += int(u.get("completion_tokens", 0))
        self.total_tokens += int(u.get("total_tokens", 0))


@dataclass
class ChatTurn:
    role: str  # user / assistant / tool
    content: str
    tool_call_id: str = ""
    tool_calls: list = field(default_factory=list)  # 仅 assistant 带原始 tool_calls

    def to_message(self) -> dict:
        """DS/OpenAI 消息格式：省略空字段（tool_call_id 仅 tool 角色有）。"""
        msg = {"role": self.role, "content": self.content}
        if self.role == "tool" and self.tool_call_id:
            msg["tool_call_id"] = self.tool_call_id
        if self.role == "assistant" and self.tool_calls:
            msg["tool_calls"] = self.tool_calls
        return msg


def _tool_result_message(tool_call_id: str, content: str) -> dict:
    return {"role": "tool", "tool_call_id": tool_call_id, "content": content}


class DeepSeekClient:
    """DS 客户端 + function calling + usage + 降级。

    ``run_stream``：单次 DS 调用（流式），返回 (text, tool_calls, usage)，
    并逐 delta 回调。``chat_once``：跑一轮 agent loop（DS→tool→DS 续回复），
    内部循环 ``_MAX_TOOL_ROUNDS`` 次封顶。
    """

    _MAX_TOOL_ROUNDS = 4

    def __init__(
        self,
        api_key: str,
        registry,
        base_url: str = DS_BASE_URL,
        model: str = DS_MODEL,
        system_prompt: str = SYSTEM_PROMPT,
        timeout=_TIMEOUT,
    ) -> None:
        self._api_key = api_key
        self._registry = registry
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._system = {"role": "system", "content": system_prompt}
        self._timeout = timeout
        self._resp = None  # 流式响应引用（cancel() 调 close 真中断用）
        self.usage = Usage()

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    def _payload(
        self, messages: list, tools: Optional[list], stream: bool
    ) -> dict:
        body = {
            "model": self._model,
            "messages": messages,
            "stream": stream,
        }
        if tools:
            body["tools"] = tools
        return body

    def _stream_once(
        self,
        messages: list,
        tools: Optional[list],
        on_delta=None,
    ) -> tuple[str, list, dict]:
        """单次流式请求。``on_delta(text)`` 逐 chunk 回调（增量文本）。

        返回 (full_text, tool_calls, usage_dict)。失败按 §五降级链处理。
        ``resp`` 提升到 ``self._resp``——``cancel()`` 可调 ``resp.close()`` 真中断
        流式 socket（旧版只置标志位，iter_lines 仍跑满 read 超时 120s）。
        """
        self._resp = requests.post(
            f"{self._base_url}/chat/completions",
            headers=self._headers(),
            json=self._payload(messages, tools, stream=True),
            timeout=self._timeout,
            stream=True,
        )
        # 4xx/5xx：额度/鉴权/服务端——降级预设回复
        if self._resp.status_code >= 400:
            body = ""
            try:
                body = self._resp.text[:200]
            except Exception:
                pass
            log.warning("DS HTTP %s: %s", self._resp.status_code, body)
            return FALLBACK_REPLY, [], {}

        full = []
        tool_calls: dict[int, dict] = {}  # index 聚合 DS 分片 tool_call
        usage = {}
        try:
            for line in self._resp.iter_lines(decode_unicode=True):
                if not line or not line.startswith("data:"):
                    continue
                data = line[len("data:") :].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                u = chunk.get("usage")
                if u:
                    usage = u
                for choice in chunk.get("choices", []):
                    delta = choice.get("delta", {})
                    content = delta.get("content")
                    if content:
                        full.append(content)
                        if on_delta:
                            on_delta(content)
                    for tc in delta.get("tool_calls", []) or []:
                        idx = tc.get("index", 0)
                        slot = tool_calls.setdefault(
                            idx, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
                        )
                        if tc.get("id"):
                            slot["id"] = tc["id"]
                        fn = tc.get("function", {})
                        if fn.get("name"):
                            slot["function"]["name"] = fn["name"]
                        if fn.get("arguments"):
                            slot["function"]["arguments"] += fn["arguments"]
        except requests.ConnectionError:
            # cancel() 调 resp.close() 会使 iter_lines 抛 ConnectionError
            raise OfflineError("流式中断（用户取消或连接断开）")
        finally:
            self._resp = None
        return "".join(full), list(tool_calls.values()), usage

    def _non_stream_once(
        self, messages: list, tools: Optional[list]
    ) -> tuple[str, list, dict]:
        """非流式（降级/工具结果回灌后续轮，无需逐字回显）。"""
        try:
            resp = requests.post(
                f"{self._base_url}/chat/completions",
                headers=self._headers(),
                json=self._payload(messages, tools, stream=False),
                timeout=self._timeout,
            )
        except requests.ConnectionError:
            # 离线：激活 OfflineError（run 的 except OfflineError 分支生效）
            raise OfflineError("非流式请求连接失败（疑似离线）")
        if resp.status_code >= 400:
            log.warning("DS HTTP %s: %s", resp.status_code, resp.text[:200])
            return FALLBACK_REPLY, [], {}
        try:
            data = resp.json()
            choices = data.get("choices") or []
            if not choices:
                log.warning("DS 返回无 choices: %s", str(data)[:200])
                return FALLBACK_REPLY, [], {}
            msg = choices[0].get("message") or {}
        except (ValueError, KeyError, TypeError) as e:
            log.warning("DS 响应解析失败: %s", e)
            return FALLBACK_REPLY, [], {}
        return (
            msg.get("content") or "",
            msg.get("tool_calls") or [],
            data.get("usage") or {},
        )

    def _dispatch_tool_calls(
        self, tool_calls: list, ctx
    ) -> list[dict]:
        """执行 DS 返的 tool_calls，回灌 tool 结果消息。

        失败工具（``res.success=False``）带 ``[工具失败]`` 标记回灌——让 DS
        知道失败可自行决策重试/改道，而非当作正常结果（v0.4.12 前 success 字段
        被忽略，失败结果照常回灌误导 DS）。
        """
        results = []
        for tc in tool_calls:
            tc_id = tc.get("id", "")
            if not tc_id:
                # 流式首轮分片未收到 id——跳过，避免回灌空 tool_call_id 致续轮报错
                log.warning("DS tool_call 无 id，跳过: %s", tc)
                continue
            name = tc["function"]["name"]
            try:
                args = json.loads(tc["function"]["arguments"] or "{}")
            except json.JSONDecodeError:
                args = {}
            if log.isEnabledFor(logging.INFO):
                log.info("DS 工具调用 %s args=%s", name, args)
            res = self._registry.dispatch(name, args, ctx)
            content = res.message
            if not getattr(res, "success", True):
                content = f"[工具失败] {res.message}"
            results.append(_tool_result_message(tc_id, content))
        return results

    def chat_once(
        self,
        history: list[ChatTurn],
        ctx,
        on_delta=None,
    ) -> tuple[str, list[ChatTurn]]:
        """跑一轮 agent loop：DS→（tool_calls→dispatch→回灌→DS 续）→最终回复。

        ``history`` 含到本轮为止的完整上下文（含本轮 user）。返回
        (assistant_text, appended_turns)——app 把 appended_turns 追加进历史。
        首轮流式回显（``on_delta``）；工具结果回灌后续轮非流式（避免重复刷屏）。
        """
        messages = [self._system] + [t.to_message() for t in history]
        tools = self._registry.schemas() or None
        appended: list[ChatTurn] = []

        text, tool_calls, usage = self._stream_once(messages, tools, on_delta)
        self.usage.add(usage)
        appended.append(
            ChatTurn("assistant", text, tool_calls=list(tool_calls))
        )
        rounds = 0
        while tool_calls and rounds < self._MAX_TOOL_ROUNDS:
            rounds += 1
            # 把 assistant 的 tool_calls 进消息（DS 要求 tool_calls 在 assistant 消息上）
            messages.append(
                {"role": "assistant", "content": text, "tool_calls": tool_calls}
            )
            # dispatch + 回灌 tool 结果
            tool_msgs = self._dispatch_tool_calls(tool_calls, ctx)
            for tm in tool_msgs:
                messages.append(tm)
                appended.append(ChatTurn("tool", tm["content"], tm["tool_call_id"]))
            # 续回复（非流式——最终文本仍走 on_delta 一次性回显）
            text2, tool_calls, usage = self._non_stream_once(messages, tools)
            self.usage.add(usage)
            if text2 and on_delta:
                on_delta(text2)
            if text2:
                # 触顶（rounds 达上限）时末轮仍带 tool_calls——清空避免
                # 下次 user 消息喂到"assistant 带 tool_calls 无 tool 结果"致 DS 报错
                final_tc = list(tool_calls) if rounds < self._MAX_TOOL_ROUNDS else []
                appended.append(
                    ChatTurn("assistant", text2, tool_calls=final_tc)
                )
                text = text2
        return text, appended


class ChatWorker(QThread):
    """DS 请求后台线程——流式 delta 经信号回主线程，不阻塞 _tick。

    信号：``delta``（增量文本）/``tool_started``(name)/``done``(完整回合追加
    轮次列表)/``offline``/``failed``(降级预设回复文本)。
    ``cancel()`` 中断流式（关面板调，见 T15）。
    """

    delta = Signal(str)
    tool_started = Signal(str)
    done = Signal(object)  # list[ChatTurn]
    offline = Signal()
    failed = Signal(str)  # 降级回复文本

    def __init__(
        self,
        client: DeepSeekClient,
        history: list[ChatTurn],
        user_text: str,
        ctx,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._client = client
        self._history = list(history)
        self._user_text = user_text
        self._ctx = ctx
        self._cancelled = False

    def cancel(self) -> None:
        """中断流式：置标志 + 关闭底层 resp socket（iter_lines 抛 ConnectionError
        → OfflineError 退出），而非旧版只置标志让流式跑满 120s read 超时。"""
        self._cancelled = True
        # 关闭流式响应 socket——_stream_once 的 iter_lines 会抛 ConnectionError
        try:
            if self._client._resp is not None:
                self._client._resp.close()
        except Exception:
            pass

    @Slot()
    def run(self) -> None:  # noqa: C901 - 降级分支多
        self._history.append(ChatTurn("user", self._user_text))
        try:
            text, appended = self._client.chat_once(
                self._history, self._ctx, on_delta=self._emit_delta
            )
        except OfflineError:
            # cancel() 关 resp 触发的 ConnectionError→OfflineError 不算离线
            if not self._cancelled:
                self.offline.emit()
            return
        except requests.Timeout:
            log.warning("DS 请求超时，降级预设回复")
            self.delta.emit(FALLBACK_REPLY)
            self.failed.emit(FALLBACK_REPLY)
            return
        except requests.ConnectionError as exc:
            log.warning("DS 连接失败（疑似离线）: %s", exc)
            self.offline.emit()
            return
        except Exception as exc:  # 任何未预期错误降级，不崩
            log.exception("DS 调用异常，降级")
            self.delta.emit(FALLBACK_REPLY)
            self.failed.emit(FALLBACK_REPLY)
            return
        if self._cancelled:
            return
        # 把这轮 user 追加进 appended 头部，app 一次性接回完整历史
        head = [ChatTurn("user", self._user_text)]
        self.done.emit(head + appended)

    def _emit_delta(self, text: str) -> None:
        if not self._cancelled:
            self.delta.emit(text)
