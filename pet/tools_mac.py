"""mac 平台工具实现 —— 平台适配与分工.md §五工具命令映射。

样板 ``open_app``：``open -a "App"`` 开程序 / ``open URL`` 开网页。
按 §2.2 冻结的 ``ToolHandler.execute(args, ctx) -> ToolResult`` 写，
由 ``ToolRegistry`` 注册（``build_mac_tools``），不 import 共享层。

危险操作确认不经此层（``dispatch`` 走注入的 ``confirm_fn``）。open_app 不危险
（v0.4 不触发 NSAlert，框架就位见补遗#5）。
"""

from __future__ import annotations

import logging
import re
import subprocess

from .tools_schema import ToolContext, ToolHandler, ToolResult, ToolSchema

log = logging.getLogger("pet")

# 程序名：字母数字空格点横点，禁路径分隔符（防 ``open -a "../../xxx"`` 越界）
_APP_NAME = re.compile(r"^[A-Za-z0-9 .\-]+$")
# URL：仅 http/https，防 file://、smb:// 等本机协议
_HTTP_URL = re.compile(r"^https?://", re.IGNORECASE)

OPEN_APP_SCHEMA = ToolSchema(
    name="open_app",
    description=(
        "打开一个 macOS 应用程序或网页。用于满足用户'打开 Safari'"
        "'打开浏览器'等请求。传 app（应用名，如 Safari）或 url（http(s) 网址）。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "app": {
                "type": "string",
                "description": "应用程序名，如 Safari、访达、计算器",
            },
            "url": {
                "type": "string",
                "description": "要打开的 http(s) 网址",
            },
        },
        # 二选一；不强制都给（additionalProperties False 防 DS 塞别的）
        "additionalProperties": False,
    },
    dangerous=False,
)


class OpenAppHandler:
    """``open -a`` / ``open URL``。"""

    def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        app = (args.get("app") or "").strip()
        url = (args.get("url") or "").strip()
        if app and url:
            return ToolResult(False, "app 与 url 二选一，不要同时给。")
        if app:
            if not _APP_NAME.match(app):
                return ToolResult(False, f"应用名不合法: {app!r}")
            argv = ["open", "-a", app]
            label = app
        elif url:
            if not _HTTP_URL.match(url):
                return ToolResult(False, "仅支持 http/https 网址。")
            argv = ["open", url]
            label = url
        else:
            return ToolResult(False, "需要 app 或 url 参数。")

        try:
            subprocess.run(argv, check=True, capture_output=True, timeout=10)
        except subprocess.CalledProcessError as e:
            stderr = (e.stderr or b"").decode(errors="replace").strip()
            log.warning("open_app 失败 %s: %s", label, stderr)
            return ToolResult(False, f"打开 {label} 失败: {stderr or '未知错误'}")
        except subprocess.TimeoutExpired:
            return ToolResult(False, f"打开 {label} 超时。")
        log.info("open_app 打开 %s", label)
        return ToolResult(True, f"已打开 {label}。", data={"target": label})


def build_mac_tools() -> list[tuple[ToolSchema, ToolHandler]]:
    """mac 工具注册清单（供 app 喂 ToolRegistry.register）。

    v0.4 只 open_app；v0.8 补全 clipboard/mdfind/volume/process/lock/sleep
    时往此清单加 (schema, handler) 元组，不改 llm.py。
    """
    return [(OPEN_APP_SCHEMA, OpenAppHandler())]
