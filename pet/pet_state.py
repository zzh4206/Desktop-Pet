"""养成 State（单一真相源）—— 接口冻结于 设计思路.md §2.2。

v0.1：建一个冻结的 ``PetState`` dataclass（全字段，值占位），FSM/EmojiProvider
只读 ``mood``。养成衰减 / ``PetStateStore``（save/load 原子写 + version migrate）
是 v0.2 的范围；v0.1 不实现，签名照 §2.2 冻结，行为分期补全。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Stage(Enum):
    YOUNG = "young"
    ADULT = "adult"
    FINAL = "final"


class Branch(Enum):
    HEALTHY = "healthy"
    NEGLECTED = "neglected"


class Mood(Enum):
    HAPPY = "happy"
    NEUTRAL = "neutral"
    SAD = "sad"
    SLEEPY = "sleepy"
    HUNGRY = "hungry"


@dataclass(frozen=True)
class PetState:
    """冻结的养成状态。v0.1 全字段占位，只读 mood。"""

    mood: float = 80.0
    fullness: float = 80.0
    cleanliness: float = 80.0
    age: float = 0.0
    stage: Stage = Stage.YOUNG
    branch: Branch = Branch.HEALTHY

    @classmethod
    def default(cls) -> "PetState":
        return cls()
