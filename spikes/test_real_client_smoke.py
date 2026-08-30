"""真客户端冒烟（REVIEW H1/H2 根因修复）：不走 mock，验属性+方法不炸。"""
import sys
sys.path.insert(0, ".")
from pet.llm import OpenAICompatibleClient

c = OpenAICompatibleClient(api_key="sk-fake", registry=None)
assert hasattr(c, "_timeout"), "H2: _timeout missing"
assert hasattr(c, "_resp"), "H2: _resp missing"
assert hasattr(c, "usage"), "H2: usage missing"
print("  ✅ H2: _timeout/_resp/usage 属性全存在")

c.set_memory_context("")
c.set_memory_context("\n关于用户：测试")
assert "测试" in c._system["content"]
c.set_memory_context("")
assert c._system["content"] == c._base_system
print("  ✅ H2: set_memory_context 注入/清除正常")

# H2 核心：_timeout 修复后 requests.post 的 timeout 参数能取到
import pet.llm as llm_mod
timeout_seen = {"v": None}
orig_post = llm_mod.requests.post
def fake_post(*a, **kw):
    timeout_seen["v"] = kw.get("timeout")
    raise ConnectionError("smoke")
llm_mod.requests.post = fake_post
class MiniReg:
    def schemas(self): return []
    def dispatch(self, name, args, ctx):
        from pet.tools_schema import ToolResult
        return ToolResult(True, "ok")
try:
    c2 = OpenAICompatibleClient(api_key="sk-fake", registry=MiniReg())
    c2.chat_once([], None)
except ConnectionError:
    pass
except AttributeError as e:
    raise AssertionError(f"H2 regression: {e}")
finally:
    llm_mod.requests.post = orig_post
assert timeout_seen["v"] is not None and (
    # 批次H/T3：标量分支也要 >0——timeout=0 在 urllib3 语义=永不超时
    (isinstance(timeout_seen["v"], (int, float)) and timeout_seen["v"] > 0)
    or (isinstance(timeout_seen["v"], tuple)
        and all(x > 0 for x in timeout_seen["v"])))
print(f"  ✅ H2: chat_once 传 timeout={timeout_seen['v']}s 到网络层")

# ---- 批次D（REVIEW-2026-08-28）F9/F11/F7/F14 ----
import requests as _rq
from pet.llm import OfflineError, create_client  # noqa: E402

# F9：流中读超时（urllib3 ReadTimeoutError 被 requests 包成 ConnectionError）
# 应判"本次失败"（requests.Timeout），不是 OfflineError（离线粘死）
class _ReadTimeoutError(Exception):
    pass

class _FakeResp:
    status_code = 200
    def iter_lines(self, decode_unicode=True):
        raise _rq.ConnectionError(_ReadTimeoutError("Read timed out."))
    def close(self):
        pass

c3 = OpenAICompatibleClient(api_key="sk-fake", registry=MiniReg())
_llm_post = llm_mod.requests.post
llm_mod.requests.post = lambda *a, **kw: _FakeResp()
try:
    try:
        c3._stream_once([], None)
        raise AssertionError("F9: 读超时未抛异常")
    except _rq.Timeout:
        pass
    except OfflineError:
        raise AssertionError("F9: 读超时被误判 OfflineError（离线粘死回归）")
finally:
    llm_mod.requests.post = _llm_post
print("  ✅ F9: 流读超时→Timeout（failed 降级）而非 OfflineError")

# 真断线仍是 OfflineError（取消/断网语义不变）
class _FakeRespDisc:
    status_code = 200
    def iter_lines(self, decode_unicode=True):
        raise _rq.ConnectionError(ConnectionResetError("reset"))
    def close(self):
        pass

llm_mod.requests.post = lambda *a, **kw: _FakeRespDisc()
try:
    try:
        c3._stream_once([], None)
        raise AssertionError("F9: 断线未抛 OfflineError")
    except OfflineError:
        pass
finally:
    llm_mod.requests.post = _llm_post
print("  ✅ F9: 真断线仍 OfflineError")

# F11：无 id 的 tool_call 合成占位 id 回灌失败结果（不再 skip→续轮 400）
msgs = c3._dispatch_tool_calls(
    [{"function": {"name": "clipboard", "arguments": "{}"}}], None)
assert len(msgs) == 1 and msgs[0]["tool_call_id"].startswith("call_synth_") \
    and msgs[0]["role"] == "tool" and "工具失败" in msgs[0]["content"], msgs
print("  ✅ F11: 无 id tool_call 合成占位 id 回灌（续轮消息序列合法）")

# F7：非 deepseek 缺 base_url 必须报错（不静默回落 DS 端点送 key）
try:
    create_client("openai", "sk-fake", None,
                  {"llm": {"providers": {"openai": {"model": "gpt"}}}})
    raise AssertionError("F7: 缺 base_url 未报错")
except ValueError:
    pass
print("  ✅ F7: 非 deepseek 缺 base_url 拒绝回落 DS 端点")

# F12：payload 带显式 max_tokens
body = c3._payload([], None, stream=False)
assert body.get("max_tokens") == 4096, body
print("  ✅ F12: payload 显式 max_tokens=4096")

# F14：工具轮触顶/末轮空文本补终答 turn（appended 不止于 tool 结果）
class _CapClient(OpenAICompatibleClient):
    _MAX_TOOL_ROUNDS = 1
    def _stream_once(self, messages, tools, on_delta=None):
        return "", [{"id": "c1", "type": "function",
                     "function": {"name": "t", "arguments": "{}"}}], {}
    def _non_stream_once(self, messages, tools):
        return "", [], {}   # 触顶末轮空文本

c4 = _CapClient(api_key="sk-fake", registry=MiniReg())
text, appended = c4.chat_once(
    [type("T", (), {"role": "user", "content": "hi",
                    "tool_calls": None,
                    "to_message": lambda self: {"role": "user",
                                                "content": "hi"}})()],
    None)
assert appended and appended[-1].role == "assistant" \
    and appended[-1].content, appended
assert text == appended[-1].content
print("  ✅ F14: 触顶空文本补终答 turn（text=终答非中间轮）")

print(f"\n真客户端冒烟：9/9 通过")

# ---- 批次G/F29（REVIEW-2026-08-28）：可选真 API 冒烟 ----
# 历史教训（REVIEW-08-25/27 H1/H2）：mock 掩盖真客户端问题——SSE 聚合/
# [DONE]/续轮回灌/400 降级从未被真实数据验证过。默认跳过；设
# DESKTOP_PET_REAL_SMOKE=1 且提供 key 时跑一轮含 tool_call 的真实
# chat_once（几百分钱 token），CI/手动周期性跑。
import os
if os.environ.get("DESKTOP_PET_REAL_SMOKE") == "1":
    key = (os.environ.get("DEEPSEEK_API_KEY")
           or os.environ.get("DS_API_KEY"))
    if not key:
        print("⏭ DESKTOP_PET_REAL_SMOKE=1 但无 DEEPSEEK_API_KEY，跳过")
    else:
        import pet.llm as _lm

        class _EchoReg:
            """真 tool_call 闭环：第一个声明的工具原样成功回灌。"""
            def schemas(self):
                return [{
                    "type": "function",
                    "function": {
                        "name": "echo_tool",
                        "description": "把 text 原样返回（冒烟用）",
                        "parameters": {
                            "type": "object",
                            "properties": {"text": {"type": "string"}},
                            "required": ["text"],
                        },
                    },
                }]

            def dispatch(self, name, args, ctx):
                from pet.tools_schema import ToolResult
                return ToolResult(True, f"echo: {args.get('text', '')}")

        rc = _lm.OpenAICompatibleClient(
            api_key=key, registry=_EchoReg())
        text, appended = rc.chat_once(
            [_lm.ChatTurn("user",
                          "调用 echo_tool 传文本'冒烟'，然后用一句话确认。")],
            None)
        used_tool = any(t.role == "tool" for t in appended)
        ok = bool(text.strip()) and used_tool
        print(f"  {'✅' if ok else '❌'} F29 真API冒烟："
              f"回复 {len(text)} 字，tool 轮 {'有' if used_tool else '无'}，"
              f"usage={rc.usage.total_tokens}t")
        assert ok, "真 API 冒烟失败"
else:
    print("⏭ 真API冒烟未启用（DESKTOP_PET_REAL_SMOKE=1 + DEEPSEEK_API_KEY）")
