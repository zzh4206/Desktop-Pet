"""LLM 工具层 —— 接口冻结于 设计思路.md §2.2。

``ToolContext`` / ``ToolHandler`` / ``ToolRegistry`` 三件套：schema 注册 +
``dispatch`` 路由 + 参数黑名单校验 + 危险操作确认（经注入的 ``confirm_fn``
回调，**本模块零平台库**——NSAlert/Qt 对话框由 ``app.py`` 经 ``platform``
注入，不在此 import AppKit/QtWidgets）。

v0.4 只注册 ``open_app``（不危险，NSAlert 框架就位但不触发，见补遗#5）。
后续工具（v0.8 全套）只在此 register，不改 ``llm.py``（schema 与实现解耦）。

v0.8.1：``dispatch`` 在 ChatWorker 子线程被调时，confirm_fn 用
``BlockingQueuedConnection`` 派到主线程执行（QMessageBox.exec/NSAlert.runModal
必须主线程，旧版子线程调会崩/死锁）；黑名单递归扫 dict/list；同名 register warn。
"""

from __future__ import annotations

import logging
import re
import threading
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
    # 批次A（REVIEW-2026-08-28 H4/F4）：按参数判定危险的谓词——如 open_app
    # 带 url（钓鱼面）时需确认、纯 app 名不需要。dangerous=True 或
    # dangerous_when(args) 为真，任一即走 confirm_fn。
    dangerous_when: Optional[Callable[[dict], bool]] = None
    # L10 修（REVIEW-2026-08-25）：纯文本载荷字段名——这些字段的值跳过
    # 路径/命令黑名单扫描（旧版一刀切：复制含 ".." 的文本、记忆存
    # "..." 事实全被拒）。文件路径类字段（file_search.pattern 等）不列。
    text_fields: tuple = ()


# 参数黑名单（§五 prompt 注入防护）：路径根/越界/破坏性模式。
# 工具自行按字段名校验，这里给共享判定。
# v0.8.1：去重 \.\.（旧版重复）；补 rm\t-rf/大小写/find-delete/dd-of（denylist 漏报）
_TRAVERSAL = re.compile(
    r"(^/+$|\.\.|rm[\s\t]+-rf|>\s*/dev/null|find\s+.*-delete|dd\s+.*of\s*=/dev/)",
    re.IGNORECASE,
)
# 形如 ~、~root、/、// 的裸根路径——批次F/L13（REVIEW-2026-09-04）：
# 旧版正则只匹配 ~/?，注释声称覆盖 ~root 实则放行；~ 前缀整体拒绝
# （Windows 无 ~user 展开，此类值一律按家目录根对待）
_BARE_ROOT = re.compile(r"^~|^//?$|^/+$")


def _is_unsafe_path(value: str) -> bool:
    if not isinstance(value, str):
        return False
    if _BARE_ROOT.match(value):
        return True
    if _TRAVERSAL.search(value):
        return True
    return False


def _scan_args_unsafe(args, skip=frozenset()) -> Optional[str]:
    """递归扫所有字符串值（含 dict/list 嵌套）是否命中黑名单；命中返回该值。

    v0.8.1：旧版只扫顶层 args.values()，嵌套参数（如 ``{"opts":{"p":"../"}}``）
    可绕过；现递归扫 dict/list 内所有字符串。
    L10：``skip`` 为豁免的**顶层字段名**（纯文本载荷，见 ToolSchema.text_fields）。
    """
    if isinstance(args, dict):
        for k, v in args.items():
            if k in skip:
                continue
            bad = _scan_args_unsafe(v)
            if bad is not None:
                return bad
    elif isinstance(args, list):
        for v in args:
            bad = _scan_args_unsafe(v)
            if bad is not None:
                return bad
    elif isinstance(args, str) and _is_unsafe_path(args):
        return args
    return None


ConfirmFn = Callable[[str, str, str], bool]
"""(title, command, risk) -> 是否继续。mac=NSAlert / win=Qt 对话框，经 app 注入。"""


class ToolRegistry:
    """工具注册表 + 路由。``schemas()`` 喂 DS，``dispatch`` 执行。

    签名冻结于 §2.2（``schemas``/``dispatch``）。``confirm_fn`` 为注入扩展点
    （v0.4 兼容扩展，非删改冻结签名）。

    v0.8.1：``dispatch`` 在 ChatWorker 子线程被调时，confirm_fn 经
    ``BlockingQueuedConnection`` 派到主线程执行（QMessageBox/NSAlert 必须主线程）。
    """

    def __init__(self, confirm_fn: Optional[ConfirmFn] = None) -> None:
        self._handlers: dict[str, ToolHandler] = {}
        self._schemas: dict[str, ToolSchema] = {}
        self._confirm_fn = confirm_fn
        self._confirm_caller = None  # 主线程 QObject helper（惰性创建）

    def register(self, schema: ToolSchema, handler: ToolHandler) -> None:
        if schema.name in self._schemas:
            log.warning("工具 %s 重复注册，覆盖旧 handler", schema.name)
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

    def _confirm_on_main(self, title: str, command: str, risk: str) -> bool:
        """调 confirm_fn；若当前非主线程，BlockingQueued 派到主线程（v0.8.1）。

        ChatWorker 在子线程跑 dispatch→confirm_fn，QMessageBox.exec/NSAlert.runModal
        必须主线程。用 QMetaObject.invokeMethod BlockingQueuedConnection 同步等待。
        """
        if self._confirm_fn is None:
            return False  # 无 confirm_fn → fail-closed 拒绝
        # 检测是否主线程
        try:
            from PySide6.QtCore import QCoreApplication, QThread
            app = QCoreApplication.instance()
            if app is None:
                return self._confirm_fn(title, command, risk)  # 无 app（测试）
            if QThread.currentThread() is app.thread():
                return self._confirm_fn(title, command, risk)  # 已在主线程
            # 子线程 → 派到主线程
            if self._confirm_caller is None:
                self._confirm_caller = _ConfirmCaller(self._confirm_fn)
                self._confirm_caller.moveToThread(app.thread())
            return self._confirm_caller.call_blocking(title, command, risk)
        except Exception as exc:
            log.warning("confirm 跨线程派发失败，fail-closed 拒绝: %s", exc)
            return False

    def dispatch(self, name: str, args: dict, ctx: ToolContext) -> ToolResult:
        handler = self._handlers.get(name)
        if handler is None:
            return ToolResult(False, f"未知工具: {name}")
        schema = self._schemas[name]

        # 批次A/P1-4（REVIEW-2026-09-05）：非 dict 参数防御归一——llm 解析层
        # 已归一，此处兜其他调用方（测试/未来路径），空参进 handler 由其校验。
        if not isinstance(args, dict):
            log.warning("工具 %s 收到非 dict 参数（%s），按空参处理",
                        name, type(args).__name__)
            args = {}

        # 参数黑名单校验（注入防护，递归扫 dict/list；L10：纯文本字段豁免）
        bad = _scan_args_unsafe(args,
                                skip=frozenset(schema.text_fields))
        if bad is not None:
            log.warning("工具 %s 参数命中黑名单: %r", name, bad)
            return ToolResult(False, "参数包含不安全的路径或命令。")

        # 危险操作确认（v0.4 open_app 不危险，框架就位；批次A 补
        # dangerous_when 按参数判定：open_app url 这类"部分参数危险"）。
        # 批次A/P1-4：dangerous_when 判定异常按危险处理（fail-closed）——
        # 旧版谓词异常直接逃出 dispatch 致整轮聊天降级。
        dangerous = schema.dangerous
        if not dangerous and schema.dangerous_when is not None:
            try:
                dangerous = bool(schema.dangerous_when(args))
            except Exception:
                log.warning("工具 %s dangerous_when 判定异常，按危险处理"
                            "（fail-closed）", name, exc_info=True)
                dangerous = True
        if dangerous:
            cmd_repr = f"{name}({args})"
            ok = self._confirm_on_main("危险操作确认", cmd_repr, "该操作不可撤销。")
            if not ok:
                return ToolResult(False, "用户已取消。")

        try:
            return handler.execute(args or {}, ctx)
        except Exception as e:  # 工具异常不崩主链
            log.exception("工具 %s 执行异常", name)
            return ToolResult(False, f"工具执行失败: {e}")


class _ConfirmCaller:
    """主线程 confirm 派发 helper（v0.8.1）。

    子线程经 BlockingQueuedConnection 调用主线程的 confirm_fn（QMessageBox/
    NSAlert 必须主线程）。用 QEventLoop + 信号实现同步等待。
    """

    def __init__(self, confirm_fn: ConfirmFn) -> None:
        from PySide6.QtCore import QObject, Signal

        self._fn = confirm_fn
        # 用内部 QObject 持信号（_ConfirmCaller 本身非 QObject，避免多继承）
        class _Inner(QObject):
            requested = Signal(str, str, str)
            done = Signal(bool)

            def __init__(self_inner):
                super().__init__()
                self_inner.result: bool = False
                self_inner.requested.connect(self_inner._on_requested)

            def _on_requested(self_inner, title, command, risk):
                try:
                    self_inner.result = bool(self._fn(title, command, risk))
                except Exception:
                    self_inner.result = False
                self_inner.done.emit(self_inner.result)

        self._inner = _Inner()

    def moveToThread(self, thread) -> None:
        self._inner.moveToThread(thread)

    def call_blocking(self, title: str, command: str, risk: str) -> bool:
        from PySide6.QtCore import QEventLoop, Qt

        loop = QEventLoop()
        self._inner.done.connect(loop.quit)
        # BlockingQueuedConnection：等主线程槽执行完
        self._inner.requested.emit(title, command, risk)
        if self._inner.thread() is not __import__("PySide6.QtCore", fromlist=["QThread"]).QThread.currentThread():
            loop.exec()  # 子线程阻塞等主线程 done
        else:
            # 信号同步派发（同线程直连）
            pass
        return self._inner.result
