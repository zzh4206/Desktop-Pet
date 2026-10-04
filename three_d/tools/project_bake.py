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
    ap.add_argument("--single-front", action="store_true", help="纯正面单视图（零接缝实验）")
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

    # ---- v4：描边抑制（原画暗线像素置透明，采样源剔除）----
    def suppress_lines(img_u8):
        f = img_u8.astype(np.float32)
        lum = 0.299*f[:,:,0] + 0.587*f[:,:,1] + 0.114*f[:,:,2]
        a = f[:,:,3] > 0
        line = a & (lum < 110)
        for _ in range(3):
            line = line | np.roll(line,1,0) | np.roll(line,-1,0) | np.roll(line,1,1) | np.roll(line,-1,1)
        out = img_u8.copy()
        out[line, 3] = 0
        return out
    front_u8s = suppress_lines(front_u8)
    side_u8s = suppress_lines(side_u8)
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
            uniform float uUseSide;
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
                // v7b 三向软覆盖：正面图(+z)、侧图(+x 右)、侧图镜像(-x 左，
                // 角色对称)；背面留死角→扩散→LoRA。软混合必须跑在焊接+平滑
                // 网格上（v1 迷彩真因是碎片噪声网格，非软混合本身）。
                float wf = smoothstep(0.0, 0.5, n.z);
                float wr = smoothstep(0.0, 0.5,  n.x) * (1.0 - wf);
                float wl = smoothstep(0.0, 0.5, -n.x) * (1.0 - wf);
                vec2 su = sUV(v_pos);
                vec4 cf = texture(uFront, fUV(v_pos));
                vec4 cr = texture(uSide,  su);
                vec4 cl = texture(uSide,  vec2(1.0 - su.x, su.y));
                cf.a *= step(0.35, cf.a); cr.a *= step(0.35, cr.a); cl.a *= step(0.35, cl.a);
                float w = cf.a*wf + cr.a*wr + cl.a*wl;
                if (w < 0.02) { o_col = vec4(0.5, 0.5, 0.55, 0.0); return; }
                vec3 c = (cf.rgb*cf.a*wf + cr.rgb*cr.a*wr + cl.rgb*cl.a*wl) / max(w, 1e-4);
                o_col = vec4(c, min(w, 1.0));
            }
        """,
    )

    def tex2d(arr: np.ndarray) -> moderngl.Texture:
        t = ctx.texture((arr.shape[1], arr.shape[0]), 4, arr.tobytes())
        t.filter = (moderngl.LINEAR, moderngl.LINEAR)
        t.use_location = 0
        return t

    tf = tex2d(front_u8s)
    ts = tex2d(side_u8s)
    tf.use(0); ts.use(1)
    prog["uFront"].value = 0; prog["uSide"].value = 1
    fw, fh = front.shape[1], front.shape[0]
    sw, sh = side.shape[1], side.shape[0]
    prog["uFBox"].value = (fx0 / fw, fy0 / fh, fx1 / fw, fy1 / fh)
    prog["uSBox"].value = (sx0 / sw, sy0 / sh, sx1 / sw, sy1 / sh)
    prog["uMin"].value = tuple(vmin); prog["uMax"].value = tuple(vmax)
    prog["uSideSign"].value = 1.0 if args.side_from == "+x" else -1.0

    vbo = ctx.buffer(np.hstack([
        np.asarray(uvs, dtype=np.float32),
        verts_r, norms_r,
    ]).tobytes())
    ibo = ctx.buffer(faces_u.tobytes())
    vao = ctx.vertex_array(prog, vbo,
                           "in_uv", "in_pos", "in_nrm", index_buffer=ibo,
                           index_element_size=4)
    SS = 2
    fbo = ctx.simple_framebuffer((args.tex * SS, args.tex * SS))
    fbo.use()
    fbo.clear(0.5, 0.5, 0.55, 0.0)
    vao.render(moderngl.TRIANGLES)
    raw = np.frombuffer(fbo.read(components=4), dtype=np.uint8).reshape(args.tex * SS, args.tex * SS, 4)
    from PIL import Image as _Image
    px = np.asarray(_Image.fromarray(raw, "RGBA").resize((args.tex, args.tex), _Image.BOX))

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
    # ⚠️ 行序约定：fbo.read() 返回 GL 行序（行0=uv.y=0），PIL 存图把行0 当图顶
    # ——而 OBJ vt / Blender / glTF 采样 uv.y=0=图底。不翻转=整图 V 颠倒=迷彩
    # （九轮排查真因；校验工具 diag_corner_pairing.py）。
    rgb = rgb[::-1]
    out_img = Image.fromarray(np.clip(rgb, 0, 255).astype(np.uint8), "RGB")
    base = args.out[:-4] if args.out.endswith(".glb") else args.out
    out_img.save(base + "_atlas.png")

    textured = trimesh.Trimesh(
        vertices=verts_r, faces=faces_u,
        visual=trimesh.visual.TextureVisuals(uv=np.asarray(uvs, dtype=np.float32),
                                             image=out_img))
    # ⚠️ 不用 trimesh 导 GLB：其导出器拆顶点时 UV-顶点对应会错位（实测对应度
    # 24.9→92.7，产生面片级马赛克）。导 OBJ+MTL+PNG（per-corner UV 可靠），
    # Blender 再转 GLB 交给运行时。
    textured.export(base + ".obj")
    print("BAKED ->", base + ".obj (+mtl+png)")


if __name__ == "__main__":
    main()
