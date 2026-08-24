"""立绘 provider（差异化③）—— 接口冻结于 设计思路.md §2.2。

v0.1：``AssetProvider`` 双方法 + ``SpriteRef`` 冻结；实现 ``EmojiProvider``。
v0.1 不接 ``Renderer2D.draw``——window 直接把 emoji 当文字画在透明窗上；
``SpriteRef.path`` 对 emoji provider 放 emoji 字符串本身（对 AI/约稿 provider
v0.10+ 才是文件路径）。签名不动，语义这样用。
"""

from __future__ import annotations

import os

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from .pet_state import Branch, Mood, PetState, Stage

if TYPE_CHECKING:
    # 仅类型检查期 import：运行时仍需在 get_frames 内局部 import
    # （behavior → asset_provider → behavior 反向引用会循环，故局部懒加载）
    from .behavior import ActionType


# 默认显示尺寸（逻辑像素，HiDPI 由 Qt 处理），bottom_center 为锚点
# v0.10.12：192/256/320（用户两轮反馈小尺寸发糊；1:1 显示无损）
_STAGE_SIZE: dict[Stage, tuple[int, int]] = {
    Stage.YOUNG: (192, 192),
    Stage.ADULT: (256, 256),
    Stage.FINAL: (320, 320),
}

# SLEEPY 门限：系统空闲超过此时长（秒）→ mood 显示 SLEEPY（v0.10 决策②：
# 枚举早有 SLEEPY，但 _mood_from_state 从不产出；idle 高时让立绘睡觉）
_SLEEPY_IDLE_S_DEFAULT = 600.0


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
    Mood.HUNGRY: "🙀",
    Mood.SLEEPY: "😪",
}

# v0.5：按 (stage, branch, mood) 出 emoji——stage 决定"成长形态"
# （幼奶猫/成猫/终黑猫三组，尺寸另由 _STAGE_SIZE 区分），branch 决定
# "神态"：HEALTHY 走下面正常表，NEGLECTED 一律落寞（即便 mood=HAPPY 也压成
# 颓相）。每 (stage, branch) 对在 NEUTRAL/HAPPY 等常态下产出可区分 emoji。
_HEALTHY_BY_STAGE_MOOD: dict[tuple[Stage, Mood], str] = {
    # YOUNG：奶猫系
    (Stage.YOUNG, Mood.HAPPY): "😺",
    (Stage.YOUNG, Mood.NEUTRAL): "🐱",
    (Stage.YOUNG, Mood.SAD): "😿",
    (Stage.YOUNG, Mood.HUNGRY): "🙀",
    (Stage.YOUNG, Mood.SLEEPY): "😪",
    # ADULT：成猫系（emoji 明显比幼大一档）
    (Stage.ADULT, Mood.HAPPY): "😸",
    (Stage.ADULT, Mood.NEUTRAL): "🐈",
    (Stage.ADULT, Mood.SAD): "😾",
    (Stage.ADULT, Mood.HUNGRY): "😹",
    (Stage.ADULT, Mood.SLEEPY): "😪",
    # FINAL：黑猫终态系
    (Stage.FINAL, Mood.HAPPY): "😻",
    (Stage.FINAL, Mood.NEUTRAL): "🐈‍⬛",
    (Stage.FINAL, Mood.SAD): "😿",
    (Stage.FINAL, Mood.HUNGRY): "🙀",
    (Stage.FINAL, Mood.SLEEPY): "😴",
}

# NEGLECTED：落寞系，随 stage 略变（仍可与其他 stage 区分），不随 mood 雀跃
_NEGLECTED_BY_STAGE: dict[Stage, str] = {
    Stage.YOUNG: "😿",
    Stage.ADULT: "😾",
    Stage.FINAL: "🙀",
}

# v0.3 动作帧序列（emoji 占位；v0.10 AI provider 降级复用本表）
# 行走 2 帧：本体 + 迈步变体；动画 3 帧：本体→动作峰值→本体
_WALK_FRAME2: dict[Mood, str] = {
    Mood.HAPPY: "😸",
    Mood.NEUTRAL: "🐈",
    Mood.SAD: "😾",
    Mood.HUNGRY: "😹",
}
_ACT_FRAME = "😽"  # 伸懒腰/打滚峰值占位帧


def _mood_from_state(state: PetState, idle_s: float | None = None) -> Mood:
    """v0.2：饱食<20 优先 HUNGRY；mood >=50 HAPPY、>=20 NEUTRAL、余 SAD。

    v0.10：idle_s（系统空闲秒，None=不启用）≥ 门限 → SLEEPY（优先级在
    HUNGRY 之下、mood 之上——饿醒比困重要）。
    """
    if state.fullness < 20:
        return Mood.HUNGRY
    if idle_s is not None and idle_s >= _SLEEPY_IDLE_S_DEFAULT:
        return Mood.SLEEPY
    if state.mood >= 50:
        return Mood.HAPPY
    if state.mood >= 20:
        return Mood.NEUTRAL
    return Mood.SAD


class EmojiProvider:
    """按 state.stage/mood/branch 返回 emoji。v0.5 起按 (stage, branch, mood)。

    v0.10：可选 idle_fn（无参 callable 返回系统空闲秒）→ 门限判定 SLEEPY。
    """

    def __init__(self, idle_fn=None, sleepy_idle_s: float | None = None):
        self._idle_fn = idle_fn
        self._sleepy_idle_s = (
            float(sleepy_idle_s) if sleepy_idle_s is not None
            else _SLEEPY_IDLE_S_DEFAULT
        )

    def _idle_s(self) -> float | None:
        if self._idle_fn is None:
            return None
        try:
            return float(self._idle_fn())
        except Exception:
            return None

    def get_static(self, state: PetState, skin: str = "default") -> SpriteRef:
        mood = _mood_from_state(state, self._idle_s())
        width, height = _STAGE_SIZE[state.stage]
        if state.branch == Branch.NEGLECTED:
            emoji = _NEGLECTED_BY_STAGE.get(state.stage, "😿")
        else:
            emoji = _HEALTHY_BY_STAGE_MOOD.get(
                (state.stage, mood), _EMOJI_BY_MOOD.get(mood, "🐱")
            )
        return SpriteRef(
            path=emoji,
            width=width,
            height=height,
            anchor="bottom_center",
        )

    def get_frames(
        self, state: PetState, action: "ActionType", skin: str = "default"
    ) -> list[SpriteRef]:
        """v0.3 动作帧序列（emoji 占位）：MOVE_TO 行走 2 帧交替，
        ANIMATE 3 帧（本体→峰值→本体），其余动作降级静态单帧。"""
        # 局部 import 防循环依赖：behavior 顶层 import asset_provider，
        # 此处反向引用须懒加载（开销极小，仅每次 get_frames 一次查表）。
        from .behavior import ActionType

        base = self.get_static(state, skin)
        mood = _mood_from_state(state, self._idle_s())

        def _ref(path: str) -> SpriteRef:
            return SpriteRef(
                path=path, width=base.width, height=base.height,
                anchor=base.anchor,
            )

        if action == ActionType.MOVE_TO:
            # SLEEPY 行走帧缺失（emoji 表仅 4 mood）→ 降级本体，防 KeyError
            return [base, _ref(_WALK_FRAME2.get(mood) or base.path)]
        if action == ActionType.ANIMATE:
            return [base, _ref(_ACT_FRAME), base]
        return [base]

# ================= v0.10 AIArtProvider =================


class AIArtProvider:
    """AI 立绘 provider（§六三级第 2 级）。

    ``get_static`` 读 ``assets/ai/{stage}_{branch}_{mood}.png``（skin 非
    default 加 ``_{skin}`` 后缀）；缺文件/IO 异常 → **降级 EmojiProvider**
    （不崩，§六"失败降级 emoji"）。``get_frames`` 降级返回 emoji 帧
    （AI 仅静态，动画帧 v0.10 后补——§六约定）。

    与 EmojiProvider 同签名（v0.1 冻结）；构造参数透传 idle_fn/sleepy。
    """

    def __init__(self, idle_fn=None, sleepy_idle_s: float | None = None,
                 assets_dir: str = ""):
        self._fallback = EmojiProvider(idle_fn, sleepy_idle_s)
        if not assets_dir:
            import os

            assets_dir = os.path.normpath(os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                "..", "assets", "ai",
            ))
        self._dir = assets_dir
        self._miss_cache: set = set()   # 已知缺失文件（防每 tick 重复 stat）

    def get_static(self, state: PetState, skin: str = "default") -> SpriteRef:
        mood = _mood_from_state(state, self._fallback._idle_s())
        suffix = "" if skin == "default" else f"_{skin}"
        filename = f"{state.stage.value}_{state.branch.value}"                    f"_{mood.value}{suffix}.png"
        path = os.path.join(self._dir, filename)  # noqa: F821

        if filename not in self._miss_cache:
            if os.path.isfile(path):  # noqa: F821
                width, height = _STAGE_SIZE[state.stage]
                return SpriteRef(
                    path=path, width=width, height=height,
                    anchor="bottom_center",
                )
            self._miss_cache.add(filename)

        # 降级：emoji（不区分 miss 原因——缺文件/目录不存在/权限均可）
        return self._fallback.get_static(state, skin)

    # 动作 → 帧名模板（assets/frames/{stage}_{action}.png）与帧间隔
    _FRAME_SPECS: dict = {
        "walk":   (("walk_0", "walk_1"), 200),
        "stretch": (("stretch_0", "stretch_1", "stretch_2"), 260),
        "roll":   (("roll",), 260),
        "blink":  (("idle_blink_0", "idle_blink_1"), 300),
        "fall":   (("fall_air", "fall_land"), 200),
        "eat_mouse": (("eat_mouse_0", "eat_mouse_1", "eat_mouse_2", "eat_mouse_3"), 320),
        "chew":   (("chew_0", "chew_1"), 280),
    }

    def _frames_dir(self) -> str:
        import os

        return os.path.normpath(os.path.join(self._dir, "..", "frames"))

    def get_frames(
        self, state: PetState, action: "ActionType", skin: str = "default"
    ) -> list[SpriteRef]:
        """v0.10.15：按动作读 assets/frames 帧序列（原生透明 1024 图）。

        帧文件存在 → 返回 SpriteRef 序列（width/height 用 _STAGE_SIZE 显示档，
        窗口 KeepAspect 缩放底对齐）；缺帧回退 [get_static]（单帧不闪）。
        """
        from .behavior import ActionType

        stage = state.stage.value
        action_name = action.value if isinstance(action, ActionType) else str(action)
        # ACTION 名 → 帧规格键
        key = {
            "move_to": "walk", "animate": None, "fall": "fall",
            "eat_mouse": "eat_mouse", "speak": None,
            "follow_cursor": "walk",
        }.get(action_name, None)
        # animate 的 name 参数（app 传 ActionType.ANIMATE + params 里 name；
        # 这里无 params，由 app 走 get_frames(state, ANIMATE) 通用序或由
        # _play_animate 传帧名——保留 ANIMATE 返回拉伸序查表）
        if key is None:
            return [self.get_static(state, skin)]

        names = [f"{stage}_{n}" for n in self._FRAME_SPECS[key][0]]
        refs = []
        for n in names:
            p = os.path.join(self._frames_dir(), n + ".png")
            if os.path.isfile(p):
                refs.append(SpriteRef(
                    path=p, width=_STAGE_SIZE[state.stage][0],
                    height=_STAGE_SIZE[state.stage][1], anchor="bottom_center",
                ))
        if not refs:
            return [self.get_static(state, skin)]
        return refs

    def frames_for(self, stage: str, action_key: str) -> list[SpriteRef]:
        """按 (stage, action_key) 返回帧序列（无帧 → []，app 走静帧兜底）。"""
        spec = self._FRAME_SPECS.get(action_key)
        if spec is None:
            return []
        refs = []
        for n in spec[0]:
            p = os.path.join(self._frames_dir(), f"{stage}_{n}.png")
            if os.path.isfile(p):
                refs.append(SpriteRef(
                    path=p, width=1, height=1, anchor="bottom_center",
                ))
        return refs

    def frame_interval(self, action_name: str) -> int:
        return int(self._FRAME_SPECS.get(action_name, ("x", 150))[1])
