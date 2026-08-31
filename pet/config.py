"""config 读写 + schema 校验 —— 设计思路.md §2.2（PetStateStore 侧）/ §十一。

v0.2：``decay_per_hour`` / ``interaction_gain`` / ``score`` 权重阈值进默认值，
``jsonschema`` 校验数值范围（非负 / 上限），非法值整段回退默认值 +
``log.warning``。用户 config 深合并到默认值上（嵌套 dict 逐键覆盖）。

v0.4.13：补 ``behavior`` / ``proactive`` 段 schema；``_validate_sections`` 回退后
再校验一次，仍非法用代码内硬编码安全默认（防默认值本身非法致"回退到自身"死
循环，见 v0.4.13 前的 decay=400 案例）；加 ``config_version`` 迁移钩子；
``_defaults`` 缓存到局部变量；非法 JSON 路径也过校验；``score`` 加 required。
"""

from __future__ import annotations

import json
import logging
import os
from copy import deepcopy

import jsonschema

log = logging.getLogger("pet")

_DEFAULTS_PATH = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "config.example.json")
)

# config schema 版本（迁移链入口，对齐 pet_state SCHEMA_VERSION 体系）
CONFIG_VERSION = 1

# 回退到默认仍非法时的硬编码安全默认（防 example.json 本身被改坏）
_SAFE_DEFAULTS: dict[str, dict] = {
    "decay_per_hour": {"mood": 2.0, "fullness": 3.0, "cleanliness": 1.5},
    "interaction_gain": {"pet": 5, "feed": 20, "clean": 15, "poke": -8},
    "score": {
        "mood_weight": 0.4,
        "fullness_weight": 0.4,
        "cleanliness_weight": 0.2,
        "healthy_threshold": 70,
    },
    "age_speed_multiplier": 1,
    "evolve_threshold_days": {"young": 7, "adult": 21},
    "behavior": {
        "walk_speed": 120,
        "follow_speed": 600,
        "wander_idle_min_s": 5,
        "wander_idle_max_s": 15,
        "first_idle_s": 3,
        "edge_margin_px": 40,
        "climb_min_depth_px": 30,
        "pet_height_px": 64,
    },
    "proactive": {
        "quiet_hours": [23, 8],
        "sedentary_min": 45,
        "sedentary_cooldown_min": 30,
        "idle_threshold_min": 5,
        "eat_mouse_duration_s": 10,
        "dnd": False,
        "video_apps": [],
        "eat_mouse_gain": {"fullness": 5, "mood": 3},
    },
    "chat_emotion": {
        "enabled": True, "schedule": ["22:00"],
        "retention_hours": 48, "expression_minutes": 5,
        "confidence_threshold": 0.55, "event_confidence_threshold": 0.5,
        "mood_delta": {"happy": 4, "neutral": 0, "sad": -3,
                        "sleepy": -1, "hungry": -2},
    },
}

# 需校验的数值子段 schema（其余键 v0.2 不强校验）
_SECTION_SCHEMAS: dict[str, dict] = {
    "decay_per_hour": {
        "type": "object",
        "properties": {
            "mood": {"type": "number", "minimum": 0, "maximum": 100},
            "fullness": {"type": "number", "minimum": 0, "maximum": 100},
            "cleanliness": {"type": "number", "minimum": 0, "maximum": 100},
        },
        "required": ["mood", "fullness", "cleanliness"],
        "additionalProperties": False,
    },
    "interaction_gain": {
        "type": "object",
        "properties": {
            "pet": {"type": "number", "minimum": -100, "maximum": 100},
            "feed": {"type": "number", "minimum": -100, "maximum": 100},
            "clean": {"type": "number", "minimum": -100, "maximum": 100},
            "poke": {"type": "number", "minimum": -100, "maximum": 100},
        },
        "required": ["pet", "feed", "clean", "poke"],
        "additionalProperties": False,
    },
    "score": {
        "type": "object",
        "properties": {
            "mood_weight": {"type": "number", "minimum": 0, "maximum": 1},
            "fullness_weight": {"type": "number", "minimum": 0, "maximum": 1},
            "cleanliness_weight": {"type": "number", "minimum": 0, "maximum": 1},
            "healthy_threshold": {
                "type": "number",
                "minimum": 0,
                "maximum": 100,
            },
        },
        "required": [
            "mood_weight",
            "fullness_weight",
            "cleanliness_weight",
            "healthy_threshold",
        ],
        "additionalProperties": False,
    },
    "age_speed_multiplier": {"type": "number", "minimum": 0, "maximum": 1000000},
    "evolve_threshold_days": {
        "type": "object",
        "properties": {
            "young": {"type": "number", "minimum": 0, "maximum": 3650},
            "adult": {"type": "number", "minimum": 0, "maximum": 3650},
        },
        "required": ["young", "adult"],
        "additionalProperties": False,
    },
    "behavior": {
        "type": "object",
        "properties": {
            "walk_speed": {"type": "number", "minimum": 0, "maximum": 2000},
            "follow_speed": {"type": "number", "minimum": 0, "maximum": 5000},
            "wander_idle_min_s": {"type": "number", "minimum": 0, "maximum": 600},
            "wander_idle_max_s": {"type": "number", "minimum": 0, "maximum": 3600},
            "first_idle_s": {"type": "number", "minimum": 0, "maximum": 600},
            "edge_margin_px": {"type": "number", "minimum": 0, "maximum": 500},
            "climb_min_depth_px": {"type": "number", "minimum": 0, "maximum": 200},
            "pet_height_px": {"type": "number", "minimum": 1, "maximum": 500},
        },
        "additionalProperties": False,
    },
    "proactive": {
        "type": "object",
        "properties": {
            "quiet_hours": {
                "type": "array",
                "items": {"type": "number", "minimum": 0, "maximum": 23},
                "minItems": 2,
                "maxItems": 2,
            },
            # 最小 0.01min(0.6s)：测试需亚分钟阈值；example 的 0.1 也合法
            "sedentary_min": {"type": "number", "minimum": 0.01,
                              "maximum": 480},
            "sedentary_cooldown_min": {"type": "number", "minimum": 0.01,
                                       "maximum": 480},
            "festivals": {"type": "object"},
            # v0.7 键（此前 schema 漏配，additionalProperties:false 致整段
            # 回退默认——久坐 45min 永不触发，吃鼠标测试无门）
            "idle_threshold_min": {"type": "number", "minimum": 0.01,
                                   "maximum": 480},
            "eat_mouse_duration_s": {"type": "number", "minimum": 0.3,
                                     "maximum": 15},
            "dnd": {"type": "boolean"},
            "video_apps": {"type": "array",
                           "items": {"type": "string"}},
            # M10 修（REVIEW-2026-08-25）：代码读此键定制气泡吐出热键文案
            # （proactive.py），旧版 schema 未收——用户一配即整段校验失败
            # 回退默认（quiet_hours/sedentary 等自定义全丢）
            "eat_mouse_hotkey_label": {"type": "string"},
            "eat_mouse_gain": {
                "type": "object",
                "properties": {
                    "fullness": {"type": "number", "minimum": -100,
                                 "maximum": 100},
                    "mood": {"type": "number", "minimum": -100,
                             "maximum": 100},
                },
                "additionalProperties": False,
            },
        },
        "additionalProperties": False,
    },
    "chat_emotion": {
        "type": "object",
        "properties": {
            "enabled": {"type": "boolean"},
            "schedule": {"type": "array", "minItems": 1, "maxItems": 8,
                         "items": {"type": "string", "pattern": "^[0-2][0-9]:[0-5][0-9]$"}},
            "retention_hours": {"type": "number", "minimum": 1, "maximum": 168},
            "expression_minutes": {"type": "number", "minimum": 1, "maximum": 60},
            "confidence_threshold": {"type": "number", "minimum": 0, "maximum": 1},
            "event_confidence_threshold": {"type": "number", "minimum": 0, "maximum": 1},
            "mood_delta": {"type": "object", "properties": {
                "happy": {"type": "number", "minimum": -20, "maximum": 20},
                "neutral": {"type": "number", "minimum": -20, "maximum": 20},
                "sad": {"type": "number", "minimum": -20, "maximum": 20},
                "sleepy": {"type": "number", "minimum": -20, "maximum": 20},
                "hungry": {"type": "number", "minimum": -20, "maximum": 20},
            }, "required": ["happy", "neutral", "sad", "sleepy", "hungry"],
            "additionalProperties": False},
        }, "additionalProperties": False,
    },
}


def _defaults() -> dict:
    with open(_DEFAULTS_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _deep_merge(base: dict, overlay: dict) -> dict:
    """嵌套 dict 逐键覆盖；非 dict 值直接替换。"""
    out = deepcopy(base)
    for k, v in overlay.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = deepcopy(v)
    return out


def _migrate(cfg: dict, defaults: dict) -> dict:
    """config_version 迁移钩子（对齐 pet_state SCHEMA_VERSION 体系）。

    当前 CONFIG_VERSION=1，无历史版本需迁移。未来 schema 变更在此追加
    ``if cfg.get("config_version", 0) < N: ...`` 分支，逐版本升级。
    """
    ver = cfg.get("config_version", 0)
    if ver < CONFIG_VERSION:
        # 占位：未来按版本号差值链式迁移字段
        cfg["config_version"] = CONFIG_VERSION
    return cfg


def _validate_section(cfg: dict, key: str, schema: dict, defaults: dict) -> None:
    """校验单个段；非法先回退默认值，默认值仍非法再回退硬编码安全默认。"""
    if key not in cfg:
        return
    try:
        jsonschema.Draft7Validator(schema).validate(cfg[key])
        return
    except jsonschema.ValidationError as e:
        log.warning("config %s 非法（%s），回退默认值", key, e.message)
    # 回退到默认值
    fallback = deepcopy(defaults.get(key))
    if fallback is not None:
        try:
            jsonschema.Draft7Validator(schema).validate(fallback)
            cfg[key] = fallback
            return
        except jsonschema.ValidationError:
            log.error("config %s 默认值也非法，回退硬编码安全默认", key)
    # 默认值也非法 → 硬编码安全默认
    safe = deepcopy(_SAFE_DEFAULTS.get(key))
    if safe is not None:
        cfg[key] = safe


def _validate_sections(cfg: dict, defaults: dict) -> dict:
    """逐段校验数值范围；非法回退默认，默认仍非法回退硬编码安全默认。"""
    for key, schema in _SECTION_SCHEMAS.items():
        _validate_section(cfg, key, schema, defaults)
    return cfg


def load_config(config_path: str) -> dict:
    defaults = _defaults()
    cfg = deepcopy(defaults)
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                user = json.load(f)
            if not isinstance(user, dict):
                log.warning("用户 config 顶层非 dict，回退默认值")
                return _validate_sections(cfg, defaults)
            cfg = _deep_merge(cfg, user)
        except (OSError, json.JSONDecodeError) as e:
            log.warning("用户 config 非法，回退默认值: %s", e)
            # 非法 JSON 也过校验（防默认值本身非法）
            return _validate_sections(cfg, defaults)
    cfg = _migrate(cfg, defaults)
    return _validate_sections(cfg, defaults)
