#!/usr/bin/env python3
"""弹簧链 sidecar 导出（S1.6）：rig_scene.blend 的配件骨 → spring_params.json。

链分组（骨名模式）与参数分型：
  hair_* / bangs / ahoge  stiffness .30 drag .85（软发）
  skirt_* / apron_*       stiffness .42 drag .80（布料偏硬）
  tail_*                  stiffness .25 drag .88（最软长摆）
链 rest = 各骨 head 世界坐标（Blender 系→glTF y-up：(x,-z,y)）相对链根。
bones 名单随链导出，运行时按名驱动关节。

用法：
  blender -b -P rig_spring_export.py -- [rig_scene.blend] [out spring_params.json]
"""
import json
import sys
from pathlib import Path

KIT = Path("/Users/zzh4206/Desktop_Pet/three_d/assets_src/blender_kit")
argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
BLEND = argv[0] if len(argv) > 0 else str(KIT / "rig_scene.blend")
OUT = argv[1] if len(argv) > 1 else \
    "/Users/zzh4206/Desktop_Pet/three_d_assets/models/adult/sidecar/spring_params.json"

CHAIN_DEFS = [
    # (链名, 骨名序列, 参数型)
    ("tail",        ["tail_01", "tail_02", "tail_03", "tail_fluke"], "tail"),
    ("hair_back_l", ["head", "hair_back_l_01", "hair_back_l_02", "hair_back_l_03"], "hair"),
    ("hair_back_r", ["head", "hair_back_r_01", "hair_back_r_02", "hair_back_r_03"], "hair"),
    ("hair_side_l", ["head", "hair_side_l_01", "hair_side_l_02"], "hair"),
    ("hair_side_r", ["head", "hair_side_r_01", "hair_side_r_02"], "hair"),
    ("bangs",       ["head", "bangs_01", "bangs_02", "bangs_03"], "hair"),
    ("ahoge",       ["head", "ahoge_01", "ahoge_02"], "ahoge"),
    ("skirt_l",     ["skirt_root", "skirt_hem_l"], "cloth"),
    ("skirt_r",     ["skirt_root", "skirt_hem_r"], "cloth"),
    ("apron",       ["apron_root", "apron_tip"], "cloth"),
]
PARAM_STYLES = {
    "hair":  {"stiffness": 0.30, "drag": 0.85, "gravity": -9.8},
    "cloth": {"stiffness": 0.42, "drag": 0.80, "gravity": -9.8},
    "tail":  {"stiffness": 0.25, "drag": 0.88, "gravity": -9.8},
    "ahoge": {"stiffness": 0.50, "drag": 0.82, "gravity": -4.0},  # 呆毛轻翘抗垂
}

import bpy  # noqa: E402

bpy.ops.wm.open_mainfile(filepath=BLEND)
arm = next(o for o in bpy.context.scene.objects if o.type == "ARMATURE")

chains = []
for name, bones, style in CHAIN_DEFS:
    heads = []
    for bn in bones:
        pb = arm.pose.bones.get(bn)
        if pb is None:
            raise SystemExit(f"缺骨: {bn}（链 {name}）")
        w = arm.matrix_world @ pb.head
        heads.append((w.x, -w.z, w.y))          # Blender z-up → glTF y-up
    root = heads[0]
    rest = [[round(p - r, 4) for p, r in zip(h, root)] for h in heads]
    chains.append({
        "name": name,
        "bones": bones,                          # 运行时按名驱动（[0]=锚骨）
        "root_world": [round(c, 4) for c in root],   # 锚点（glTF 系，静止近似）
        "rest": rest,
        "params": PARAM_STYLES[style],
    })
    print(f"链 {name:<12} {len(bones)} 骨 rest 末节点 {rest[-1]}")

out = {"chains": chains}
Path(OUT).parent.mkdir(parents=True, exist_ok=True)
Path(OUT).write_text(json.dumps(out, ensure_ascii=False))
print(f"导出 {len(chains)} 链 -> {OUT}")
