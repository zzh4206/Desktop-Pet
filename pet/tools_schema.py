"""LLM 工具层 —— 接口冻结于 设计思路.md §2.2。

``ToolContext`` / ``ToolHandler`` / ``ToolRegistry`` 三件套：schema 注册 +
``dispatch`` 路由 + 参数黑名单校验 + 危险操作确认（经注入的 ``confirm_fn``
回调，**本模块零平台库**——NSAlert/Qt 对话框由 ``app.py`` 经 ``platform``
注入，不在此 import AppKit/QtWidgets）。

v0.4 只注册 ``open_app``（不危险，NSAlert 框架就位但不触发，见补遗#5）。
后续工具（v0.8 全套）只在此 register，不改 ``llm.py``（schema 与实现解耦）。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Callable, Optional, Protocol

log = logging.getLogger("pet")


@dataclass
class ToolResult:
    """工具执行结果。

    ``success=False`` 时，``llm._dispatch_tool_calls`` 在 ``message`` 前加
    ``[工具失败]`` 标记回灌 DS（让模型知道失败可决策重试/改道，v0.4.12 起真
    读取此字段；此前 success 被忽略，失败结果照常回灌误导 DS）。
    """

    success: bool
    message: str
    data: dict = field(default_factory=dict)


@dataclass
class ToolContext:
    """工具执行上下文（冻结于 §2.2）。v0.4 只用 ``config``/``user_name``。"""

    pet_state: object  # PetState；v0.4 不强类型以免环 import
    user_name: str
    config: dict
    window_info: Optional[dict] = None


class ToolHandler(Protocol):
    def execute(self, args: dict, ctx: ToolContext) -> ToolResult: ...


@dataclass
class ToolSchema:
    """工具元信息。``parameters`` 为 JSON Schema（喂 DS function calling）。"""

    name: str
    description: str
    parameters: dict
    dangerous: bool = False  # True→dispatch 先走 confirm_fn


# 参数黑名单（§五 prompt 注入防护）：路径根/越界/破坏性模式。
# 工具自行按字段名校验，这里给共享判定。
_TRAVERSAL = re.compile(r"(^/+$|\.\.|\.\.|rm\s+-rf|>\s*/dev/null)")
# 形如 ~、~root、/、// 的裸根路径
_BARE_ROOT = re.compile(r"^~/?$|^//?$|^/+$")


def _is_unsafe_path(value: str) -> bool:
    if not isinstance(value, str):
        return False
    if _BARE_ROOT.match(value):
        return True
    if _TRAVERSAL.search(value):
        return True
    return False


def _scan_args_unsafe(args: dict) -> Optional[str]:
    """扫所有字符串值是否命中黑名单；命中返回该值供报错。"""
    for v in args.values():
        if isinstance(v, str) and _is_unsafe_path(v):
            return v
    return None


ConfirmFn = Callable[[str, str, str], bool]
"""(title, command, risk) -> 是否继续。mac=NSAlert / win=Qt 对话框，经 app 注入。"""


class ToolRegistry:
    """工具注册表 + 路由。``schemas()`` 喂 DS，``dispatch`` 执行。

    签名冻结于 §2.2（``schemas``/``dispatch``）。``confirm_fn`` 为注入扩展点
    （v0.4 兼容扩展，非删改冻结签名）。
    """

    def __init__(self, confirm_fn: Optional[ConfirmFn] = None) -> None:
        self._handlers: dict[str, ToolHandler] = {}
        self._schemas: dict[str, ToolSchema] = {}
        self._confirm_fn = confirm_fn

    def register(self, schema: ToolSchema, handler: ToolHandler) -> None:
        self._schemas[schema.name] = schema
        self._handlers[schema.name] = handler

    def schemas(self) -> list[dict]:
        """OpenAI 兼容 function-calling 格式（deepseek-chat 同格式）。"""
        out = []
        for s in self._schemas.values():
            out.append(
                {
                    "type": "function",
                    "function": {
                        "name": s.name,
                        "description": s.description,
                        "parameters": s.parameters,
                    },
                }
            )
        return out

    def dispatch(self, name: str, args: dict, ctx: ToolContext) -> ToolResult:
        handler = self._handlers.get(name)
        if handler is None:
            return ToolResult(False, f"未知工具: {name}")
        schema = self._schemas[name]

        # 参数黑名单校验（注入防护）
        bad = _scan_args_unsafe(args or {})
        if bad is not None:
            log.warning("工具 %s 参数命中黑名单: %r", name, bad)
            return ToolResult(False, "参数包含不安全的路径或命令。")

        # 危险操作确认（v0.4 open_app 不危险，框架就位）
        if schema.dangerous and self._confirm_fn is not None:
            cmd_repr = f"{name}({args})"
            ok = self._confirm_fn("危险操作确认", cmd_repr, "该操作不可撤销。")
            if not ok:
                return ToolResult(False, "用户已取消。")

        try:
            return handler.execute(args or {}, ctx)
        except Exception as e:  # 工具异常不崩主链
            log.exception("工具 %s 执行异常", name)
            return ToolResult(False, f"工具执行失败: {e}")
