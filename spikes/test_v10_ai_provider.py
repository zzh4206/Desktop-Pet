"""v0.10 AIArtProvider 接入验证（win 端，offscreen 渲染）。

覆盖：① 30 张 (stage,branch,mood) 静态图全部可解析且文件存在
② 缺文件/空目录 → 降级 EmojiProvider 不崩
③ skin 非 default 后缀 + 缺失降级
④ get_frames 返回 AI 静帧单帧（v0.10.1 防闪烁约定）
⑤ WindowBase 渲染分支：file→QPixmap、emoji→文本、启动即显示（非空）
"""
import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication

app = QApplication.instance() or QApplication([])

from pet.asset_provider import AIArtProvider, EmojiProvider
from pet.behavior import ActionType
from pet.pet_state import Branch, Mood, PetState, Stage
from pet.window import WindowBase

ASSETS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "ai"
)
SIZE = {"young": 64, "adult": 96, "final": 128}

passed = 0


def ok(name: str) -> None:
    global passed
    passed += 1
    print(f"  ✅ {name}")


def state_for(stage, branch, mood):
    m = {"happy": 80, "neutral": 30, "sad": 10}
    return PetState(
        mood=m.get(mood, 80),
        fullness=10 if mood == "hungry" else 80,
        stage=Stage(stage),
        branch=Branch(branch),
    )


def mood_for(state, provider, idle=None) -> Mood:
    if idle is not None:
        return provider.get_static(state).path and None
    return None


# ---------- ① 30 张全部可解析 ----------
print("== ① 30 张静态图解析 ==")
p_ai = AIArtProvider(idle_fn=None)
for stage in ("young", "adult", "final"):
    for branch in ("healthy", "neglected"):
        for mood in ("happy", "neutral", "sad", "sleepy", "hungry"):
            state = state_for(stage, branch, mood)
            # sleepy/hungry 直接构造 mood 文件路径：sleepy 走 idle、hungry 走 fullness
            sr = AIArtProvider(
                idle_fn=(lambda: 700.0) if mood == "sleepy" else None
            ).get_static(state)
            expect = f"{stage}_{branch}_{mood}.png"
            assert sr.path.endswith(expect), f"{expect} -> {sr.path}"
            assert os.path.isfile(sr.path), f"{sr.path} 文件不存在!"
            assert sr.width == SIZE[stage] and sr.height == SIZE[stage]
ok("全部 30 张路径解析/文件存在/尺寸正确")

# ①b 情绪→mood 判定抽查
assert AIArtProvider().get_static(state_for("adult", "healthy", "happy")).path.endswith("adult_healthy_happy.png")
assert AIArtProvider().get_static(state_for("adult", "healthy", "sad")).path.endswith("adult_healthy_sad.png")
assert AIArtProvider(idle_fn=lambda: 700.0).get_static(state_for("adult", "healthy", "sleepy")).path.endswith("_sleepy.png")
assert AIArtProvider().get_static(state_for("adult", "healthy", "hungry")).path.endswith("_hungry.png")
ok("情绪判定（happy/sad/sleepy-idle/hungry）正确")

# ---------- ② 缺文件降级 ----------
print("== ② 缺失降级 ==")
p_empty = AIArtProvider(assets_dir=tempfile.mkdtemp())
sr = p_empty.get_static(state_for("adult", "healthy", "neutral"))
assert not os.path.isfile(sr.path)  # 返回 emoji 字符串
assert len(sr.path) <= 8  # emoji 字符
ok("空目录 → emoji 降级不崩")
sr = p_empty.get_frames(state_for("adult", "healthy", "neutral"), ActionType.MOVE_TO)
assert len(sr) >= 1 and not os.path.isfile(sr[0].path)
ok("降级 get_frames 正常")

# ---------- ③ skin ----------
print("== ③ skin 后缀 ==")
sr = p_ai.get_static(state_for("adult", "healthy", "neutral"), skin="swimsuit")
assert not os.path.isfile(sr.path)  # 无 swimsuit 文件 → emoji 降级
ok("skin 自定义缺失 → 降级")

# ---------- ④ get_frames AI 静帧 ----------
print("== ④ get_frames ==")
fr = p_ai.get_frames(state_for("adult", "healthy", "neutral"), ActionType.ANIMATE)
assert len(fr) == 1 and fr[0].path.endswith("adult_healthy_neutral.png")
ok("ANIMATE 返回 AI 静帧单帧（不闪 emoji）")

# ---------- ⑤ 窗口渲染 ----------
print("== ⑤ WindowBase 渲染 ==")
# 5a 启动即显示（小图 64×64 模式）：init 后位图非空
w = WindowBase(p_ai.get_static(state_for("young", "healthy", "neutral")))
assert w._sprite.path.endswith("young_healthy_neutral.png")
assert not w._label.pixmap().isNull(), "启动 AI 图必显示位图"
ok("启动 AI 图 → pixmap 非空")
# 5b emoji SpriteRef init → 文本非空
we = WindowBase(EmojiProvider().get_static(state_for("young", "healthy", "neutral")))
txt = we._label.text()
assert txt and len(txt) >= 1, f"emoji 启动应为文本（实际 text={txt!r}）"
assert we._label.pixmap().isNull(), "emoji 模式不应有位图"
ok("启动 emoji → 文本非空（防 init 清屏回归）")
# 5c 切换：file → emoji（QLabel.pixmap() 从不返回 None，空状态=isNull）
w.set_sprite(EmojiProvider().get_static(state_for("adult", "healthy", "neutral")))
assert w._label.text() and w._label.pixmap().isNull(), \
    f"file→emoji: text={w._label.text()!r} pixnull={w._label.pixmap().isNull()}"
ok("file→emoji 切换正确")
# 5d emoji → file
w.set_sprite(p_ai.get_static(state_for("adult", "healthy", "neutral")))
assert not w._label.pixmap().isNull(), "emoji→file 应切位图"
ok("emoji→file 切换正确")
# 5e provider 链：on_state_change
w.set_sprite_provider(p_ai)
w.on_state_change(state_for("final", "neglected", "sad"))
assert not w._label.pixmap().isNull()
ok("on_state_change → AI 位图")

print(f"\n结果：{passed} 项通过")
