"""config 读写 + schema 校验 —— 设计思路.md §2.2（PetStateStore 侧）/ §十一。

v0.2：``decay_per_hour`` / ``interaction_gain`` / ``score`` 权重阈值进默认值，
``jsonschema`` 校验数值范围（非负 / 上限），非法值整段回退默认值 +
``log.warning``。用户 config 深合并到默认值上（嵌套 dict 逐键覆盖）。
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

# 需校验的数值子段 schema（其余键 v0.2 不强校验）
_SECTION_SCHEMAS: dict[str, dict] = {
    "decay_per_hour": {
        "type": "object",
        "properties": {
            "mood": {"type": "number", "minimum": 0, "maximum": 100},
            "fullness": {"type": "number", "minimum": 0, "maximum": 100},
            "cleanliness": {"type": "number", "minimum": 0, "maximum": 100},
        },
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
        "additionalProperties": False,
    },
    "age_speed_multiplier": {"type": "number", "minimum": 0, "maximum": 1000000},
    "evolve_threshold_days": {
        "type": "object",
        "properties": {
            "young": {"type": "number", "minimum": 0, "maximum": 3650},
            "adult": {"type": "number", "minimum": 0, "maximum": 3650},
        },
        "additionalProperties": False,
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


def _validate_sections(cfg: dict, defaults: dict) -> dict:
    """逐段校验数值范围；非法整段回退默认 + 告警。"""
    for key, schema in _SECTION_SCHEMAS.items():
        if key not in cfg:
            continue
        try:
            jsonschema.Draft7Validator(schema).validate(cfg[key])
        except jsonschema.ValidationError as e:
            log.warning(
                "config %s 非法（%s），回退默认值", key, e.message
            )
            cfg[key] = deepcopy(defaults.get(key))
    return cfg


def load_config(config_path: str) -> dict:
    cfg = _defaults()
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                user = json.load(f)
            if not isinstance(user, dict):
                log.warning("用户 config 顶层非 dict，回退默认值")
                return cfg
            cfg = _deep_merge(cfg, user)
        except (OSError, json.JSONDecodeError) as e:
            log.warning("用户 config 非法，回退默认值: %s", e)
            return cfg
    return _validate_sections(cfg, _defaults())
