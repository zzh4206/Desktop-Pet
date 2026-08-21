"""记忆 DS 工具（memory_save / memory_search）+ prompt 注入辅助。

共享层（无平台库）：工具 schema/handler 按 v0.4 框架写，app 注册到
ToolRegistry（加工具不改 llm.py）。recall→注入 system 由 app 在发消息
前调 ``memory_context``。
"""

from __future__ import annotations

import logging

from .memory import MemoryStore
from .tools_schema import ToolContext, ToolHandler, ToolResult, ToolSchema

_log = logging.getLogger("pet")

MEMORY_SAVE_SCHEMA = ToolSchema(
    name="memory_save",
    description=(
        "把关于用户的持久信息存入长期记忆（跨会话保留）。用于用户说出"
        "偏好/事实/约定时，如'我喜欢喝咖啡''我叫小明''每周五要交周报'。"
        "importance 0-1：日常偏好 0.5-0.7，姓名/重要约定 0.9-1.0。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "fact": {"type": "string",
                     "description": "要记住的事实，一句话，第三人称"},
            "importance": {"type": "number",
                           "description": "重要性 0-1"},
        },
        "required": ["fact", "importance"],
        "additionalProperties": False,
    },
    dangerous=False,
)

MEMORY_SEARCH_SCHEMA = ToolSchema(
    name="memory_search",
    description=(
        "检索长期记忆中与查询相关的条目（返回最多 k 条）。"
        "对话中需要回忆用户信息时调用。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "k": {"type": "number", "description": "返回条数，默认 5"},
        },
        "required": ["query"],
        "additionalProperties": False,
    },
    dangerous=False,
)


class _MemoryToolHandler:
    """闭包注入 store（ToolHandler 协议）。"""

    def __init__(self, store: MemoryStore, mode: str) -> None:
        self._store = store
        self._mode = mode

    def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        if self._mode == "save":
            fact = (args.get("fact") or "").strip()
            if not fact:
                return ToolResult(False, "fact 不能为空。")
            try:
                imp = float(args.get("importance", 0.5))
            except (TypeError, ValueError):
                imp = 0.5
            mid = self._store.memorize(fact, imp)
            return ToolResult(True, f"已记住（id={mid}）。",
                              data={"id": mid})
        # search
        query = (args.get("query") or "").strip()
        if not query:
            return ToolResult(False, "query 不能为空。")
        try:
            k = int(args.get("k", 5))
        except (TypeError, ValueError):
            k = 5
        hits = self._store.recall(query, k=min(k, 10))
        if not hits:
            return ToolResult(True, "没有相关记忆。")
        return ToolResult(
            True, "\n".join(f"- {h['fact']}" for h in hits),
            data={"hits": hits},
        )


def build_memory_tools(store: MemoryStore) -> list:
    """记忆工具清单（app 喂 ToolRegistry.register，与平台工具并列）。"""
    return [
        (MEMORY_SAVE_SCHEMA, _MemoryToolHandler(store, "save")),
        (MEMORY_SEARCH_SCHEMA, _MemoryToolHandler(store, "search")),
    ]


def memory_context(store: MemoryStore, query: str, k: int = 5) -> str:
    """发消息前调：recall→拼 system 注入段（无命中返空串）。

    拼进 system prompt 末尾（"关于用户的长期记忆"一节）——满足
    "后续会话正确 recall"的 Must：DS 无需调工具即可看到相关记忆。
    """
    hits = store.recall(query, k=k)
    if not hits:
        return ""
    lines = "\n".join(f"- {h['fact']}" for h in hits)
    return "\n\n关于用户的长期记忆（自动检索，可靠）：\n" + lines
