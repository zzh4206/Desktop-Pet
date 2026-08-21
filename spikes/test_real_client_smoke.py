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
try:
    c2 = OpenAICompatibleClient(api_key="sk-fake", registry=MiniReg())
    c2.chat_once([], None)
except ConnectionError:
    pass
except AttributeError as e:
    raise AssertionError(f"H2 regression: {e}")
finally:
    llm_mod.requests.post = orig_post
assert timeout_seen["v"] is not None and (isinstance(timeout_seen["v"], (int, float, tuple)) and (isinstance(timeout_seen["v"], (int, float)) or all(x > 0 for x in timeout_seen["v"])))
print(f"  ✅ H2: chat_once 传 timeout={timeout_seen['v']}s 到网络层")

print("\n真客户端冒烟：3/3 通过")
