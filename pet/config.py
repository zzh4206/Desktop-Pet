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
# v2（0.19.1）：interaction_gain.poke 旧默认 -8 → 4（动词换代"逗一逗"取正语义）
CONFIG_VERSION = 2

# 回退到默认仍非法时的硬编码安全默认（防 example.json 本身被改坏）
# 批次J/L14（REVIEW-2026-08-31 F21）：safe defaults 必须过同名 schema
# 终检（spikes/test_v11_hotkey_parse_win.py 锁定）；新增段须同步补此表
_SAFE_DEFAULTS: dict = {
    "decay_per_hour": {"mood": 2.0, "fullness": 3.0, "cleanliness": 1.5},
    "interaction_gain": {"pet": 5, "feed": 20, "clean": 15, "poke": 4},
    # v0.19.0 F3：交互文案池覆盖（interaction.messages 平铺覆盖该交互全池）；
    # v0.19.1 F6/F7：饱和拒绝阈值 + 疲劳窗口（次数/分钟）
    "interaction": {"messages": {}, "reject_fullness": 92,
                    "fatigue_times": 5, "fatigue_window_min": 10},
    # v0.19.0 F4：交互音效管线（默认关；资产 assets/sounds/ 后补不阻塞）
    "sound": {"enabled": False, "volume": 0.6},
    "score": {
        "mood_weight": 0.4,
        "fullness_weight": 0.4,
        "cleanliness_weight": 0.2,
        "healthy_threshold": 70,
    },
    "age_speed_multiplier": 1,
    "evolve_threshold_days": {"young": 7, "adult": 21},
    "behavior": {
        "walk_speed": 80,
        "follow_speed": 600,
        "wander_idle_min_s": 5,
        "wander_idle_max_s": 15,
        "first_idle_s": 3,
        "edge_margin_px": 40,
        "climb_min_depth_px": 30,
        # pet_height_px 不再入默认（批次C/P3-10：开机即被真实显示尺寸覆盖
        # 的死旋钮，example 已移除；behavior.py 仍留作启动前初始兜底）
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
        # v0.19.2 F8：需求求助触发线（数值低于阈值宠物主动开口；空对象关闭）
        "need_bubble": {"fullness": 30, "cleanliness": 25, "mood": 20},
        "need_cooldown_min": 120,
        # v0.19.4 F16：用户生日 "MM-DD"（空=不启用；当日一次祝福+心情奖励）
        "birthday": "",
    },
    # 批次J/L14（F23）：以下段补 schema 校验，safe defaults 同步补齐
    "provider": "emoji",
    "presentation": "rig",
    # G6：ADULT 行走——legacy = 正面原地步态（旧行为）；side_rig = 转身片段 + 侧身骨骼行走
    "adult_locomotion": "side_rig",
    "final_locomotion": "side_rig",
    "log_level": "INFO",
    "sleepy_idle_minutes": 10,
    "hotkeys": {},
    "llm": {"providers": {}},  # v0.20: selected 亦合法（schema 见 _SECTION_SCHEMAS）
    "chat_emotion": {
        "enabled": True, "schedule": ["22:00"],
        "retention_hours": 48, "expression_minutes": 5,
        "confidence_threshold": 0.55, "event_confidence_threshold": 0.5,
        "mood_delta": {"happy": 4, "neutral": 0, "sad": -3,
                        "sleepy": -1, "hungry": -2},
    },
    # v0.16 风通道：实时风力 → 立绘静止摆幅（Open-Meteo 免 key + 兜底）
    "wind": {
        "enabled": False, "poll_minutes": 20, "fallback_gain": 1.0,
    },
    # v0.17 光影通道：实时太阳位置 → 地面阴影（纯本地计算，无网络）
    "sun": {
        "enabled": False, "shadow_alpha": 0.4,
    },
    # three_d 3D 渲染实验线（D05：默认关、云端推送恒 false；D16 安全上限制）
    "render3d": {
        "enabled": False, "stage": "adult", "light_level": 1,
        "fps_cap": 30, "safe_rss_mb": 500.0,
    },
    # v0.19.8 帧率分档（pet/perf.py）：auto=平台判档+过载自动降档；手动档
    # 不降档。菜单「流畅度」选择持久化到用户 config 的本段。
    "performance": {"frame_tier": "auto"},
    # v0.19.8 养成联动（pet/needs.py）：饿/脏给心情加压，双高抵扣心情衰减。
    # 均为点/小时（drag_*/content_* 为触发阈值）。
    "needs": {
        "hunger_mood_drag": 1.5, "dirt_mood_drag": 1.0,
        "content_mood_gain": 2.0, "drag_fullness": 30,
        "drag_cleanliness": 25, "content_fullness": 80,
        "content_cleanliness": 65,
    },
    # v0.21 mini-swe 集成（pet/swe_env.py + swe_agent.py）：沙箱 CLI 能力。
    # 默认关；首次经聊天 swe_task 工具触发时确认后启用。
    "swe": {
        "enabled": False, "workspace_dir": "", "step_limit": 10,
        "wall_time_s": 300.0, "command_timeout_s": 30.0,
        "output_max_chars": 8000, "no_network": False,
        "block_patterns": [],
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
    # v0.19.0 F3：文案池覆盖——平铺 list 覆盖该交互全池（不分 mood 桶）；
    # v0.19.1 F6/F7：拒绝阈值与疲劳窗口
    "interaction": {
        "type": "object",
        "properties": {
            "messages": {
                "type": "object",
                "properties": {
                    "pet": {"type": "array", "minItems": 1,
                            "items": {"type": "string", "minLength": 1}},
                    "feed": {"type": "array", "minItems": 1,
                             "items": {"type": "string", "minLength": 1}},
                    "clean": {"type": "array", "minItems": 1,
                              "items": {"type": "string", "minLength": 1}},
                    "poke": {"type": "array", "minItems": 1,
                             "items": {"type": "string", "minLength": 1}},
                },
                "additionalProperties": False,
            },
            "reject_fullness": {"type": "number", "minimum": 0,
                                "maximum": 100},
            "fatigue_times": {"type": "integer", "minimum": 2, "maximum": 100},
            "fatigue_window_min": {"type": "number", "minimum": 0.01,
                                   "maximum": 1440},
        },
        "required": ["messages"],
        "additionalProperties": False,
    },
    # v0.19.0 F4：音效段（资产缺失不影响校验，仅开关与音量）
    "sound": {
        "type": "object",
        "properties": {
            "enabled": {"type": "boolean"},
            "volume": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        },
        "required": ["enabled", "volume"],
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
                # L6（REVIEW-2026-09-04）：限整数——旧版 number 放行 22.5，
                # proactive 构造校验 int() 截断后存原 float，_quiet_end_after
                # 的 replace(hour=float) 抛异常且 22.5 实际 23 点才静默
                "type": "array",
                "items": {"type": "integer", "minimum": 0, "maximum": 23},
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
            # v0.19.2 F8：需求求助触发线与冷却
            "need_bubble": {
                "type": "object",
                "properties": {
                    "fullness": {"type": "number", "minimum": 0,
                                 "maximum": 100},
                    "cleanliness": {"type": "number", "minimum": 0,
                                    "maximum": 100},
                    "mood": {"type": "number", "minimum": 0,
                             "maximum": 100},
                },
                "additionalProperties": False,
            },
            "need_cooldown_min": {"type": "number", "minimum": 0.01,
                                  "maximum": 1440},
            # v0.19.4 F16：用户生日（MM-DD；空串=不启用）
            "birthday": {"type": "string", "pattern": "^(|(0[1-9]|1[0-2])-"
                                                   "(0[1-9]|[12][0-9]|3[01]))$"},
        },
        "additionalProperties": False,
    },
    # 批次J/L14（REVIEW-2026-08-31 F23）：此前这些段无 schema——
    # 非法值（如 presentation 拼错）静默漏过，行为与预期脱节无告警
    "provider": {"enum": ["emoji", "ai", "commission"]},
    "presentation": {"enum": ["frames", "rig", "paperdoll"]},
    "adult_locomotion": {"enum": ["legacy", "side_rig"]},
    "final_locomotion": {"enum": ["legacy", "side_rig"]},
    "log_level": {"enum": ["DEBUG", "INFO", "WARNING", "ERROR"]},
    # 批次C/P3-10（REVIEW-2026-09-05）：user_name 入 schema——此前只被
    # app 读取（ToolContext.user_name）却无处可配（示例/校验双缺，改值
    # 静默绕过校验）
    "user_name": {"type": "string", "minLength": 1, "maxLength": 32},
    # L8（REVIEW-2026-09-04）：0=禁用睡姿（旧版 0 → 门限 0s 恒 SLEEPY，
    # 且配置值此前从未真正接入判定——见 asset_provider._mood_from_state）
    "sleepy_idle_minutes": {"type": "number", "minimum": 0,
                            "maximum": 1440},
    "hotkeys": {
        "type": "object",
        "properties": {
            "chat": {"type": "string", "minLength": 1},
            "spit": {"type": "string", "minLength": 1},
        },
        "additionalProperties": False,
    },
    "llm": {
        "type": "object",
        "properties": {
            # v0.20 模型管理：selected=当前使用的 provider 名（模型管理对话框/
            # 托盘切换时写回，启动据此免弹选）。
            # providers **保持宽松**（仅 object）——严格条目校验失败会整段回退
            # example 默认，用户自定义 provider 从合并配置里消失后，对话框
            # 下一次提交会把盘上自定义条目抹掉（数据丢失）；条目合法性由
            # model_registry.validate_entry 在对话框入口把关，坏 URL 会在
            # 请求期可见地失败，好过静默吞配置。
            "selected": {"type": "string"},
            "providers": {"type": "object"},
            "max_tokens": {"type": "number", "minimum": 256,
                           "maximum": 128000},
            "stream_total_s": {"type": "number", "minimum": 10,
                               "maximum": 3600},
        },
        "additionalProperties": True,
    },
    "chat_emotion": {
        "type": "object",
        "properties": {
            "enabled": {"type": "boolean"},
            "schedule": {"type": "array", "minItems": 1, "maxItems": 8,
                         # L7（REVIEW-2026-09-04）：旧版 ^[0-2][0-9]:… 放行
                         # 24-29 点，due_slots 字符串比较永不触发=静默无效
                         "items": {"type": "string",
                                   "pattern": "^(?:[01][0-9]|2[0-3]):[0-5][0-9]$"}},
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
    "wind": {
        "type": "object",
        "properties": {
            "enabled": {"type": "boolean"},
            "latitude": {"type": "number", "minimum": -90, "maximum": 90},
            "longitude": {"type": "number", "minimum": -180, "maximum": 180},
            "poll_minutes": {"type": "number", "minimum": 5, "maximum": 1440},
            "fallback_gain": {"type": "number", "minimum": 0.0,
                              "maximum": 4.0},
        },
        "additionalProperties": False,
    },
    "sun": {
        "type": "object",
        "properties": {
            "enabled": {"type": "boolean"},
            "latitude": {"type": "number", "minimum": -90, "maximum": 90},
            "longitude": {"type": "number", "minimum": -180, "maximum": 180},
            "timezone_offset": {"type": ["number", "null"],
                                "minimum": -14.0, "maximum": 14.0},
            "shadow_alpha": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        },
        "additionalProperties": False,
    },
    # three_d 3D 渲染实验线（three_d/wiki/设计-子模块与接口.md §4）
    "render3d": {
        "type": "object",
        "properties": {
            "enabled": {"type": "boolean"},
            "stage": {"type": "string", "enum": ["young", "adult", "final"]},
            "light_level": {"type": "integer", "minimum": 0, "maximum": 3},
            "fps_cap": {"type": "integer", "minimum": 5, "maximum": 120},
            "safe_rss_mb": {"type": "number", "minimum": 100, "maximum": 5000},
        },
        "additionalProperties": False,
    },
    # v0.19.8 帧率分档（pet/perf.py）
    "performance": {
        "type": "object",
        "properties": {
            "frame_tier": {"enum": ["auto", "low", "medium", "high"]},
        },
        "required": ["frame_tier"],
        "additionalProperties": False,
    },
    # v0.19.8 养成联动（pet/needs.py；界与 needs._INTERLOCK_MAX 一致）
    "needs": {
        "type": "object",
        "properties": {
            "hunger_mood_drag": {"type": "number", "minimum": 0,
                                 "maximum": 50},
            "dirt_mood_drag": {"type": "number", "minimum": 0, "maximum": 50},
            "content_mood_gain": {"type": "number", "minimum": 0,
                                  "maximum": 50},
            "drag_fullness": {"type": "number", "minimum": 0,
                              "maximum": 100},
            "drag_cleanliness": {"type": "number", "minimum": 0,
                                 "maximum": 100},
            "content_fullness": {"type": "number", "minimum": 0,
                                 "maximum": 100},
            "content_cleanliness": {"type": "number", "minimum": 0,
                                    "maximum": 100},
        },
        "additionalProperties": False,
    },
    # v0.21 mini-swe 集成（pet/swe_env.py + swe_agent.py）
    "swe": {
        "type": "object",
        "properties": {
            "enabled": {"type": "boolean"},
            "workspace_dir": {"type": "string", "maxLength": 512},
            "step_limit": {"type": "integer", "minimum": 1, "maximum": 100},
            "wall_time_s": {"type": "number", "minimum": 0, "maximum": 3600},
            "command_timeout_s": {"type": "number", "minimum": 1,
                                  "maximum": 600},
            "output_max_chars": {"type": "integer", "minimum": 256,
                                 "maximum": 100000},
            "no_network": {"type": "boolean"},
            "block_patterns": {"type": "array", "maxItems": 50,
                               "items": {"type": "string", "minLength": 1,
                                         "maxLength": 256}},
        },
        "required": ["enabled"],
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


def _migrate(cfg: dict, defaults: dict) -> dict:
    """config_version 迁移钩子（对齐 pet_state SCHEMA_VERSION 体系）。

    v2（0.19.1）：interaction_gain.poke 旧默认 -8 → 4。后续 schema 变更
    在此追加 ``if cfg.get("config_version", 0) < N: ...`` 分支，逐版本升级。
    """
    ver = cfg.get("config_version", 0)
    if ver < 2:
        # 0.19.1 动词换代"逗一逗"取正增益；旧默认 -8 自动迁移（想保留负 poke
        # 可手改——负反馈语义如今由拒绝/疲劳通道承担，不再靠"戳"）
        gain = cfg.get("interaction_gain")
        if isinstance(gain, dict) and gain.get("poke") == -8:
            gain["poke"] = 4
    if ver < CONFIG_VERSION:
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
        except (OSError, ValueError) as e:
            # 批次B/P2-1（REVIEW-2026-09-05）：补 ValueError——用户手编 config
            # 存成 GBK/含坏字节时 open(...encoding="utf-8") 抛 UnicodeDecodeError
            # （是 ValueError 不是 JSONDecodeError），旧版逃出 except →
            # app.py 裸调 load_config 启动即崩，删文件才能恢复。非法也过校验
            # （防默认值本身非法）
            log.warning("用户 config 非法，回退默认值: %s", e)
            return _validate_sections(cfg, defaults)
    cfg = _migrate(cfg, defaults)
    return _validate_sections(cfg, defaults)
