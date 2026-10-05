#!/usr/bin/env python3
"""S1.4 绑骨（无头）v2：权重走"水密母网格骨热 → Data Transfer 转贴图网格"。

背景：贴图 30k 网格来自"拆分网格上 decimate"，接缝 T 型错位焊不干净，
骨热（要连续流形）在其上全零权重静默失败。母网格 hunyuan_final（33 万面）
trimesh 实测 watertight=True，骨热可正常解。

流程：
  1. starter_scene 只留 47 骨骨架；
  2. 导入贴图 30k（TARGET）对位（平移 only：脚底 z=0 + XY 中心对齐骨架）；
  3. 导入母网格 33 万（SOURCE）同法对位 → 焊接 → 骨热自动权重；
  4. Data Transfer（VGROUP_WEIGHTS，面投影插值）SOURCE → TARGET；
  5. 零权重兜底挂最近骨；删 SOURCE；
  6. 变形 QA 渲染 ×6 姿势；存 rig_scene.blend；导出蒙皮 GLB。

用法：
  blender -b -P three_d/tools/rig_bind.py -- [target.glb] [out.blend] [out.glb] [qa_prefix]
"""
import sys

import bpy
import numpy as np
from mathutils import Vector

KIT = "/Users/zzh4206/Desktop_Pet/three_d/assets_src/blender_kit"
argv = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
TARGET_GLB = argv[0] if len(argv) > 0 else "/tmp/rigbake/hunyuan_rig30k.glb"
SOURCE_GLB = argv[1] if len(argv) > 1 else f"{KIT}/hunyuan_final.glb"
OUT_BLEND = argv[2] if len(argv) > 2 else f"{KIT}/rig_scene.blend"
OUT_GLB = argv[3] if len(argv) > 3 else f"{KIT}/hunyuan_rig30k_skinned.glb"
QA = argv[4] if len(argv) > 4 else "/tmp/rigbake/qa_pose"


def bbox(objects):
    mn, mx = Vector((1e9,) * 3), Vector((-1e9,) * 3)
    for ob in objects:
        for c in ob.bound_box:
            w = ob.matrix_world @ Vector(c)
            mn, mx = Vector(map(min, mn, w)), Vector(map(max, mx, w))
    return mn, mx


def align_to_skeleton(mesh, a_mn, a_mx, scale_to_fit: bool = True):
    """平移对齐 + 可选均匀缩放。第三方模型身高与骨架基准常差 20%+
    （如 1.15m vs 1.36m），不缩放则关节全悬空、骨热错层。"""
    if scale_to_fit:
        m_mn, m_mx = bbox([mesh])
        s = (a_mx.z - a_mn.z) / max(1e-9, m_mx.z - m_mn.z)
        mesh.scale = (s, s, s)
        bpy.context.view_layer.update()
    m_mn, m_mx = bbox([mesh])
    off = ((a_mn + a_mx) - (m_mn + m_mx)) * 0.5
    mesh.location += Vector((off.x, off.y, -m_mn.z))
    bpy.context.view_layer.update()
    print(f"[rig] {mesh.name} aligned scale={mesh.scale.x:.3f} "
          f"off=({off.x:+.3f},{off.y:+.3f},{-m_mn.z:+.3f})")


def weld(mesh, thr):
    bpy.context.view_layer.objects.active = mesh
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.select_all(action="SELECT")
    bpy.ops.mesh.remove_doubles(threshold=thr)
    bpy.ops.object.mode_set(mode="OBJECT")
    # 欧拉示性：闭合流形 V-E+F≈2
    me = mesh.data
    chi = len(me.vertices) - len(me.edges) + len(me.polygons)
    print(f"[rig] {mesh.name} welded@{thr}: V={len(me.vertices)} E={len(me.edges)} F={len(me.polygons)} 欧拉χ={chi}")


# ---- 1. 骨架 ----
bpy.ops.wm.open_mainfile(filepath=f"{KIT}/starter_scene.blend")
arm = None
for ob in list(bpy.data.objects):
    if ob.type == "ARMATURE":
        arm = ob
    else:
        bpy.data.objects.remove(ob, do_unlink=True)
assert arm is not None, "starter_scene.blend 里没有骨架"
print(f"[rig] skeleton: {arm.name} bones={len(arm.data.bones)}")
a_mn, a_mx = bbox([arm])

# ---- 2. TARGET：贴图 30k ----
bpy.ops.import_scene.gltf(filepath=TARGET_GLB)
target = next(o for o in bpy.context.scene.objects if o.type == "MESH")
target.name = "pet_body"
align_to_skeleton(target, a_mn, a_mx)
weld(target, 1e-5)

# ---- 3. SOURCE：母网格 33 万，焊接→骨热权重 ----
bpy.ops.import_scene.gltf(filepath=SOURCE_GLB)
source = next(o for o in bpy.context.scene.objects if o.type == "MESH" and o != target)
source.name = "weight_source"
align_to_skeleton(source, a_mn, a_mx)
weld(source, 1e-4)

source.select_set(True)
arm.select_set(True)
bpy.context.view_layer.objects.active = arm
bpy.ops.object.parent_set(type="ARMATURE_AUTO")


def zero_weight_count(mesh):
    wsum = np.zeros(len(mesh.data.vertices))
    for v in mesh.data.vertices:
        wsum[v.index] = sum(g.weight for g in v.groups)
    return np.where(wsum < 1e-4)[0]


zsrc = zero_weight_count(source)
print(f"[rig] source zero-weight: {len(zsrc)}/{len(source.data.vertices)}")
assert len(zsrc) < 0.02 * len(source.data.vertices), "母网格骨热也失败——查焊接阈值"

# ---- 4. Data Transfer：权重 SOURCE → TARGET ----
# 坑（Blender 5.2.2 实测）：DATA_TRANSFER 修改器只写入目标上**已存在的同名组**，
# 不预建则静默零产出（两平面最小复现坐实）；先克隆源组名。
for g in source.vertex_groups:
    if g.name not in target.vertex_groups:
        target.vertex_groups.new(name=g.name)
print(f"[rig] pre-created {len(target.vertex_groups)} groups on target")

dt = target.modifiers.new("wtrans", "DATA_TRANSFER")
dt.object = source
dt.use_vert_data = True
dt.data_types_verts = {"VGROUP_WEIGHTS"}
dt.vert_mapping = "POLYINTERP_NEAREST"
dt.mix_mode = "REPLACE"
bpy.context.view_layer.objects.active = target
target.select_set(True)
bpy.ops.object.modifier_apply(modifier="wtrans")

ztgt = zero_weight_count(target)
print(f"[rig] target zero-weight after transfer: {len(ztgt)}/{len(target.data.vertices)}")

# ---- 5. 零权重兜底：挂最近骨 ----
if len(ztgt):
    groups = {g.name for g in target.vertex_groups}
    segs = []
    for pb in arm.pose.bones:
        if pb.name in groups:
            segs.append((pb.name, arm.matrix_world @ pb.head.copy(), arm.matrix_world @ pb.tail.copy()))

    def nearest(px):
        best, bd = None, 1e18
        for name, h, t in segs:
            u = max(0.0, min(1.0, (px - h).dot(t - h) / max(1e-12, (t - h).length_squared)))
            dd = (h + u * (t - h) - px).length
            if dd < bd:
                best, bd = name, dd
        return best

    mw = target.matrix_world
    fixed = {}
    for vi in ztgt:
        name = nearest(mw @ target.data.vertices[int(vi)].co)
        if name:
            target.vertex_groups[name].add([int(vi)], 1.0, "REPLACE")
            fixed[name] = fixed.get(name, 0) + 1
    print(f"[rig] zero-fix: {sum(fixed.values())} verts / {len(fixed)} bones")

bpy.data.objects.remove(source, do_unlink=True)

# ---- 5.5 绑定：TARGET 挂骨架（只建 Armature 修改器，不碰已转好的权重）----
target.select_set(True)
arm.select_set(True)
bpy.context.view_layer.objects.active = arm
bpy.ops.object.parent_set(type="ARMATURE")
print(f"[rig] target bound to armature, armature mods: "
      f"{[m.type for m in target.modifiers]}")

# ---- 6. 变形 QA ----
scn = bpy.context.scene
cam_data = bpy.data.cameras.new("qa")
cam_data.type = "ORTHO"
cam = bpy.data.objects.new("qa", cam_data)
scn.collection.objects.link(cam)
scn.camera = cam
scn.render.engine = "BLENDER_WORKBENCH"
scn.display.shading.light = "FLAT"
scn.display.shading.color_type = "TEXTURE"
scn.render.resolution_x = 640
scn.render.resolution_y = 800
ctr = (a_mn + a_mx) * 0.5
span = max(a_mx - a_mn)
cam_data.ortho_scale = span * 1.15
d = Vector((1, -1, 0.28)).normalized()
cam.location = ctr + d * span * 3
cam.rotation_euler = (ctr - cam.location).normalized().to_track_quat("-Z", "Y").to_euler()

POSES = [
    ("neutral", {}),
    ("head_turn", {"head": (0, 0, 0.45)}),
    ("shoulders", {"upper_arm_l": (0, 0, -0.5), "upper_arm_r": (0, 0, 0.5)}),
    ("elbows", {"forearm_l": (0, 0, -0.6), "forearm_r": (0, 0, 0.6)}),
    ("spine", {"spine": (0.3, 0, 0)}),
    ("hips", {"upper_leg_l": (-0.4, 0, 0), "upper_leg_r": (0.3, 0, 0)}),
]
for name, rots in POSES:
    for pb in arm.pose.bones:
        pb.rotation_mode = "XYZ"  # 姿势骨默认四元数——直接写 rotation_euler 会被静默忽略
        pb.rotation_euler = (0, 0, 0)
    for bn, rot in rots.items():
        if bn in arm.pose.bones:
            arm.pose.bones[bn].rotation_euler = rot
        else:
            print(f"[rig] WARN bone not found: {bn}")
    arm.data.pose_position = "POSE"
    scn.render.filepath = f"{QA}_{name}.png"
    bpy.ops.render.render(write_still=True)
    print(f"[qa] {name}")
for pb in arm.pose.bones:
    pb.rotation_euler = (0, 0, 0)

# ---- 7. 保存 + 导出 ----
bpy.ops.wm.save_as_mainfile(filepath=OUT_BLEND)
bpy.ops.export_scene.gltf(filepath=OUT_GLB, export_format="GLB", export_yup=True,
                          export_materials="EXPORT")
print(f"[out] {OUT_BLEND}")
print(f"[out] {OUT_GLB}")
