#!/usr/bin/env python3
"""Blender 无头减面：GLB 进 → decimate 到目标面数 → 白模 GLB 出。

用法：
  blender -b -P three_d/tools/decimate.py -- <in.glb> <out.glb> [target_faces]
默认 30000 面。减面后贴图/atlas 已不适用（拓扑变了）——配套用 project_bake_v9.py
在减面网格上重新展开+烘焙。
"""
import sys

import bpy
from mathutils import Vector

KIT = "/Users/zzh4206/Desktop_Pet/three_d/assets_src/blender_kit"
argv = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
IN = argv[0] if len(argv) > 0 else f"{KIT}/hunyuan_final.glb"
OUT = argv[1] if len(argv) > 1 else f"{KIT}/hunyuan_dec30k_white.glb"
TARGET = int(argv[2]) if len(argv) > 2 else 30000

bpy.ops.object.select_all(action="SELECT")
bpy.ops.object.delete()
bpy.ops.import_scene.gltf(filepath=IN)

mn, mx = Vector((1e9,) * 3), Vector((-1e9,) * 3)
total = 0
for ob in bpy.context.scene.objects:
    if ob.type != "MESH":
        continue
    total += len(ob.data.polygons)
    for c in ob.bound_box:
        w = ob.matrix_world @ Vector(c)
        mn, mx = Vector(map(min, mn, w)), Vector(map(max, mx, w))
print(f"in: faces={total} bbox_min={tuple(round(v,3) for v in mn)} bbox_max={tuple(round(v,3) for v in mx)}")

for ob in bpy.context.scene.objects:
    if ob.type != "MESH":
        continue
    mod = ob.modifiers.new("dec", "DECIMATE")
    mod.decimate_type = "COLLAPSE"
    mod.ratio = min(1.0, TARGET / max(1, len(ob.data.polygons)))
    ob.data.materials.clear()

# 应用修改器后导出（apply 在 mesh 上，GLB 导出不带修改器栈也能带出来，但显式应用更稳）
bpy.ops.object.select_all(action="SELECT")
bpy.context.view_layer.objects.active = next(o for o in bpy.context.scene.objects if o.type == "MESH")
bpy.ops.object.modifier_apply(modifier="dec")

total_out = sum(len(o.data.polygons) for o in bpy.context.scene.objects if o.type == "MESH")
bpy.ops.export_scene.gltf(filepath=OUT, export_format="GLB", export_yup=True, export_materials="NONE")
print(f"out: faces={total_out} -> {OUT}")
