"""立绘 provider（差异化③）—— 接口冻结于 设计思路.md §2.2。

v0.1：``AssetProvider`` 双方法 + ``SpriteRef`` 冻结；实现 ``EmojiProvider``。
v0.1 不接 ``Renderer2D.draw``——window 直接把 emoji 当文字画在透明窗上；
``SpriteRef.path`` 对 emoji provider 放 emoji 字符串本身（对 AI/约稿 provider
v0.10+ 才是文件路径）。签名不动，语义这样用。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .pet_state import Branch, Mood, PetState, Stage


# 默认显示尺寸（逻辑像素，HiDPI 由 Qt 处理），bottom_center 为锚点
_STAGE_SIZE: dict[Stage, tuple[int, int]] = {
    Stage.YOUNG: (64, 64),
    Stage.ADULT: (96, 96),
    Stage.FINAL: (128, 128),
}


@dataclass
class SpriteRef:
    path: str
    width: int
    height: int
    anchor: str = "bottom_center"


class AssetProvider(Protocol):
    def get_static(self, state: PetState, skin: str = "default") -> SpriteRef:
        ...

    def get_frames(
        self, state: PetState, action: "ActionType", skin: str = "default"
    ) -> list[SpriteRef]:
        ...


_EMOJI_BY_MOOD: dict[Mood, str] = {
    Mood.HAPPY: "😺",
    Mood.NEUTRAL: "🐱",
    Mood.SAD: "😿",
    Mood.SLEEPY: "😴",
    Mood.HUNGRY: "🙀",
}

# v0.3 动作帧序列（emoji 占位；v0.10 AI provider 降级复用本表）
# 行走 2 帧：本体 + 迈步变体；动画 3 帧：本体→动作峰值→本体
_WALK_FRAME2: dict[Mood, str] = {
    Mood.HAPPY: "😸",
    Mood.NEUTRAL: "🐈",
    Mood.SAD: "😾",
    Mood.SLEEPY: "😪",
    Mood.HUNGRY: "😹",
}
_ACT_FRAME = "😽"  # 伸懒腰/打滚峰值占位帧


def _mood_from_state(state: PetState) -> Mood:
    """v0.2：饱食<20 优先 HUNGRY；mood >=50 HAPPY、>=20 NEUTRAL、余 SAD。"""
    if state.fullness < 20:
        return Mood.HUNGRY
    if state.mood >= 50:
        return Mood.HAPPY
    if state.mood >= 20:
        return Mood.NEUTRAL
    return Mood.SAD


class EmojiProvider:
    """按 state.stage/mood/branch 返回 emoji。v0.1 只用到 mood。"""

    def get_static(self, state: PetState, skin: str = "default") -> SpriteRef:
        mood = _mood_from_state(state)
        width, height = _STAGE_SIZE[state.stage]
        return SpriteRef(
            path=_EMOJI_BY_MOOD[mood],
            width=width,
            height=height,
            anchor="bottom_center",
        )

    def get_frames(
        self, state: PetState, action: "ActionType", skin: str = "default"
    ) -> list[SpriteRef]:
        """v0.3 动作帧序列（emoji 占位）：MOVE_TO 行走 2 帧交替，
        ANIMATE 3 帧（本体→峰值→本体），其余动作降级静态单帧。"""
        from .behavior import ActionType  # 局部 import 防循环依赖

        base = self.get_static(state, skin)
        mood = _mood_from_state(state)

        def _ref(path: str) -> SpriteRef:
            return SpriteRef(
                path=path, width=base.width, height=base.height,
                anchor=base.anchor,
            )

        if action == ActionType.MOVE_TO:
            return [base, _ref(_WALK_FRAME2[mood])]
        if action == ActionType.ANIMATE:
            return [base, _ref(_ACT_FRAME), base]
        return [base]
