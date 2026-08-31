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
    def get_static(self, state: PetState, skin: str = "default", mood_override: Mood | None = None) -> SpriteRef:
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

    def get_static(self, state: PetState, skin: str = "default", mood_override: Mood | None = None) -> SpriteRef:
        mood = mood_override or _mood_from_state(state, self._idle_s())
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
            assets_dir = os.path.normpath(os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                "..", "assets", "ai",
            ))
        self._dir = assets_dir
        self._miss_cache: set = set()   # 已知缺失文件（防每 tick 重复 stat）
        # M9 修（REVIEW-2026-08-25）：frames_for 结果缓存（含 None 缺帧
        # 哨兵）——walk 模式 _frame_tick 每 50ms 调用，旧版每 tick 重复
        # isfile ≈ 40 stat/s 持续在主线程
        self._frames_cache: dict = {}

    def get_static(self, state: PetState, skin: str = "default", mood_override: Mood | None = None) -> SpriteRef:
        mood = mood_override or _mood_from_state(state, self._fallback._idle_s())
        suffix = "" if skin == "default" else f"_{skin}"
        filename = f"{state.stage.value}_{state.branch.value}"                    f"_{mood.value}{suffix}.png"
        path = os.path.join(self._dir, filename)

        if filename not in self._miss_cache:
            if os.path.isfile(path):
                width, height = _STAGE_SIZE[state.stage]
                return SpriteRef(
                    path=path, width=width, height=height,
                    anchor="bottom_center",
                )
            self._miss_cache.add(filename)

        # 降级：emoji（不区分 miss 原因——缺文件/目录不存在/权限均可）
        return self._fallback.get_static(state, skin)

    def side_walk_static(self, state: PetState,
                         skin: str = "default") -> SpriteRef | None:
        """侧身行走立绘（v0.14.6）—— 从已验收 {stage}_walk_0 帧像素拷贝，
        拆出前后腿 limb 部件做程序化侧身步态；与帧行走同源零生成像素。

        命名挂在 healthy 下仅为复用 {stage}_{branch}_{mood} 的 figure_key
        反解规则；资产与分支无关（两分支共享，同帧行走约定）。缺失→None。
        """
        suffix = "" if skin == "default" else f"_{skin}"
        filename = f"{state.stage.value}_healthy_side{suffix}.png"
        path = os.path.join(self._dir, filename)
        if os.path.isfile(path):
            width, height = _STAGE_SIZE[state.stage]
            return SpriteRef(path=path, width=width, height=height,
                             anchor="bottom_center")
        return None

    def neutral_static(self, state: PetState,
                       skin: str = "default") -> SpriteRef | None:
        """同阶段/分支的 neutral 立绘（v0.14.4 行走覆盖用）。

        paperdoll 行走时把画面切到 neutral 核心（唯一有 limb 腿部件的
        figure），部件步态得以在任意 mood 下接管；文件缺失返回 None
        （调用方维持帧回退）。"""
        suffix = "" if skin == "default" else f"_{skin}"
        filename = (f"{state.stage.value}_{state.branch.value}"
                    f"_neutral{suffix}.png")
        path = os.path.join(self._dir, filename)
        if os.path.isfile(path):
            width, height = _STAGE_SIZE[state.stage]
            return SpriteRef(path=path, width=width, height=height,
                             anchor="bottom_center")
        return None

    # 动作 → 帧名模板（assets/frames/{stage}_{action}.png）与帧间隔
    # v0.13.5/0.13.7：walk 支持可选中间步姿渐进增强——
    #   4 帧: walk_0b(0→1), walk_1b(1→0)
    #   8 帧: 另有 walk_m1(0→0b), walk_m2(0b→1), walk_m3(1→1b), walk_m4(1b→0)
    # frames_for 对缺文件自动跳过 ⇒ 有几张入几张，未产帧阶段自动 2 帧。
    _FRAME_SPECS: dict = {
        "walk":   (("walk_0", "walk_m1", "walk_0b", "walk_m2",
                    "walk_1", "walk_m3", "walk_1b", "walk_m4"), 200),
        "stretch": (("stretch_0", "stretch_1", "stretch_2"), 260),
        "roll":   (("roll",), 260),
        "blink":  (("idle_blink_0", "idle_blink_1"), 300),
        "fall":   (("fall_air", "fall_land"), 200),
        "eat_mouse": (("eat_mouse_0", "eat_mouse_1", "eat_mouse_2", "eat_mouse_3"), 320),
        "chew":   (("chew_0", "chew_1"), 280),
    }

    def _frames_dir(self) -> str:
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
        """按 (stage, action_key) 返回帧序列（无帧 → []，app 走静帧兜底）。

        v0.10.18：SpriteRef 直接填 _STAGE_SIZE 显示档（旧版 width=1/height=1
        依赖调用方先改尺寸——新调用方直接 play_frames 会把窗口 resize 成
        1×1）。
        M9 修（REVIEW-2026-08-25）：stat 结果按 (stage, key) 缓存（缺帧也
        缓存 None 哨兵）。返回防御拷贝（调用方会改 SpriteRef 尺寸，如
        app._play_key 按 window 尺寸覆写）——不污染缓存。帧文件**热替换**
        不受影响：路径不变，window 层缓存键含 mtime 自行失效重读；但
        **新增**此前缺失的帧文件需重启才发现（与 _miss_cache 同语义）。
        """
        spec = self._FRAME_SPECS.get(action_key)
        if spec is None:
            return []
        ck = (stage, action_key)
        if ck not in self._frames_cache:
            try:
                width, height = _STAGE_SIZE[Stage(stage)]
            except (KeyError, ValueError):
                width, height = 192, 192
            refs = []
            for n in spec[0]:
                p = os.path.join(self._frames_dir(), f"{stage}_{n}.png")
                if os.path.isfile(p):
                    refs.append(SpriteRef(
                        path=p, width=width, height=height,
                        anchor="bottom_center",
                    ))
            self._frames_cache[ck] = refs or None
        cached = self._frames_cache[ck]
        if not cached:
            return []
        return [SpriteRef(path=f.path, width=f.width, height=f.height,
                          anchor=f.anchor) for f in cached]

    def frame_interval(self, action_name: str, stage: str | None = None) -> int:
        """帧间隔。v0.13.5/0.13.7：walk 按实际帧数分配步态周期——
        2 帧=200ms（旧行为）；4 帧=140ms（560ms）；8 帧=90ms（720ms，
        单槽硬切下无残影，离散感由帧距减半消除）。
        stage=None 时退回 spec 值（旧调用兼容）。"""
        base = int(self._FRAME_SPECS.get(action_name, ("x", 150))[1])
        if action_name == "walk" and stage is not None:
            cached = self._frames_cache.get((stage, "walk"))
            n = len(cached) if cached else 0
            table = {2: 200, 4: 140, 8: 90}
            if n in table:
                return table[n]
            if n:
                return max(85, 720 // n)
        return base
