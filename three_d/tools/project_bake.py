#!/usr/bin/env python3
"""立绘投影烘焙器（D17 路线 C）——正/侧原画正交投影到 3D 白模，出带贴图 GLB。

用法（TripoSR venv，含 moderngl/xatlas）：
  cd /Users/zzh4206/TripoSR && source .venv/bin/activate
  python /Users/zzh4206/Desktop_Pet/three_d/tools/project_bake.py \
      --mesh <in.glb> --front front_rest.png --side side_key.png \
      --out <out.glb> [--tex 2048] [--side-from +x|-x]

原理：xatlas 展开 → moderngl 单 pass 光栅化到 atlas（片元着色器内：世界坐标
→ 正/侧图像 UV → 采样两张原画 → 按法线朝向加权混合；源 alpha 门控）→
CPU 膨胀填补投影死角 → TextureVisuals 导出。
映射：网格 bbox ↔ 图像角色 alpha bbox（front: x↔u, y↔v；side: z↔u,v 同理；
side 相机默认 +x（尾在图像左侧的约定），法线权重 w=|n·视线|）。
"""

from __future__ import annotations

import argparse

import numpy as np


def char_bbox(img_rgba: np.ndarray, thr: int = 12) -> tuple[int, int, int, int]:
    """角色 alpha 包围盒（x0, y0, x1, y1，像素坐标 y 向下）。"""
    a = img_rgba[:, :, 3]
    ys, xs = np.where(a > thr)
    return int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())


def main() -> None:
    import moderngl
    import trimesh
    import xatlas
    from PIL import Image

    ap = argparse.ArgumentParser()
    ap.add_argument("--mesh", required=True)
    ap.add_argument("--front", required=True)
    ap.add_argument("--side", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--tex", type=int, default=2048)
    ap.add_argument("--side-from", default="+x", choices=["+x", "-x"])
    ap.add_argument("--power", type=float, default=0.7, help="法线权重幂（越大越锐利分带）")
    args = ap.parse_args()

    mesh = trimesh.load(args.mesh, force="mesh")
    verts = np.asarray(mesh.vertices, dtype=np.float32)
    faces = np.asarray(mesh.faces, dtype=np.uint32)
    norms = np.asarray(mesh.vertex_normals, dtype=np.float32)
    vmin, vmax = verts.min(0), verts.max(0)

    front_u8 = np.asarray(Image.open(args.front).convert("RGBA"))
    side_u8 = np.asarray(Image.open(args.side).convert("RGBA"))
    fx0, fy0, fx1, fy1 = char_bbox(front_u8)
    sx0, sy0, sx1, sy1 = char_bbox(side_u8)
    front = front_u8.astype(np.float32) / 255.0
    side = side_u8.astype(np.float32) / 255.0
    print(f"front char bbox: x {fx0}-{fx1}, y {fy0}-{fy1} / {front.shape[1]}x{front.shape[0]}")
    print(f"side  char bbox: x {sx0}-{sx1}, y {sy0}-{sy1}")

    # ---- xatlas UV ----
    atlas = xatlas.Atlas()
    atlas.add_mesh(verts, faces)
    xatlas.ChartOptions().max_cost * 1.0  # 默认参数
    atlas.generate()
    vmapping, indices, uvs = atlas[0]
    verts_r, norms_r = verts[vmapping], norms[vmapping]
    faces_u = indices.astype(np.uint32)
    print(f"xatlas: {len(indices)} tris -> atlas {atlas.width}x{atlas.height}")

    ctx = moderngl.create_context(standalone=True)
    prog = ctx.program(
        vertex_shader="""
            #version 330
            in vec2 in_uv; in vec3 in_pos; in vec3 in_nrm;
            out vec3 v_pos; out vec3 v_nrm;
            void main() {
                v_pos = in_pos; v_nrm = in_nrm;
                gl_Position = vec4(in_uv * 2.0 - 1.0, 0.0, 1.0);
            }
        """,
        fragment_shader="""
            #version 330
            in vec3 v_pos; in vec3 v_nrm;
            uniform sampler2D uFront; uniform sampler2D uSide;
            uniform vec4 uFBox;   // fx0, fy0(inv), fx1, fy1(inv)  图像归一化坐标
            uniform vec4 uSBox;
            uniform vec3 uMin; uniform vec3 uMax;   // 网格 bbox
            uniform float uSideSign; uniform float uPow;
            out vec4 o_col;
            vec2 fUV(vec3 p) {   // 正视：u<-x, v<-y（图像 v 向下）
                float u = mix(uFBox.x, uFBox.z, (p.x - uMin.x) / (uMax.x - uMin.x));
                float v = mix(uFBox.w, uFBox.y, (p.y - uMin.y) / (uMax.y - uMin.y));
                return vec2(u, v);
            }
            vec2 sUV(vec3 p) {   // 侧视：u<-z（+x 相机时屏幕右=+z；-x 相机翻转）
                float t = (p.z - uMin.z) / (uMax.z - uMin.z);
                float u = mix(uSBox.x, uSBox.z, uSideSign > 0. ? t : 1.0 - t);
                float v = mix(uSBox.w, uSBox.y, (p.y - uMin.y) / (uMax.y - uMin.y));
                return vec2(u, v);
            }
            void main() {
                vec3 n = normalize(v_nrm);
                float wf = pow(max(dot(n, vec3(0., 0., 1.)), 0.0), uPow);
                float ws = pow(max(dot(n, vec3(uSideSign, 0., 0.)), 0.0), uPow);
                vec4 cf = texture(uFront, fUV(v_pos));
                vec4 cs = texture(uSide,  sUV(v_pos));
                wf *= step(0.35, cf.a);   // 源 alpha 门控（原画透明区不投色）
                ws *= step(0.35, cs.a);
                float w = wf + ws;
                if (w < 0.02) { o_col = vec4(0.5, 0.5, 0.55, 0.0); return; }  // 死角：低 alpha 标记
                vec3 c = (cf.rgb * wf + cs.rgb * ws) / w;
                o_col = vec4(c, min(w, 1.0));
            }
        """,
    )

    def tex2d(arr: np.ndarray) -> moderngl.Texture:
        t = ctx.texture((arr.shape[1], arr.shape[0]), 4, arr.tobytes())
        t.filter = (moderngl.LINEAR, moderngl.LINEAR)
        t.use_location = 0
        return t

    tf = tex2d(front_u8)
    ts = tex2d(side_u8)
    tf.use(0); ts.use(1)
    prog["uFront"].value = 0; prog["uSide"].value = 1
    fw, fh = front.shape[1], front.shape[0]
    sw, sh = side.shape[1], side.shape[0]
    prog["uFBox"].value = (fx0 / fw, fy0 / fh, fx1 / fw, fy1 / fh)
    prog["uSBox"].value = (sx0 / sw, sy0 / sh, sx1 / sw, sy1 / sh)
    prog["uMin"].value = tuple(vmin); prog["uMax"].value = tuple(vmax)
    prog["uSideSign"].value = 1.0 if args.side_from == "+x" else -1.0
    prog["uPow"].value = args.power

    vbo = ctx.buffer(np.hstack([
        np.asarray(uvs, dtype=np.float32),
        verts_r, norms_r,
    ]).tobytes())
    ibo = ctx.buffer(faces_u.tobytes())
    vao = ctx.vertex_array(prog, vbo,
                           "in_uv", "in_pos", "in_nrm", index_buffer=ibo,
                           index_element_size=4)
    fbo = ctx.simple_framebuffer((args.tex, args.tex))
    fbo.use()
    fbo.clear(0.5, 0.5, 0.55, 0.0)
    vao.render(moderngl.TRIANGLES)
    px = np.frombuffer(fbo.read(components=4), dtype=np.uint8).reshape(args.tex, args.tex, 4)

    # ---- CPU 填补死角：低 alpha 区域从邻域扩散 ----
    rgb = px[:, :, :3].astype(np.float32)
    known = px[:, :, 3] > 200
    print("dead-zone texels: %.1f%%" % (100 * (~known).mean()))
    for _ in range(24):
        acc = np.zeros_like(rgb)
        cnt = np.zeros(known.shape, dtype=np.float32)
        for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            k = np.roll(known.astype(np.float32), (dy, dx), (0, 1))
            r = np.roll(rgb * known[:, :, None], (dy, dx), (0, 1))
            acc += r
            cnt += k
        upd = (~known) & (cnt > 0)
        if not upd.any():
            break
        for ch in range(3):
            rgb[:, :, ch][upd] = acc[:, :, ch][upd] / cnt[upd]
        known |= upd
    out_img = Image.fromarray(np.clip(rgb, 0, 255).astype(np.uint8), "RGB")
    out_img.save(args.out.replace(".glb", "_atlas.png"))

    textured = trimesh.Trimesh(
        vertices=verts_r, faces=faces_u,
        visual=trimesh.visual.TextureVisuals(uv=np.asarray(uvs, dtype=np.float32),
                                             image=out_img))
    textured.export(args.out)
    print("BAKED ->", args.out)


if __name__ == "__main__":
    main()
