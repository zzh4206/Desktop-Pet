"""v0.8.1 危险拦截框架验证 —— P14 confirm 跨线程 / P15 fail-closed /
黑名单补强 / _APP_NAME 禁纯点号。

运行：python spikes/test_v08_dangerous.py
"""

from __future__ import annotations

import sys

sys.path.insert(0, ".")

from pet.tools_schema import (  # noqa: E402
    ToolContext, ToolRegistry, ToolResult, ToolSchema,
)
from pet.tools_win import OpenAppHandler, OPEN_APP_SCHEMA as WIN_SCHEMA  # noqa: E402

PASS, FAIL = [], []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("  ✅ " if cond else "  ❌ ") + name)


class StubHandler:
    """ToolHandler 结构性实现（Protocol，无需继承）。"""
    def __init__(self, result: ToolResult):
        self._result = result

    def execute(self, args, ctx):
        return self._result


def main() -> int:
    # ---- P15: 基类 confirm_dangerous fail-closed（返 False）----
    from pet.platform import PlatformAdapter
    base = PlatformAdapter.__new__(PlatformAdapter)
    check("P15 基类 confirm_dangerous fail-closed 返 False",
          base.confirm_dangerous("t", "c", "r") is False)

    # ---- P15: confirm_fn 抛异常时 fail-closed ----
    def boom_confirm(t, c, r):
        raise RuntimeError("boom")

    reg = ToolRegistry(confirm_fn=boom_confirm)
    schema = ToolSchema(name="danger_tool", description="d",
                        parameters={}, dangerous=True)
    reg.register(schema, StubHandler(ToolResult(True, "执行了")))
    r = reg.dispatch("danger_tool", {}, ToolContext(
        pet_state=None, user_name="x", config={}))
    check("P15 confirm_fn 抛异常 fail-closed 不执行工具",
          r.success is False and "取消" in r.message)

    # ---- 主线程 confirm 通过 → 执行 ----
    def ok_confirm(t, c, r):
        return True

    reg2 = ToolRegistry(confirm_fn=ok_confirm)
    reg2.register(schema, StubHandler(ToolResult(True, "ok")))
    r2 = reg2.dispatch("danger_tool", {}, ToolContext(
        pet_state=None, user_name="x", config={}))
    check("主线程 confirm 通过 → 执行工具", r2.success is True)

    # ---- 不危险工具不走 confirm ----
    safe_schema = ToolSchema(name="safe_tool", description="d",
                             parameters={}, dangerous=False)
    reg3 = ToolRegistry(confirm_fn=lambda *a: (_ for _ in ()).throw(Exception("不该调")))
    reg3.register(safe_schema, StubHandler(ToolResult(True, "safe")))
    r3 = reg3.dispatch("safe_tool", {}, ToolContext(
        pet_state=None, user_name="x", config={}))
    check("不危险工具不走 confirm_fn", r3.success is True)

    # ---- 黑名单补强（v0.8.1）----
    from pet.tools_schema import _is_unsafe_path, _scan_args_unsafe
    check("黑名单 rm\\t-rf 命中", _is_unsafe_path("rm\t-rf /"))
    check("黑名单大小写不敏感 RM -RF 命中", _is_unsafe_path("RM -RF /"))
    check("黑名单 find -delete 命中", _is_unsafe_path("find / -delete"))
    check("黑名单 dd of=/dev/ 命中", _is_unsafe_path("dd if=x of=/dev/sda"))
    # 递归扫 dict/list
    check("_scan_args 递归 dict 命中",
          _scan_args_unsafe({"opts": {"p": "../../etc"}}) is not None)
    check("_scan_args 递归 list 命中",
          _scan_args_unsafe({"items": ["..", "normal"]}) is not None)
    # 正常参数不命中
    check("正常参数不命中黑名单",
          _scan_args_unsafe({"app": "notepad", "url": "https://x.com"}) is None)

    # ---- register 同名 warn（v0.8.1）----
    reg4 = ToolRegistry()
    reg4.register(safe_schema, StubHandler(ToolResult(True, "a")))
    # 重复注册应 warn（不崩，覆盖）
    reg4.register(safe_schema, StubHandler(ToolResult(True, "b")))
    check("register 同名覆盖不崩", reg4._handlers["safe_tool"]._result.message == "b")

    # ---- _APP_NAME 禁纯点号串（v0.8.1，win/mac 双端）----
    from pet.tools_win import _APP_NAME as WIN_APP
    from pet.tools_mac import _APP_NAME as MAC_APP
    check("win _APP_NAME 禁 '..'", WIN_APP.match("..") is None)
    check("win _APP_NAME 禁 '.'", WIN_APP.match(".") is None)
    check("win _APP_NAME 禁 '...'", WIN_APP.match("...") is None)
    check("win _APP_NAME 接受 'notepad'", WIN_APP.match("notepad") is not None)
    check("mac _APP_NAME 禁 '..'", MAC_APP.match("..") is None)
    check("mac _APP_NAME 接受 'Safari'", MAC_APP.match("Safari") is not None)

    print(f"\n结果：{len(PASS)} 通过 / {len(FAIL)} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
