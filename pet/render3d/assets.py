"""资产装载与完整性校验（纯逻辑，零 Qt）。

Balsam 产物目录约定（three_d 资产交付三件套，调研-建模页管线第 7 步）::

    <root>/<stage>/
      model.qml              # Balsam 生成的组件（内含 Model.source 引用）
      meshes/*.mesh          # Balsam 二进制网格
      sidecar/               # 可选 sidecar（缺省走内置默认）
        spring_params.json   #   弹簧骨参数（spring.py 消费）
        expression_map.json  #   表情映射（morph.py 消费）
        skeleton3d.json      #   骨架基准（bone_bridge/anim 消费）

校验失败返回 None（不抛）——调用方据此置 is_ready=False 触发降级（D04）。
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field

logger = logging.getLogger("pet.render3d")

REQUIRED_QML = "model.qml"
MESH_DIR = "meshes"
SIDECAR_DIR = "sidecar"
SIDECAR_FILES = ("spring_params.json", "expression_map.json", "skeleton3d.json",
                 "rig_profile.json")   # 骨名↔语义角色映射（bone_bridge 解耦任意骨架）


@dataclass(frozen=True)
class AssetBundle:
    """一次校验通过的资产清单（路径均绝对路径）。"""

    root: str
    qml_path: str
    mesh_files: tuple[str, ...] = field(default_factory=tuple)
    sidecars: dict = field(default_factory=dict)   # 名（不含 .json）→ dict | None


def _read_json(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def load(stage: str, root: str) -> AssetBundle | None:
    """校验并装载 <root>/<stage>；任何不完整 → None（单行日志，不抛）。"""
    base = os.path.join(root, stage)
    qml = os.path.join(base, REQUIRED_QML)
    if not os.path.isfile(qml):
        logger.warning("render3d 资产不完整：缺 %s", qml)
        return None
    mesh_dir = os.path.join(base, MESH_DIR)
    if not os.path.isdir(mesh_dir):
        logger.warning("render3d 资产不完整：缺目录 %s", mesh_dir)
        return None
    meshes = tuple(
        os.path.join(mesh_dir, f) for f in sorted(os.listdir(mesh_dir))
        if f.endswith(".mesh")
    )
    if not meshes:
        logger.warning("render3d 资产不完整：%s 无 .mesh", mesh_dir)
        return None
    sidecars: dict = {}
    sc_dir = os.path.join(base, SIDECAR_DIR)
    for name in SIDECAR_FILES:
        p = os.path.join(sc_dir, name)
        sidecars[name.removesuffix(".json")] = _read_json(p) if os.path.isfile(p) else None
    return AssetBundle(root=base, qml_path=qml, mesh_files=meshes, sidecars=sidecars)


def default_root() -> str:
    """默认资产根（随包分发形态；开发期可被 RENDER3D_ASSET_ROOT 覆盖）。"""
    env = os.environ.get("RENDER3D_ASSET_ROOT")
    if env:
        return env
    return os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), "three_d_assets", "models")
