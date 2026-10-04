#!/usr/bin/env python3
"""Blender 无头 QA 渲染：OBJ+MTL+PNG 出正/侧两张 Workbench 平光纯贴图图。

用法：
  blender -b -P three_d/tools/blender_qa.py -- <obj_path> <out_prefix>
（不传参时默认渲染 hunyuan_final.obj 到 blender_kit/qa_v9_fixed_*.png）
"""
import math
import sys

import bpy
from mathutils import Vector

KIT = "/Users/zzh4206/Desktop_Pet/three_d/assets_src/blender_kit"
argv = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
OBJ = argv[0] if len(argv) > 0 else f"{KIT}/hunyuan_final.obj"
PREFIX = argv[1] if len(argv) > 1 else f"{KIT}/qa_v9_fixed"

bpy.ops.object.select_all(action="SELECT")
bpy.ops.object.delete()
bpy.ops.wm.obj_import(filepath=OBJ)

mn = Vector((1e9, 1e9, 1e9))
mx = Vector((-1e9, -1e9, -1e9))
for ob in bpy.context.scene.objects:
    if ob.type == "MESH":
        for c in ob.bound_box:
            w = ob.matrix_world @ Vector(c)
            mn = Vector(map(min, mn, w))
            mx = Vector(map(max, mx, w))
center = (mn + mx) * 0.5
span = max(mx - mn)
d = span * 2

cam_data = bpy.data.cameras.new("cam")
cam_data.type = "ORTHO"
cam_data.ortho_scale = span * 1.12
cam = bpy.data.objects.new("cam", cam_data)
bpy.context.scene.collection.objects.link(cam)
bpy.context.scene.camera = cam

scn = bpy.context.scene
scn.render.engine = "BLENDER_WORKBENCH"
scn.display.shading.light = "FLAT"
scn.display.shading.color_type = "TEXTURE"
scn.render.resolution_x = 900
scn.render.resolution_y = 900

for name, loc, rot in (
    ("front", (center.x, center.y, center.z + d), (0, 0, 0)),
    ("side", (center.x + d, center.y, center.z), (0, math.pi / 2, 0)),
):
    cam.location = loc
    cam.rotation_euler = rot
    scn.render.filepath = f"{PREFIX}_{name}.png"
    bpy.ops.render.render(write_still=True)
    print("rendered", scn.render.filepath)
