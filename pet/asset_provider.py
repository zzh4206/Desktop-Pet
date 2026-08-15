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
        # v0.1 无真实帧序列，降级为静态单帧
        return [self.get_static(state, skin)]
