#!/usr/bin/env python3
"""Blender 无头 OBJ→GLB 转换（烘焙产物的最后一棒）。

用法：
  blender -b -P three_d/tools/obj_to_glb.py -- <obj_path> [out.glb]
不传参时默认 hunyuan_final.obj → hunyuan_final.glb（贴图内嵌）。
"""
import sys

import bpy

KIT = "/Users/zzh4206/Desktop_Pet/three_d/assets_src/blender_kit"
argv = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
OBJ = argv[0] if len(argv) > 0 else f"{KIT}/hunyuan_final.obj"
OUT = argv[1] if len(argv) > 1 else OBJ.rsplit(".", 1)[0] + ".glb"

bpy.ops.object.select_all(action="SELECT")
bpy.ops.object.delete()
bpy.ops.wm.obj_import(filepath=OBJ)

bpy.ops.export_scene.gltf(
    filepath=OUT,
    export_format="GLB",
    export_yup=True,
    export_materials="EXPORT",
)
print("GLB ->", OUT)
