"""win 平台工具实现 —— 平台适配与分工.md §六工具映射。

样板 ``open_app``：``os.startfile`` 开程序/网页（ShellExecute，注册的应用
名如 notepad/calc 或完整 URL 均可）。按 §2.2 冻结的
``ToolHandler.execute(args, ctx) -> ToolResult`` 写，由 ``ToolRegistry``
注册（``build_win_tools``），不 import 共享层。

危险操作确认不经此层（``dispatch`` 走注入的 confirm_fn）。
open_app 不危险（v0.4 不触发确认框，框架就位）。
"""

from __future__ import annotations

import logging
import os
import re

from .tools_schema import ToolContext, ToolHandler, ToolResult, ToolSchema

log = logging.getLogger("pet")

# 程序名：字母数字空格点横点，禁路径分隔符（防 "../../xxx" 越界）
# v0.8.1：禁纯点号串（".." "." "..."，防 os.startfile("..") 弹上级目录 Explorer）
_APP_NAME = re.compile(r"^(?![.\s]+$)[A-Za-z0-9 .\-]+$")
# URL：仅 http/https，防 file:// 等本机协议
_HTTP_URL = re.compile(r"^https?://", re.IGNORECASE)

OPEN_APP_SCHEMA = ToolSchema(
    name="open_app",
    description=(
        "打开一个 Windows 应用程序或网页。用于满足用户'打开记事本'"
        "'打开浏览器'等请求。传 app（应用名，如 notepad、calc）或"
        " url（http(s) 网址）。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "app": {
                "type": "string",
                "description": "应用程序名，如 notepad、calc、explorer",
            },
            "url": {
                "type": "string",
                "description": "要打开的 http(s) 网址",
            },
        },
        "additionalProperties": False,
    },
    dangerous=False,
)


class OpenAppHandler:
    """``os.startfile``（ShellExecute：注册应用名 / URL / 文档均可）。"""

    def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        app = (args.get("app") or "").strip()
        url = (args.get("url") or "").strip()
        if app and url:
            return ToolResult(False, "app 与 url 二选一，不要同时给。")
        if app:
            if not _APP_NAME.match(app):
                return ToolResult(False, f"应用名不合法: {app!r}")
            target = app
        elif url:
            if not _HTTP_URL.match(url):
                return ToolResult(False, "仅支持 http/https 网址。")
            target = url
        else:
            return ToolResult(False, "需要 app 或 url 参数。")

        try:
            os.startfile(target)  # noqa: S606（startfile 本就执行目标，已过白名单）
        except OSError as e:
            log.warning("open_app 失败 %s: %s", target, e)
            return ToolResult(
                False,
                f"打开 {target} 失败: {e}（未注册的应用名？）",
            )
        log.info("open_app 打开 %s", target)
        return ToolResult(True, f"已打开 {target}。", data={"target": target})


def build_win_tools() -> list[tuple[ToolSchema, ToolHandler]]:
    """win 工具注册清单（供 app 喂 ToolRegistry.register）。

    v0.4 只 open_app；v0.8 补全 clip/搜索/音量/taskkill/睡眠时往此清单加
    (schema, handler) 元组，不改 llm.py。
    """
    return [(OPEN_APP_SCHEMA, OpenAppHandler())]
