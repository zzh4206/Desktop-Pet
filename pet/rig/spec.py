"""rig 清单（manifest.json）定义与校验 —— v0.13。

清单结构（缺任何必填键 → 整体校验失败，调用方回退 frames 模式）::

    {
      "spec": 1,
      "figures": {"neutral": "../../ai/final_healthy_neutral.png", ...},
      "parts": [
        {"id": "tail", "file": "parts/tail.png",
         "source_figure": "neutral",       # 仅该 figure 展示时可见
         "px_rect": [x0, y0, x1, y1],      # 部件在源图中的包围盒（源图像素）
         "pivot": [x, y],                  # 旋转轴（同坐标系）
         "z": "under_core",
         "sway": {"amp_deg": 4, "period_ms": 2600, "phase_ms": 0}}
      ]
    }

校验用 jsonschema Draft7（与 config.py 同套路）；解析后的 ``RigSpec`` 只保留
运行期需要的归一化数据（路径转绝对、数值收敛 float）。
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field

import jsonschema

log = logging.getLogger("pet")

_MANIFEST_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "spec": {"const": 1},
        "figures": {
            "type": "object",
            "minProperties": 1,
            "additionalProperties": {"type": "string"},
        },
        "parts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "minLength": 1},
                    "file": {"type": "string"},
                    "source_figure": {"type": "string"},
                    "px_rect": {
                        "type": "array",
                        "items": {"type": "number"},
                        "minItems": 4,
                        "maxItems": 4,
                    },
                    "pivot": {
                        "type": "array",
                        "items": {"type": "number"},
                        "minItems": 2,
                        "maxItems": 2,
                    },
                    "z": {"enum": ["under_core", "over_core"]},
                    "kind": {"enum": ["sway", "limb"]},
                    "sway": {
                        "type": "object",
                        "properties": {
                            "amp_deg": {"type": "number", "minimum": -45,
                                        "maximum": 45},
                            "period_ms": {"type": "number", "minimum": 200},
                            "phase_ms": {"type": "number", "minimum": 0},
                        },
                        "required": ["amp_deg", "period_ms"],
                        "additionalProperties": False,
                    },
                },
                "required": ["id", "file", "source_figure", "px_rect",
                             "pivot", "z"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["spec", "figures"],
    "additionalProperties": False,
}


@dataclass
class RigPart:
    """一个可动部件（v0.13 鲸尾 sway；v0.14 增 limb=行走驱动肢体）。

    kind 仅是驱动器语义标签：limb 在无 limb 驱动的旧引擎上按 sway 解释
    （amp/period/phase 字段同形），资产向后兼容。
    """

    id: str
    path: str                 # 部件 PNG 绝对路径
    source_figure: str        # 绑定的 figure 名（仅其展示时可见）
    px_rect: tuple            # 源图包围盒 (x0, y0, x1, y1)
    pivot: tuple              # 源图坐标旋转轴
    z: str                    # under_core / over_core
    kind: str = "sway"        # sway=常驻正弦摆 / limb=行走驱动（v0.14）
    amp_deg: float = 0.0
    period_ms: float = 2600.0
    phase_ms: float = 0.0


@dataclass
class RigSpec:
    """解析后的 rig 资产描述（figures：名称→绝对路径）。"""

    stage: str
    figures: dict[str, str] = field(default_factory=dict)
    parts: list[RigPart] = field(default_factory=list)

    def figure_for(self, key: str) -> str | None:
        """按 figure 名取路径；未登记返回 None（调用方走整帧/静帧路径）。"""
        return self.figures.get(key)


def load_rig_spec(rig_dir: str, stage: str) -> RigSpec | None:
    """读 ``{rig_dir}/manifest.json``；缺失/损坏/部件文件丢失一律返回 None。

    宽进严出：任何不合格都降级（app 装配处随即回退 frames），不抛异常——
    展示层永不阻断启动。
    """
    manifest_path = os.path.join(rig_dir, "manifest.json")
    if not os.path.isfile(manifest_path):
        return None
    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        jsonschema.Draft7Validator(_MANIFEST_SCHEMA).validate(raw)
    except (OSError, ValueError, jsonschema.ValidationError) as e:
        log.warning("rig manifest %s 非法，回退帧动画：%s", manifest_path, e)
        return None

    figures: dict[str, str] = {}
    for name, rel in raw["figures"].items():
        p = os.path.normpath(os.path.join(rig_dir, rel))
        if os.path.isfile(p):
            figures[name] = p
        else:
            log.warning("rig figure %s 缺文件（%s），忽略", name, p)
    if not figures:
        log.warning("rig manifest %s 无可用 figure，回退帧动画", manifest_path)
        return None

    parts: list[RigPart] = []
    for item in raw.get("parts", []):
        # 部件绑定的 figure 必须仍存活；部件文件必须存在 —— 否则弃件不弃场
        if item["source_figure"] not in figures:
            log.warning("rig part %s 绑定 figure 不存在，弃件", item["id"])
            continue
        pp = os.path.normpath(os.path.join(rig_dir, item["file"]))
        if not os.path.isfile(pp):
            log.warning("rig part %s 缺文件，弃件", item["id"])
            continue
        sway = item.get("sway", {})
        parts.append(RigPart(
            id=item["id"], path=pp,
            source_figure=item["source_figure"],
            px_rect=tuple(float(v) for v in item["px_rect"]),
            pivot=tuple(float(v) for v in item["pivot"]),
            z=item["z"],
            kind=item.get("kind", "sway"),
            amp_deg=float(sway.get("amp_deg", 0.0)),
            period_ms=float(sway.get("period_ms", 2600.0)),
            phase_ms=float(sway.get("phase_ms", 0.0)),
        ))

    return RigSpec(stage=stage, figures=figures, parts=parts)
