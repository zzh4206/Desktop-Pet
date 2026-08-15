"""config 读写 —— 设计思路.md §2.2（PetStateStore 侧）/ §十一。

v0.1：最小——读 ``config.example.json`` 作默认值，叠加用户
``config.json``（若存在）；非法用户 config 回退默认值（schema 校验
``jsonschema`` + 回退 + 告警是 v0.2）。``decay_per_hour`` / ``interaction_gain``
/ score 权重是 v0.2 的，v0.1 不预置。
"""

from __future__ import annotations

import json
import logging
import os

log = logging.getLogger("pet")

_DEFAULTS_PATH = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "config.example.json")
)


def _defaults() -> dict:
    with open(_DEFAULTS_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def load_config(config_path: str) -> dict:
    cfg = _defaults()
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                user = json.load(f)
            if isinstance(user, dict):
                cfg.update(user)
        except (OSError, json.JSONDecodeError) as e:
            # v0.1 不做 schema，仅吞掉非法用户 config（v0.2 加告警）
            log.warning("用户 config 非法，回退默认值: %s", e)
    return cfg
