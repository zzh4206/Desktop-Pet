#!/usr/bin/env python3
"""v9 最终版投影烘焙——基于已验证正确的极简管线（单视图+OBJ导出）。
正面全投影 + 背面扩散填充。用法：python3 v9 <mesh.glb> <out.obj>
"""
import sys
import numpy as np, trimesh, xatlas, moderngl
from PIL import Image

MESH = sys.argv[1] if len(sys.argv) > 1 else "/Users/zzh4206/Desktop_Pet/three_d/assets_src/blender_kit/hunyuan_final.glb"  # 原默认 hunyuan_retopo.glb 已剪枝；final 同几何（330,308 面），xatlas 会重新展开
FRONT = "/Users/zzh4206/Desktop_Pet/assets/rig_adult_walk_v1/references/front_rest.png"
OUT = sys.argv[2] if len(sys.argv) > 2 else "/Users/zzh4206/Desktop_Pet/three_d/assets_src/blender_kit/hunyuan_final.obj"

m = trimesh.load(MESH, force="mesh")
verts = np.asarray(m.vertices, dtype=np.float32)
faces = np.asarray(m.faces, dtype=np.uint32)
vmin, vmax = verts.min(0), verts.max(0)
front_u8 = np.asarray(Image.open(FRONT).convert("RGBA"))
fh, fw = front_u8.shape[:2]
ys, xs = np.where(front_u8[:, :, 3] > 12)
fx0, fy0, fx1, fy1 = xs.min(), ys.min(), xs.max(), ys.max()

atlas = xatlas.Atlas(); atlas.add_mesh(verts, faces); atlas.generate()
vm, idx, uvs = atlas[0]
vr = verts[vm]

ctx = moderngl.create_context(standalone=True)
prog = ctx.program(
    vertex_shader="""#version 330
in vec2 in_uv; in vec3 in_pos;
out vec3 v_pos;
void main(){ v_pos=in_pos; gl_Position=vec4(in_uv*2.0-1.0,0.0,1.0);}""",
    fragment_shader="""#version 330
in vec3 v_pos; uniform sampler2D uF;
uniform vec4 uB; uniform vec3 uMin; uniform vec3 uMax;
out vec4 o;
void main(){
    vec2 uv = vec2(mix(uB.x,uB.z,(v_pos.x-uMin.x)/(uMax.x-uMin.x)),
                   mix(uB.w,uB.y,(v_pos.y-uMin.y)/(uMax.y-uMin.y)));
    o = vec4(texture(uF, uv).rgb, 1.0);}""")
t = ctx.texture((fw, fh), 4, front_u8.tobytes()); t.use(0)
prog["uF"].value = 0
prog["uB"].value = (fx0/fw, fy0/fh, fx1/fw, fy1/fh)
prog["uMin"].value = tuple(vmin); prog["uMax"].value = tuple(vmax)
vbo = ctx.buffer(np.hstack([np.asarray(uvs, np.float32), vr]).tobytes())
ibo = ctx.buffer(idx.astype(np.uint32).tobytes())
vao = ctx.vertex_array(prog, vbo, "in_uv", "in_pos", index_buffer=ibo, index_element_size=4)
fbo = ctx.simple_framebuffer((2048, 2048)); fbo.use(); fbo.clear(0, 0, 0, 0)
vao.render(moderngl.TRIANGLES)
AT = np.frombuffer(fbo.read(components=4), dtype=np.uint8).reshape(2048, 2048, 4)[:, :, :3]

# 扩散填死角
rgb = AT.astype(np.float32)
known = (AT != 0).any(2)
for _ in range(40):
    acc = np.zeros_like(rgb); cnt = np.zeros(known.shape, np.float32)
    for dy, dx in ((1,0),(-1,0),(0,1),(0,-1)):
        acc += np.roll(rgb * known[:,:,None], (dy,dx), (0,1))
        cnt += np.roll(known.astype(np.float32), (dy,dx), (0,1))
    upd = (~known) & (cnt > 0)
    if not upd.any(): break
    for c in range(3):
        rgb[:,:,c][upd] = acc[:,:,c][upd] / cnt[upd]
    known |= upd
# ⚠️ 行序约定：fbo.read() 返回 GL 行序（行0=GL y0=uv.y=0），PIL 存图把行0
# 当图顶——而 OBJ vt / Blender / glTF 采样时 uv.y=0=图底。不翻转=整图 V 颠倒
# =迷彩（九轮排查真因，diag_corner_pairing.py 一枪定位）。
rgb = rgb[::-1]
out_img = Image.fromarray(np.clip(rgb, 0, 255).astype(np.uint8))

tm = trimesh.Trimesh(vertices=vr, faces=idx.astype(np.uint32),
                     visual=trimesh.visual.TextureVisuals(
                         uv=np.asarray(uvs, np.float32), image=out_img))
tm.export(OUT)
out_img.save(OUT.replace(".obj", "_atlas.png"))
print("V9 BAKED ->", OUT)
