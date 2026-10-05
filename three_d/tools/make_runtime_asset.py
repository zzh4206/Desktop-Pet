#!/usr/bin/env python3
"""运行时资产安装器：任意 skinned GLB → Balsam 转换 → three_d_assets 落位
→ 自动生成 rig_profile.json（骨名↔语义角色映射，展示层解耦的关键 sidecar）。

产物结构（assets.py 的约定）::
    three_d_assets/models/<stage>/
      model.qml            # Balsam 组件（Joint 按 objectName=骨名寻址）
      meshes/*.mesh
      maps/*
      sidecar/
        skeleton3d.json    # 若提供基准骨架 json 则拷入
        rig_profile.json   # joints 序 + roles 映射 + 根节点提示

roles 生成策略（任意模型适配）：
  1. skeleton3d json 的 bone 带 vrm 字段 → 直接映射（VRM humanoid 名）
  2. 骨名模式匹配兜底（hips/spine/chest/neck/head/arm/hand/leg/foot 等常见词，
     兼容 snake_case 与 CamelCase）
  3. 都不认识的角色直接缺省——运行时按"有则动、无则静"降级

用法：
  python make_runtime_asset.py <skinned.glb> <stage> [--skeleton skeleton3d.json]
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

BALSAM = "/Users/zzh4206/Library/Python/3.9/bin/pyside6-balsam"
MODELS_ROOT = Path("/Users/zzh4206/Desktop_Pet/three_d_assets/models")

# 语义角色 ← 骨名模式（顺序敏感：先具体后泛化）
ROLE_PATTERNS: list[tuple[str, str]] = [
    ("hips", r"(hips|pelvis|root_hip|waist)"),
    ("spine", r"spine(\d*|_lower)?$|spine_01$"),
    ("chest", r"chest|spine_0[23]|upper_chest"),
    ("neck", r"neck"),
    ("head", r"head(_top)?$"),
    ("arm_upper_l", r"(upper[_]?arm|shoulder|arm)[_]?l(eft)?$"),
    ("arm_lower_l", r"(fore[_]?arm|lower[_]?arm|elbow)[_]?l(eft)?$"),
    ("hand_l", r"(hand|wrist)[_]?l(eft)?$"),
    ("arm_upper_r", r"(upper[_]?arm|shoulder|arm)[_]?r(ight)?$"),
    ("arm_lower_r", r"(fore[_]?arm|lower[_]?arm|elbow)[_]?r(ight)?$"),
    ("hand_r", r"(hand|wrist)[_]?r(ight)?$"),
    ("leg_upper_l", r"(upper[_]?leg|thigh|leg)[_]?l(eft)?$"),
    ("leg_lower_l", r"(lower[_]?leg|calf|knee|shin)[_]?l(eft)?$"),
    ("foot_l", r"(foot|ankle)[_]?l(eft)?$"),
    ("leg_upper_r", r"(upper[_]?leg|thigh|leg)[_]?r(ight)?$"),
    ("leg_lower_r", r"(lower[_]?leg|calf|knee|shin)[_]?r(ight)?$"),
    ("foot_r", r"(foot|ankle)[_]?r(ight)?$"),
]
VRM_TO_ROLE = {
    "hips": "hips", "spine": "spine", "chest": "chest", "neck": "neck",
    "head": "head",
    "leftUpperArm": "arm_upper_l", "leftLowerArm": "arm_lower_l",
    "leftHand": "hand_l",
    "rightUpperArm": "arm_upper_r", "rightLowerArm": "arm_lower_r",
    "rightHand": "hand_r",
    "leftUpperLeg": "leg_upper_l", "leftLowerLeg": "leg_lower_l",
    "leftFoot": "foot_l",
    "rightUpperLeg": "leg_upper_r", "rightLowerLeg": "leg_lower_r",
    "rightFoot": "foot_r",
}


def glb_skin_joints(glb: Path) -> tuple[list[str], list[str], dict]:
    """读 GLB：返回 (skin joints 骨名序, 全部节点名, {骨名: rest 变换})。

    rest 变换 = glTF 节点的 TRS（未给 rotation 的按 identity）——运行时
    程序化动画要在 Python 端合成 绝对四元数 = rest ⊗ delta，QML 只赋值。
    """
    raw = glb.read_bytes()
    assert raw[:4] == b"glTF", "不是 GLB"
    ln = struct.unpack("<I", raw[12:16])[0]
    g = json.loads(raw[20:20 + ln])
    names = [n.get("name", f"node{i}") for i, n in enumerate(g.get("nodes", []))]
    rest: dict[str, list[float]] = {}
    for i, n in enumerate(g.get("nodes", [])):
        t = n.get("translation") or [0.0, 0.0, 0.0]
        q = n.get("rotation") or [0.0, 0.0, 0.0, 1.0]   # glTF: xyzw
        rest[names[i]] = [*t, *q]
    skins = g.get("skins") or []
    if not skins:
        return [], names, rest
    joints = [names[j] for j in skins[0]["joints"]]
    return joints, names, rest


def build_roles(joints: list[str], skeleton: dict | None) -> dict[str, str]:
    """语义角色 → 骨名。VRM 字段优先，模式匹配兜底；只保留首个命中。"""
    roles: dict[str, str] = {}
    vrm_of = {}
    for b in (skeleton or {}).get("bones") or []:
        v = b.get("vrm")
        if v and v in VRM_TO_ROLE:
            vrm_of[b.get("name") or b.get("bone_name", "")] = VRM_TO_ROLE[v]
    for j in joints:
        if j in vrm_of and vrm_of[j] not in roles:
            roles[vrm_of[j]] = j
    for role, pat in ROLE_PATTERNS:
        if role in roles:
            continue
        rx = re.compile(pat, re.IGNORECASE)
        for j in joints:
            if j in vrm_of:            # 已被 VRM 映射占用的骨不参与兜底
                continue
            if rx.search(j):
                roles[role] = j
                break
    return roles


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("glb")
    ap.add_argument("stage")
    ap.add_argument("--skeleton", default=None, help="骨架基准 json（可选，增强角色映射）")
    ap.add_argument("--allow-static", action="store_true",
                    help="无 skin 的静态模型也接入（展示可用，动画静默缺位）")
    args = ap.parse_args()
    glb = Path(args.glb).resolve()
    out_dir = MODELS_ROOT / args.stage
    skeleton = json.loads(Path(args.skeleton).read_text()) if args.skeleton else None

    joints, _, rest = glb_skin_joints(glb)
    if not joints:
        if not args.allow_static:
            sys.exit("GLB 无 skin——展示层需要蒙皮模型（或 --allow-static 静态接入）")
        print("无 skin：静态接入（roles 空，bone_bridge 优雅静默）")
    roles = build_roles(joints, skeleton)
    print(f"joints={len(joints)} roles={len(roles)}/{len(ROLE_PATTERNS)}: "
          f"{sorted(roles)}")

    with tempfile.TemporaryDirectory() as td:
        r = subprocess.run([BALSAM, "--outputPath", td, str(glb)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            sys.exit(f"balsam 失败: {r.stderr[-500:]}")
        tmp = Path(td)
        qmls = list(tmp.glob("*.qml"))
        assert qmls, "balsam 未产出 qml"

        if out_dir.exists():
            shutil.rmtree(out_dir)
        out_dir.mkdir(parents=True)
        shutil.copy(qmls[0], out_dir / "model.qml")
        for sub in ("meshes", "maps"):
            if (tmp / sub).is_dir():
                shutil.copytree(tmp / sub, out_dir / sub)
        sc = out_dir / "sidecar"
        sc.mkdir()
        if skeleton is not None:
            (sc / "skeleton3d.json").write_text(json.dumps(skeleton, ensure_ascii=False))
        profile = {
            "stage": args.stage,
            "source_glb": glb.name,
            "joints": joints,
            "roles": roles,
            "rest": {j: rest.get(j, [0, 0, 0, 0, 0, 0, 1]) for j in joints},
            "unit_hint": "meter",
        }
        (sc / "rig_profile.json").write_text(
            json.dumps(profile, ensure_ascii=False, indent=1))
    print(f"INSTALLED -> {out_dir}")


if __name__ == "__main__":
    main()
