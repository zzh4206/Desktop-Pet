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

    # ---- L10 修（REVIEW-2026-08-27）：纯文本字段跳过黑名单 ----
    # clipboard.text / memory fact/query 是任意自然文本，旧版一刀切扫描
    # 拒绝"复制含 .. 的文本""记忆含 rm -rf 字样的事实"
    from pet.tools_win import CLIPBOARD_SCHEMA
    from pet.memory_tools import (
        MEMORY_SAVE_SCHEMA, MEMORY_SEARCH_SCHEMA,
    )

    reg5 = ToolRegistry(confirm_fn=lambda *a: True)  # 批次A：clipboard 已 dangerous，豁免测试需放行 confirm
    # 用 dispatch 层验证豁免（handler 用 Stub 不真写剪贴板）
    reg5.register(CLIPBOARD_SCHEMA, StubHandler(ToolResult(True, "copied")))
    r5 = reg5.dispatch(
        "clipboard",
        {"action": "set", "text": "路径说明：cd ../parent 与 rm -rf 命令"},
        ToolContext(pet_state=None, user_name="x", config={}))
    check("L10 clipboard.text 含 '../''rm -rf' 字样不再被黑名单拒",
          r5.success is True)
    reg5.register(MEMORY_SAVE_SCHEMA, StubHandler(ToolResult(True, "saved")))
    r6 = reg5.dispatch(
        "memory_save",
        {"fact": "用户提到过 ... 与 ../../etc 字样", "importance": 0.5},
        ToolContext(pet_state=None, user_name="x", config={}))
    check("L10 memory fact 含路径字样不再被拒", r6.success is True)
    check("L10 text_fields 已标注（clipboard/memory_save/search）",
          CLIPBOARD_SCHEMA.text_fields == ("text",)
          and MEMORY_SAVE_SCHEMA.text_fields == ("fact",)
          and MEMORY_SEARCH_SCHEMA.text_fields == ("query",))
    # 路径类字段仍拦截（file_search.pattern 不豁免）
    from pet.tools_win import FILE_SEARCH_SCHEMA
    reg5.register(FILE_SEARCH_SCHEMA, StubHandler(ToolResult(True, "hit")))
    r7 = reg5.dispatch(
        "file_search", {"pattern": "../../etc/passwd"},
        ToolContext(pet_state=None, user_name="x", config={}))
    check("L10 file_search.pattern 越界仍被拒（豁免仅限文本字段）",
          r7.success is False and "不安全" in r7.message)

    # ---- register 同名 warn（v0.8.1）----
    reg4 = ToolRegistry()
    reg4.register(safe_schema, StubHandler(ToolResult(True, "a")))
    # 重复注册应 warn（不崩，覆盖）
    reg4.register(safe_schema, StubHandler(ToolResult(True, "b")))
    check("register 同名覆盖不崩", reg4._handlers["safe_tool"]._result.message == "b")

    # ---- 批次A（REVIEW-2026-08-28 H5/F4/H6/F6/F12）----
    # H5：clipboard 读写都走确认框——无 confirm_fn 的 registry 必 fail-closed
    reg6 = ToolRegistry()
    reg6.register(CLIPBOARD_SCHEMA, StubHandler(ToolResult(True, "leak")))
    r8 = reg6.dispatch("clipboard", {"action": "get"},
                       ToolContext(pet_state=None, user_name="x", config={}))
    check("批次A clipboard dangerous：无 confirm_fn fail-closed 不执行",
          r8.success is False and "取消" in r8.message)

    # F4：open_app 按 dangerous_when 分流——url 走确认、纯 app 名不走
    reg6.register(WIN_SCHEMA, StubHandler(ToolResult(True, "opened")))
    r9 = reg6.dispatch("open_app", {"url": "https://example.com"},
                       ToolContext(pet_state=None, user_name="x", config={}))
    check("批次A open_app url 无 confirm_fn fail-closed",
          r9.success is False and "取消" in r9.message)
    r10 = reg6.dispatch("open_app", {"app": "notepad"},
                        ToolContext(pet_state=None, user_name="x", config={}))
    check("批次A open_app 纯 app 名不走 confirm_fn（dangerous_when 分流）",
          r10.success is True)

    # 批次F/L12（REVIEW-2026-09-04）：open_app 系统程序黑名单——
    # _APP_NAME 放行 "cmd"/"regedit.exe"，注入可借记忆通道诱导开系统程序
    _octx = ToolContext(pet_state=None, user_name="x", config={})
    r_cmd = OpenAppHandler().execute({"app": "cmd"}, _octx)
    r_re = OpenAppHandler().execute({"app": "regedit.exe"}, _octx)
    check("L12 open_app 系统程序黑名单拒绝（cmd/regedit.exe）",
          r_cmd.success is False and r_re.success is False)
    # 批次F/L13：_BARE_ROOT ~ 前缀整体拒绝（旧版注释称覆盖 ~root 实则放行）
    from pet.tools_schema import _is_unsafe_path

    check("L13 _BARE_ROOT 覆盖 ~root（注释承诺兑现）",
          _is_unsafe_path("~root") and _is_unsafe_path("~")
          and not _is_unsafe_path("C:/Users/x/notes.txt"))
    from pet.tools_mac import CLIPBOARD_SCHEMA as MAC_CLIP
    check("批次A mac clipboard 同步 dangerous + text_fields 对齐",
          MAC_CLIP.dangerous is True and MAC_CLIP.text_fields == ("text",))

    # H6：PowerShell 单引号转义
    from pet.tools_win import _ps_sq
    check("批次A _ps_sq 单引号翻倍转义", _ps_sq("C:\\Users\\O'Brien") ==
          "C:\\Users\\O''Brien")
    check("批次A _ps_sq 无引号原样返回", _ps_sq("C:\\Users\\lenovo") ==
          "C:\\Users\\lenovo")

    # F6：win process 系统关键进程硬拒（直接调 handler，不真跑 taskkill）
    from pet.tools_win import ProcessHandler
    r11 = ProcessHandler().execute(
        {"name": "explorer.exe"},
        ToolContext(pet_state=None, user_name="x", config={}))
    check("批次A process 硬拒 explorer.exe",
          r11.success is False and "关键进程" in r11.message)
    r12 = ProcessHandler().execute(
        {"name": "EXPLORER.EXE"},
        ToolContext(pet_state=None, user_name="x", config={}))
    check("批次A process 硬拒大小写不敏感", r12.success is False)
    # 批次E/M5（REVIEW-2026-08-31）：pid 路径也过硬拒名单——解析 csrss 真实
    # pid（只读 Get-Process，不真跑 taskkill），旧版 pid 直达 taskkill
    from pet.tools_win import _ps_run
    okp, csrss_pid = _ps_run(["(Get-Process csrss | Select-Object -First 1).Id"])
    if okp and csrss_pid.strip().isdigit():
        r13 = ProcessHandler().execute(
            {"pid": int(csrss_pid.strip())},
            ToolContext(pet_state=None, user_name="x", config={}))
        check("批次E process pid 路径硬拒 csrss（denylist 不再可绕）",
              r13.success is False and "关键进程" in r13.message)
    else:
        check("批次E process pid 硬拒（取不到 csrss pid，环境跳过）", True)

    # F12：工具结果回灌截断
    from pet.llm import truncate_tool_result, _TOOL_RESULT_MAX_CHARS
    long_text = "x" * (_TOOL_RESULT_MAX_CHARS + 500)
    cut = truncate_tool_result(long_text)
    check("批次A 超限工具结果被截断且带标注",
          len(cut) < _TOOL_RESULT_MAX_CHARS + 100
          and "已截断" in cut and str(_TOOL_RESULT_MAX_CHARS + 500) in cut)
    check("批次A 未超限结果原样返回",
          truncate_tool_result("short") == "short")

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
