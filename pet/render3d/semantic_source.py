"""语义对接器（纯逻辑，零 Qt）——2D 线实况 → scene_contract 四通道。

职责边界（设计-子模块与接口.md）：翻译发生在"进 3D 前"的这一层；
本模块不 import app/FSM/Qt——调用方（app 侧胶水 ~10 行）从各处取值填
``PetSnapshot``，本模块负责词表映射与合法性：

  ActionType / FSM 实况 → PoseSemantics.action_id + phase
    walking(motion_mode/move_to) → walk（phase 由 walk_hz 积分喂入）
    airborne / FALL             → drag（提起-悬空语义最近似；renderer 未
                                  实现时契约回落 idle，无害）
    ANIMATE / EAT_MOUSE / 点击类 → click（反应动画）
    其余（SPEAK/静止）           → idle（气泡是 2D 叠加层，不进 3D）
  情绪 → ExpressionState.emotion_label
    chat_emotion 的显式标签优先（已是 VRM 5 情绪词表 + 5 分钟回落 neutral）；
    无显式标签时按 mood 阈值兜底：≥85 happy / <30 sad / 其余 neutral。
  sun.py SunPosition → LightWeatherState（方位/高度直传；色温按高度分档
    晨昏暖-正午白；强度=max(0.05, sin(elevation)) 的昼夜调制；夜间渲染侧处理）
  wind → wind_speed 直传（m/s 语义量）

所有输出经契约自身校验（非法词表会在构造时抛错——本层在映射表里就保证合法）。
"""

from __future__ import annotations

from dataclasses import dataclass

from pet.scene_contract import (ACTION_CLICK, ACTION_DRAG, ACTION_IDLE,
                                ACTION_WALK, EXP_HAPPY, EXP_NEUTRAL, EXP_SAD,
                                EXPRESSION_PRESETS, ExpressionState,
                                LightWeatherState, PoseSemantics, SceneState)

# ActionType 名（字符串比较，避免 import 2D 线模块）
_MOVE_TO, _FALL, _ANIMATE, _SPEAK, _EAT_MOUSE = (
    "move_to", "fall", "animate", "speak", "eat_mouse")


@dataclass
class PetSnapshot:
    """调用方逐帧/逐拍填充的 2D 线实况快照（全默认=安全的中性静止）。"""

    action_type: str = _SPEAK            # 当前 ActionType 名（缺省无害）
    walking: bool = False                # FSM 移动中（move_to/漫游）
    walk_phase: float = 0.0              # 步频积分相位（0-1，调用方推进）
    airborne: bool = False               # 悬空（拖起/坠落）
    mood: float = 80.0                   # pet_state.mood（0-100）
    emotion_label: str | None = None     # chat_emotion 显式标签（优先）
    sun_azimuth_deg: float = 0.0         # sun.py SunPosition.azimuth_deg
    sun_elevation_deg: float = 0.0       # sun.py SunPosition.elevation_deg
    wind_speed: float = 0.0              # m/s
    blink_progress: float = 0.0          # 0-1（motion 眨眼脉冲）
    gaze_x: float = 0.0                  # -1..1
    gaze_y: float = 0.0


def _emotion_of(snap: PetSnapshot) -> str | None:
    if snap.emotion_label in EXPRESSION_PRESETS:
        return snap.emotion_label
    if snap.emotion_label == "neutral":
        return EXP_NEUTRAL
    if snap.mood >= 85.0:
        return EXP_HAPPY
    if snap.mood < 30.0:
        return EXP_SAD
    return EXP_NEUTRAL


def _action_of(snap: PetSnapshot) -> tuple[str, float]:
    if snap.airborne or snap.action_type == _FALL:
        return ACTION_DRAG, 0.5            # 悬空段（提起-放下三段的中间档）
    if snap.walking or snap.action_type == _MOVE_TO:
        return ACTION_WALK, snap.walk_phase % 1.0
    if snap.action_type in (_ANIMATE, _EAT_MOUSE):
        return ACTION_CLICK, 0.0
    return ACTION_IDLE, 0.0


def _color_temp_k(elev_deg: float) -> float:
    if elev_deg < 0.0:
        return 2700.0                      # 夜/晨昏前：暖烛光（渲染侧再压强度）
    if elev_deg < 15.0:
        return 3200.0                      # 晨昏金
    if elev_deg < 45.0:
        return 5000.0                      # 白昼
    return 6500.0                          # 正午


def scene_state(snap: PetSnapshot) -> SceneState:
    """PetSnapshot → SceneState（输出保证通过契约校验）。"""
    import math

    action_id, phase = _action_of(snap)
    elev = max(-90.0, min(90.0, snap.sun_elevation_deg))
    return SceneState(
        light=LightWeatherState(
            sun_azimuth_deg=snap.sun_azimuth_deg % 360.0,
            sun_elevation_deg=elev,
            sun_intensity=max(0.05, math.sin(math.radians(max(0.0, elev)))),
            color_temp_k=_color_temp_k(elev),
            wind_speed=max(0.0, snap.wind_speed),
        ),
        pose=PoseSemantics(action_id=action_id, phase=phase),
        expression=ExpressionState(
            emotion_label=_emotion_of(snap),
            blink_progress=max(0.0, min(1.0, snap.blink_progress)),
            gaze_x=max(-1.0, min(1.0, snap.gaze_x)),
            gaze_y=max(-1.0, min(1.0, snap.gaze_y)),
        ),
    )
