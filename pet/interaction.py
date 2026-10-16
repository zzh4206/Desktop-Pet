"""交互语义与决策层（v0.19.x）——呈现无关，2D/3D 双实现共用。

v0.19.0 前交互语义（字段表/文案）散在 app.py 模块级；v0.19.1 起收拢到本
模块并加入"宠物有意见"三态决策：正常生效 / 饱和拒绝 / 互动疲劳。
decide_interaction() 是纯函数（时钟注入），产出 InteractionOutcome——
飘字/气泡/音效/动画四通道都从 outcome 取数，app._interact 只做装配。
动词经 0.19.1 换代（洗澡→梳梳毛、戳一戳→逗一逗），信号键不动。
"""

from __future__ import annotations

import random
from dataclasses import dataclass

# 交互：kind → 数值字段（信号键稳定，勿随文案改）
INTERACT_FIELD = {
    "pet": "mood",
    "feed": "fullness",
    "clean": "cleanliness",
    "poke": "mood",
}
# 飘字/状态板共用的字段名（与 _status_rows 的"饱食"措辞一致）
INTERACT_FIELD_LABEL = {
    "mood": "心情",
    "fullness": "饱食",
    "cleanliness": "清洁",
    "attention": "陪伴",
}
# 右键菜单显示动词（v0.19.1 换代：喂食→喂点吃的、洗澡→梳梳毛、戳一戳→逗一逗）
VERBS = {
    "pet": "摸摸头",
    "feed": "喂点吃的",
    "clean": "梳梳毛",
    "poke": "逗一逗",
}


def mood_bucket(mood: float) -> str:
    """文案分桶——阈值与 asset_provider._mood_from_state 一致。"""
    if mood >= 50:
        return "happy"
    if mood >= 20:
        return "neutral"
    return "sad"


# 文案池：交互 × mood 分桶随机（config interaction.messages 平铺覆盖整池）
MSG_POOLS = {
    "pet": {
        "happy": ("摸摸头～好舒服呀", "嘿嘿，最喜欢摸摸了～", "呼噜呼噜…"),
        "neutral": ("摸摸头～", "蹭蹭你的手心", "嗯嗯，我在呢"),
        "sad": ("摸摸头，谢谢你陪我", "有你在，好受多了…", "再摸一会儿好吗"),
    },
    "feed": {
        "happy": ("吃饱啦！今天也好好吃～", "真好吃！还有吗？", "幸福感满满～"),
        "neutral": ("吃饱啦！", "谢谢投喂～", "咕噜咕噜…真香"),
        "sad": ("谢谢…吃上饭就好多了", "吃饱了，心情也亮了一点", "有你记得喂我，真好"),
    },
    "clean": {
        "happy": ("梳梳毛～毛都顺啦", "蓬松松的，好舒服", "咕噜咕噜…最喜欢梳毛了"),
        "neutral": ("梳梳毛～", "毛顺了，清爽！", "梳好啦～"),
        "sad": ("梳梳毛，舒服多了…", "谢谢你帮我打理", "身上不糙了，谢谢你"),
    },
    "poke": {
        "happy": ("逗我玩啦！扑——", "哈哈，好好玩！", "再来再来！"),
        "neutral": ("逗一逗～扑！", "好玩好玩", "扑通扑通…"),
        "sad": ("逗逗我，心情好点了…", "玩一会儿，没那么难过了", "谢谢你陪我玩"),
    },
}

# F6 饱和拒绝文案（feed 专用；0.19.x 后续种类再扩键）
REJECT_MSGS = {
    "feed": ("吃不下了…肚子圆滚滚的", "现在好饱，等下再喂我吧～", "再多要吃不下啦"),
}

# F7 疲劳文案（各交互通用）
FATIGUE_MSGS = ("歇一会儿再陪我玩嘛…", "有点累啦，缓缓～", "玩累了，等下继续～")

# F12 交互事件记忆模板（v0.19.3）：调用方加日期戳前缀后 memorize——
# 同文自动去重合并 = 同类事件当日一条，低重要度由 forget_expired 自然衰减
MEM_FACTS = {
    "pet": {"ok": "主人摸了摸我的头", "tired": "主人一直摸我陪我玩"},
    "feed": {"ok": "主人喂我吃了东西", "tired": "主人一直喂我吃东西",
             "full": "我吃饱了主人还想喂我"},
    "clean": {"ok": "主人帮我梳了毛", "tired": "主人一直帮我梳毛"},
    "poke": {"ok": "主人陪我玩了逗一逗", "tired": "主人陪我玩了好久"},
}


def memory_fact(kind: str, outcome: InteractionOutcome) -> tuple[str, float] | None:
    """F12：交互 → (记忆事实, 重要度)；None 不写。宠物视角描述主人。

    拒绝仅 feed 会出现——其它 kind 的 rejected（实际不可达）落回 ok 模板。"""
    tpl = MEM_FACTS.get(kind) or {}
    if outcome.rejected and tpl.get("full"):
        return tpl["full"], 0.25
    if outcome.fatigued and tpl.get("tired"):
        return tpl["tired"], 0.2
    fact = tpl.get("ok")
    return (fact, 0.35) if fact else None


def pet_status_line(state) -> str:
    """F11：PetState → 聊天 system prompt 一行摘要（LLM 可自然提及状态）。"""
    try:
        return (f"宠物当前状态：{state.stage.value}期·{state.branch.value}，"
                f"饱食{state.fullness:.0f}/心情{state.mood:.0f}/"
                f"清洁{state.cleanliness:.0f}。"
                "可以像自己的感受一样自然融入回复，不要罗列数字。")
    except (AttributeError, TypeError, ValueError):
        return ""


@dataclass
class InteractionOutcome:
    """一次交互的决策结果——四通道反馈的唯一数据源。

    field=None 表示未知 kind（调用方应静默返回）；rejected/fatigued 为 True
    时 delta=0（数值不动，但反馈照走——拒绝也是反馈）。
    """

    kind: str
    field: str | None
    delta: float
    rejected: bool = False
    fatigued: bool = False
    message: str | None = None
    floating_text: str = ""
    floating_tone: str = "pos"   # pos / neg / flat
    sound: str = ""
    chew: bool = False


def _pick_msg(kind: str, mood: float, overrides: dict | None) -> str | None:
    if overrides and overrides.get(kind):
        return random.choice(overrides[kind])
    pool = MSG_POOLS.get(kind)
    if not pool:
        return None
    msgs = pool.get(mood_bucket(mood)) or ()
    return random.choice(msgs) if msgs else None


def decide_interaction(
    kind: str,
    *,
    gain: float,
    mood: float,
    fullness: float,
    reject_fullness: float,
    fatigue_times: int,
    fatigue_window_s: float,
    recent: list[float],
    now: float,
    msg_overrides: dict | None = None,
) -> InteractionOutcome:
    """三态决策（纯函数）：疲劳 > 饱和拒绝 > 正常生效。

    recent 为该 kind 窗口内已生效的时间戳（调用方维护、只增不改），
    窗口过滤在此完成——换一种交互立即恢复（各 kind 独立计数）。
    """
    field = INTERACT_FIELD.get(kind)
    if field is None:
        return InteractionOutcome(kind, None, 0.0)
    in_window = [t for t in recent if now - t <= fatigue_window_s]
    if len(in_window) >= fatigue_times:
        return InteractionOutcome(
            kind, field, 0.0, fatigued=True,
            message=random.choice(FATIGUE_MSGS),
            floating_text="玩累了…", floating_tone="flat", sound=kind)
    if kind == "feed" and fullness >= reject_fullness:
        return InteractionOutcome(
            kind, field, 0.0, rejected=True,
            message=random.choice(REJECT_MSGS[kind]),
            floating_text="吃不下了…", floating_tone="flat", sound="reject")
    return InteractionOutcome(
        kind, field, gain,
        message=_pick_msg(kind, mood, msg_overrides),
        floating_text=f"{INTERACT_FIELD_LABEL.get(field, field)} {gain:+g}",
        floating_tone="pos" if gain >= 0 else "neg",
        sound=kind, chew=(kind == "feed" and gain > 0))
