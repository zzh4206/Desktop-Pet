#!/usr/bin/env python3
"""多视图投影烘焙 v2（着色优化：同状态正/侧图 + 角度相互拟合修正）。

与 v9（单视图）的差异：
  1. 相互拟合修正（CPU 预处理）：对"两视图都中等可见"的表面点收集
     (side_rgb, front_rgb) 颜色对，稳健线性拟合 per-channel gain/bias
     把侧图色阶配准到正图（曝光/色调差的相互修正），再参与混合；
  2. 角度权重混合：front(+z)/side(+x)/side 镜像(-x) 按法线幂权重，
     背面(-z)以侧图剪影回填（长发/裙背与侧缘同色系，避免正面镜像
     把五官带到后脑）；
  3. 描边抑制（暗线像素置透明）+ 死角扩散填充 + 存图前 V 翻转。

用法（TripoSR venv）：
  python project_bake_mv.py --mesh in.glb --front front_rest.png --side side_rest.png \
      --out out_prefix [--tex 2048] [--power 1.5]
产物：out_prefix.obj/.mtl/material_0.png + out_prefix_atlas.png
"""
from __future__ import annotations

import argparse

import numpy as np


def char_bbox(img_rgba: np.ndarray, thr: int = 12) -> tuple[int, int, int, int]:
    a = img_rgba[:, :, 3]
    ys, xs = np.where(a > thr)
    return int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())


def suppress_lines(img_u8: np.ndarray) -> np.ndarray:
    f = img_u8.astype(np.float32)
    lum = 0.299 * f[:, :, 0] + 0.587 * f[:, :, 1] + 0.114 * f[:, :, 2]
    a = f[:, :, 3] > 0
    line = a & (lum < 110)
    for _ in range(3):
        line = line | np.roll(line, 1, 0) | np.roll(line, -1, 0) | np.roll(line, 1, 1) | np.roll(line, -1, 1)
    out = img_u8.copy()
    out[line, 3] = 0
    return out


def fit_side_to_front(front_u8: np.ndarray, side_u8: np.ndarray,
                      verts: np.ndarray, norms: np.ndarray,
                      vmin: np.ndarray, vmax: np.ndarray,
                      fbox: tuple, sbox: tuple) -> tuple[np.ndarray, dict]:
    """角度相互拟合：45° 带两视图都可见的顶点 → side→front 线性色阶配准。"""
    fh, fw = front_u8.shape[:2]
    sh, sw = side_u8.shape[:2]
    fx0, fy0, fx1, fy1 = fbox
    sx0, sy0, sx1, sy1 = sbox
    tx = np.clip((verts[:, 0] - vmin[0]) / (vmax[0] - vmin[0]), 0, 1)
    ty = np.clip((verts[:, 1] - vmin[1]) / (vmax[1] - vmin[1]), 0, 1)
    tz = np.clip((verts[:, 2] - vmin[2]) / (vmax[2] - vmin[2]), 0, 1)
    fcol = np.clip((fx0 + (fx1 - fx0) * tx).astype(int), 0, fw - 1)
    frow = np.clip((fy1 + (fy0 - fy1) * ty).astype(int), 0, fh - 1)
    scol = np.clip((sx0 + (sx1 - sx0) * tz).astype(int), 0, sw - 1)
    srow = np.clip((sy1 + (sy0 - sy1) * ty).astype(int), 0, sh - 1)
    nz, nx = norms[:, 2], norms[:, 0]
    m = (nz > 0.2) & (np.abs(nx) > 0.2) & \
        (front_u8[frow, fcol, 3] > 200) & (side_u8[srow, scol, 3] > 200)
    info = {"pairs": int(m.sum())}
    if m.sum() < 200:
        info["gain"] = [1.0, 1.0, 1.0]
        info["bias"] = [0.0, 0.0, 0.0]
        info["resid"] = -1.0
        return side_u8, info
    F = front_u8[frow[m], fcol[m], :3].astype(np.float32)
    S = side_u8[srow[m], scol[m], :3].astype(np.float32)
    gains, biases = [], []
    for c in range(3):
        A = np.stack([S[:, c], np.ones_like(S[:, c])], 1)
        sol, *_ = np.linalg.lstsq(A, F[:, c], rcond=None)
        gain, bias = float(sol[0]), float(sol[1])
        r = np.abs(A @ sol - F[:, c])
        keep = r < max(3.0, 2.5 * np.median(r))  # 稳健：剔离群（接缝/遮挡错配）
        A2, F2 = A[keep], F[:, c][keep]
        sol, *_ = np.linalg.lstsq(A2, F2, rcond=None)
        gains.append(float(sol[0]))
        biases.append(float(sol[1]))
    fixed = side_u8.copy().astype(np.float32)
    for c in range(3):
        fixed[:, :, c] = fixed[:, :, c] * gains[c] + biases[c]
    resid = float(np.abs(
        np.stack([fixed[srow[m], scol[m], c] for c in range(3)], 1)
        - F).mean())
    info.update(gain=[round(g, 3) for g in gains],
                bias=[round(b, 1) for b in biases],
                resid=round(resid, 2))
    return np.clip(fixed, 0, 255).astype(np.uint8), info


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
    ap.add_argument("--power", type=float, default=8.0)
    ap.add_argument("--keep-lines", action="store_true")
    args = ap.parse_args()

    mesh = trimesh.load(args.mesh, force="mesh")
    # ⚠️ 几何连通修复：GLB 按角属性（UV）拆顶点→xatlas 面面不共边→微型图表
    # （装包利用率塌方，wiki 已录此坑）。按位置合并（忽略 UV）恢复连通后再展开。
    mesh.merge_vertices(merge_tex=True, merge_norm=True)
    verts = np.asarray(mesh.vertices, dtype=np.float32)
    faces = np.asarray(mesh.faces, dtype=np.uint32)
    norms = np.asarray(mesh.vertex_normals, dtype=np.float32)
    vmin, vmax = verts.min(0), verts.max(0)

    front_u8 = np.asarray(Image.open(args.front).convert("RGBA"))
    side_u8 = np.asarray(Image.open(args.side).convert("RGBA"))
    fbox = char_bbox(front_u8)
    sbox = char_bbox(side_u8)
    print(f"front bbox {fbox} / side bbox {sbox}")

    # 相互拟合：拟合一律在描边抑制后的图上算（黑线污染拟合），但修正量
    # （gain/bias）作用于实际参与烘焙的图——线条保留与否与色阶配准解耦。
    fs, ss = suppress_lines(front_u8), suppress_lines(side_u8)
    _, fit_info = fit_side_to_front(fs, ss, verts, norms, vmin, vmax, fbox, sbox)
    degrade = fit_info.get("resid", -1) > 25 or any(
        g < 0.6 or g > 1.6 for g in fit_info.get("gain", [1, 1, 1]))
    if degrade:
        print(f"color-fit 劣化，回退恒等: {fit_info}")
        fit_info = {**fit_info, "fallback": True}
    else:
        print(f"color-fit: {fit_info}")
    if not args.keep_lines:
        front_u8, side_u8 = fs, ss
    if not degrade:
        f32 = side_u8.astype(np.float32)
        for c in range(3):
            f32[:, :, c] = f32[:, :, c] * fit_info["gain"][c] + fit_info["bias"][c]
        side_u8 = np.clip(f32, 0, 255).astype(np.uint8)

    # ---- xatlas ----
    atlas = xatlas.Atlas()
    atlas.add_mesh(verts, faces)
    atlas.generate()
    vm, idx, uvs = atlas[0]
    vr, nr = verts[vm], norms[vm]
    faces_u = idx.astype(np.uint32)
    uvs = np.asarray(uvs, np.float32)
    print(f"xatlas: {len(faces_u)} tris, uv range u[{uvs[:,0].min():.3f},{uvs[:,0].max():.3f}] "
          f"v[{uvs[:,1].min():.3f},{uvs[:,1].max():.3f}]")

    # ---- GPU 混合 ----
    ctx = moderngl.create_context(standalone=True)
    prog = ctx.program(
        vertex_shader="""#version 330
        in vec2 in_uv; in vec3 in_pos; in vec3 in_nrm;
        out vec3 v_pos; out vec3 v_nrm;
        void main(){ v_pos=in_pos; v_nrm=in_nrm;
            gl_Position=vec4(in_uv*2.0-1.0,0.0,1.0);}""",
        fragment_shader="""#version 330
        in vec3 v_pos; in vec3 v_nrm;
        uniform sampler2D uFront; uniform sampler2D uSide;
        uniform vec4 uFBox; uniform vec4 uSBox;
        uniform vec3 uMin; uniform vec3 uMax; uniform float uPow;
        out vec4 o;
        vec2 fUV(vec3 p){
            return vec2(mix(uFBox.x,uFBox.z,(p.x-uMin.x)/(uMax.x-uMin.x)),
                        mix(uFBox.w,uFBox.y,(p.y-uMin.y)/(uMax.y-uMin.y)));}
        vec2 sUV(vec3 p){
            return vec2(mix(uSBox.x,uSBox.z,(p.z-uMin.z)/(uMax.z-uMin.z)),
                        mix(uSBox.w,uSBox.y,(p.y-uMin.y)/(uMax.y-uMin.y)));}
        void main(){
            vec3 n = normalize(v_nrm);
            float wf = pow(max( n.z,0.0), uPow);
            float wr = pow(max( n.x,0.0), uPow);
            float wl = pow(max(-n.x,0.0), uPow);
            float wb = pow(max(-n.z,0.0), uPow) * 0.7;  // 背面吃侧图剪影
            vec2 su = sUV(v_pos);
            vec4 cf = texture(uFront, fUV(v_pos));
            vec4 cr = texture(uSide,  su);
            vec4 cl = texture(uSide,  vec2(1.0-su.x, su.y));
            vec4 cb = texture(uSide,  su);
            cf.a*=step(0.35,cf.a); cr.a*=step(0.35,cr.a);
            cl.a*=step(0.35,cl.a); cb.a*=step(0.35,cb.a);
            float w = cf.a*wf + cr.a*wr + cl.a*wl + cb.a*wb;
            if (w < 0.02){ o = vec4(0.5,0.5,0.55,0.0); return; }
            vec3 c = (cf.rgb*cf.a*wf + cr.rgb*cr.a*wr +
                      cl.rgb*cl.a*wl + cb.rgb*cb.a*wb) / max(w,1e-4);
            // alpha=覆盖指示（被有效视图认领即 1），与角度混合权重解耦——
            // 否则高幂下正面 w≈0.28 被当空心，98% atlas 会被扩散糊掉
            o = vec4(c, 1.0);}""",
    )

    def tex2d(arr):
        t = ctx.texture((arr.shape[1], arr.shape[0]), 4, arr.tobytes())
        t.filter = (moderngl.LINEAR, moderngl.LINEAR)
        return t

    tf, ts = tex2d(front_u8), tex2d(side_u8)
    tf.use(0); ts.use(1)
    prog["uFront"].value = 0; prog["uSide"].value = 1
    fh, fw = front_u8.shape[:2]
    sh, sw = side_u8.shape[:2]
    fx0, fy0, fx1, fy1 = fbox
    sx0, sy0, sx1, sy1 = sbox
    prog["uFBox"].value = (fx0 / fw, fy0 / fh, fx1 / fw, fy1 / fh)
    prog["uSBox"].value = (sx0 / sw, sy0 / sh, sx1 / sw, sy1 / sh)
    prog["uMin"].value = tuple(vmin); prog["uMax"].value = tuple(vmax)
    prog["uPow"].value = args.power

    vbo = ctx.buffer(np.hstack([np.asarray(uvs, np.float32), vr, nr]).tobytes())
    ibo = ctx.buffer(faces_u.tobytes())
    vao = ctx.vertex_array(prog, vbo, "in_uv", "in_pos", "in_nrm",
                           index_buffer=ibo, index_element_size=4)
    SS = 2
    fbo = ctx.simple_framebuffer((args.tex * SS, args.tex * SS))
    fbo.use()
    fbo.clear(0.5, 0.5, 0.55, 0.0)
    vao.render(moderngl.TRIANGLES)
    raw = np.frombuffer(fbo.read(components=4), dtype=np.uint8).reshape(
        args.tex * SS, args.tex * SS, 4)
    print(f"raw(SS) coverage a>200: {100*(raw[:,:,3]>200).mean():.1f}%  "
          f"a>0: {100*(raw[:,:,3]>0).mean():.1f}%")
    px = np.asarray(Image.fromarray(raw, "RGBA").resize(
        (args.tex, args.tex), Image.BOX))

    # ---- 死角扩散（含 chart gutter）----
    rgb = px[:, :, :3].astype(np.float32)
    known = px[:, :, 3] > 200
    dead_pct = 100 * (~known).mean()
    for _ in range(48):
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

    # ⚠️ V 翻转（fbo.read GL 行序 vs 引擎 uv.y=0=图底）——迷彩教训
    rgb = rgb[::-1]
    out_img = Image.fromarray(np.clip(rgb, 0, 255).astype(np.uint8))
    base = args.out[:-4] if args.out.endswith(".obj") else args.out
    out_img.save(base + "_atlas.png")
    print(f"direct coverage: {100-dead_pct:.1f}%  (dead {dead_pct:.1f}% 扩散填)")

    textured = trimesh.Trimesh(
        vertices=vr, faces=faces_u,
        visual=trimesh.visual.TextureVisuals(
            uv=np.asarray(uvs, dtype=np.float32), image=out_img))
    textured.export(base + ".obj")
    import json as _json
    _json.dump(fit_info, open(base + "_fit.json", "w"))
    print("MV BAKED ->", base + ".obj")


if __name__ == "__main__":
    main()
