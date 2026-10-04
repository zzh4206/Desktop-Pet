#!/usr/bin/env python3
"""迷彩一枪定位（收口路线 a）：面片角-UV 配对校验 + 独立渲染仲裁。

背景：v2–v9 渲染全迷彩、与引擎无关；atlas 目检干净、顶点级对应度通过。
怀疑面片角 (position, uv) 配对在导出/导入链路被破坏——顶点级测试测不到。

本脚本不依赖 Blender、不经 trimesh 任何合并/转换，直接对最终产物做三件事：

  A. 手工解析 OBJ（v/vt/f 原样），确认文件里的角配对结构；
  B. 数值校验：每个面片角 (pos, uv) → atlas[uv] 应≈front[pos]（v9 正面投影），
     分别按两种行序约定算误差：
       conv-PIL : atlas 行 = uv.y * H          （uv.y=0 = 图顶；脚本侧自洽约定）
       conv-ENG : atlas 行 = (1-uv.y) * H      （uv.y=0 = 图底；Blender/glTF 采样约定）
  C. moderngl 独立正交渲染（+z 正视）出 PNG 两版（uv 原样 / uv.y 翻转）：
     每角独立送 (pos, uv)——绕开一切按顶点合并的可能，看文件数据本身长什么样。

判读矩阵：
  B: 仅 conv-PIL 低 → 文件自洽但方向与引擎相反 → 迷彩=行序翻转 bug；
     仅 conv-ENG 低 → 文件配对本来就是引擎约定，迷彩另有其因（查导入/渲染链）；
     两者都高       → 文件角配对真断（导出链路坏）。
  C: 与 B 结论互相印证，并给人眼可看的证据图。

用法（TripoSR venv）：
  cd /Users/zzh4206/TripoSR && .venv/bin/python \
      /Users/zzh4206/Desktop_Pet/three_d/tools/diag_corner_pairing.py
"""
from __future__ import annotations

import sys

import numpy as np
from PIL import Image

KIT = "/Users/zzh4206/Desktop_Pet/three_d/assets_src/blender_kit"
OBJ = f"{KIT}/hunyuan_final.obj"
ATLAS = f"{KIT}/hunyuan_final_atlas.png"
FRONT = "/Users/zzh4206/Desktop_Pet/assets/rig_adult_walk_v1/references/front_rest.png"
OUTDIR = "/Users/zzh4206/Desktop_Pet/three_d/assets_src/blender_kit"


def parse_obj(path: str):
    """原样解析 v / vt / f（f 支持 a/ta、a/ta/na、a//na），不做任何合并。"""
    v_rows, vt_rows, fv_rows, ft_rows = [], [], [], []
    with open(path) as fh:
        for line in fh:
            if line.startswith("v "):
                v_rows.append(np.fromstring(line[2:], sep=" "))
            elif line.startswith("vt "):
                vt_rows.append(np.fromstring(line[3:], sep=" "))
            elif line.startswith("f "):
                toks = line[2:].split()
                if len(toks) != 3:
                    raise ValueError(f"non-triangle face: {line!r}")
                fv = [0, 0, 0]
                ft = [0, 0, 0]
                for i, tk in enumerate(toks):
                    parts = tk.split("/")
                    fv[i] = int(parts[0]) - 1
                    ft[i] = int(parts[1]) - 1 if len(parts) > 1 and parts[1] else -1
                fv_rows.append(fv)
                ft_rows.append(ft)
    return (np.asarray(v_rows, dtype=np.float64),
            np.asarray(vt_rows, dtype=np.float64),
            np.asarray(fv_rows, dtype=np.int64),
            np.asarray(ft_rows, dtype=np.int64))


def main() -> None:
    rng = np.random.default_rng(42)

    # ---- A. 解析与结构体检 ----
    v, vt, fv, ft = parse_obj(OBJ)
    n_corner_uv_missing = int((ft < 0).sum())
    # 每个 v 被多少个不同 vt 引用（正确文件：内部顶点 1:1，接缝顶点 2+）
    pair = np.unique(fv * (len(vt) + 1) + np.maximum(ft, 0))
    v_of_pair, vt_of_pair = np.divmod(pair, len(vt) + 1)
    uniq_v = np.unique(v_of_pair)
    multi = uniq_v[np.array([(vt_of_pair[v_of_pair == k]).size > 1 for k in uniq_v[:0]])] if False else None
    # 向量化版：每个 v 的不同 vt 计数
    cnt = np.bincount(v_of_pair, minlength=len(v))
    used_pairs = np.zeros(len(v), dtype=np.int64)
    np.add.at(used_pairs, v_of_pair, 1)
    seam_v = int((used_pairs > 1).sum())
    print(f"[A] v={len(v)} vt={len(vt)} f={len(fv)} 角缺uv={n_corner_uv_missing}")
    print(f"[A] 同一 v 挂多个 vt 的顶点数（接缝顶点）= {seam_v} ({100*seam_v/len(v):.1f}%)")

    # ---- B. 角级数值校验 ----
    atlas = np.asarray(Image.open(ATLAS).convert("RGB"))
    H, W = atlas.shape[:2]
    front = np.asarray(Image.open(FRONT).convert("RGB"))
    fh, fw = front.shape[:2]
    fa = np.asarray(Image.open(FRONT).convert("RGBA"))[:, :, 3]
    ys, xs = np.where(fa > 12)
    fx0, fy0, fx1, fy1 = xs.min(), ys.min(), xs.max(), ys.max()
    vmin, vmax = v.min(0), v.max(0)

    k = rng.choice(len(fv) * 3, size=min(30000, len(fv) * 3), replace=False)
    fi, ci = k // 3, k % 3
    pos = v[fv[fi, ci]]
    uv = vt[ft[fi, ci]]

    # 期望色：v9 正面投影（mesh bbox ↔ 角色 alpha bbox）
    tx = np.clip((pos[:, 0] - vmin[0]) / (vmax[0] - vmin[0]), 0, 1)
    ty = np.clip((pos[:, 1] - vmin[1]) / (vmax[1] - vmin[1]), 0, 1)
    col = np.clip((fx0 + (fx1 - fx0) * tx).astype(int), 0, fw - 1)
    row = np.clip((fy1 + (fy0 - fy1) * ty).astype(int), 0, fh - 1)
    expect = front[row, col].astype(np.float32)

    for name, r in (("conv-PIL(uv.y=0=图顶)", (uv[:, 1] * H).astype(int)),
                    ("conv-ENG(uv.y=0=图底)", ((1 - uv[:, 1]) * H).astype(int))):
        c = np.clip(r, 0, H - 1)
        cc = np.clip((uv[:, 0] * W).astype(int), 0, W - 1)
        got = atlas[c, cc].astype(np.float32)
        err = np.abs(got - expect).mean(1)
        print(f"[B] {name}: 平均色差={err.mean():6.1f}  中位={np.median(err):6.1f}  "
              f"<40 占比={100*(err<40).mean():5.1f}%")

    # 对照组：把 uv 随机换给别的角（模拟配对被打乱）
    perm = rng.permutation(len(uv))
    r = np.clip((uv[perm, 1] * H).astype(int), 0, H - 1)
    cc = np.clip((uv[perm, 0] * W).astype(int), 0, W - 1)
    got = atlas[r, cc].astype(np.float32)
    err = np.abs(got - expect).mean(1)
    print(f"[B] 打乱对照(角配对随机互换): 平均色差={err.mean():6.1f}  <40 占比={100*(err<40).mean():5.1f}%")

    # ---- C. 独立 GPU 渲染仲裁（每角独立属性，绕开一切顶点级合并） ----
    import moderngl
    ctx = moderngl.create_context(standalone=True)
    prog = ctx.program(
        vertex_shader="""#version 330
        in vec3 in_pos; in vec2 in_uv;
        out vec2 v_uv;
        uniform vec3 uMin; uniform vec3 uMax;
        void main(){
            v_uv = in_uv;
            float cx = (in_pos.x - uMin.x) / (uMax.x - uMin.x) * 2.0 - 1.0;
            float cy = (in_pos.y - uMin.y) / (uMax.y - uMin.y) * 2.0 - 1.0;
            gl_Position = vec4(cx, cy, 0.0, 1.0);
        }""",
        fragment_shader="""#version 330
        in vec2 v_uv; uniform sampler2D uTex; uniform float uFlipV;
        out vec4 o;
        void main(){
            vec2 t = vec2(v_uv.x, mix(v_uv.y, 1.0 - v_uv.y, uFlipV));
            o = vec4(texture(uTex, t).rgb, 1.0);
        }""",
    )
    # 角级展平：每面 3 角，属性 (pos, uv) 全部按角复制
    corner_pos = v[fv]          # (F,3,3)
    corner_uv = vt[ft]          # (F,3,2)
    cp = corner_pos.reshape(-1, 3).astype(np.float32)
    cu = corner_uv.reshape(-1, 2).astype(np.float32)
    tex = ctx.texture((W, H), 3, atlas.tobytes())
    tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
    tex.use(0)
    prog["uTex"].value = 0
    prog["uMin"].value = tuple(vmin.astype(np.float32))
    prog["uMax"].value = tuple(vmax.astype(np.float32))
    vbo = ctx.buffer(np.hstack([cp, cu]).tobytes())
    idx = np.arange(len(cp), dtype=np.uint32)
    ibo = ctx.buffer(idx.tobytes())
    vao = ctx.vertex_array(prog, vbo, "in_pos", "in_uv", index_buffer=ibo, index_element_size=4)
    SIZE = 1024
    fbo = ctx.simple_framebuffer((SIZE, SIZE))
    for flip, name in ((0.0, "diag_render_uvasis.png"), (1.0, "diag_render_uvflip.png")):
        fbo.use()
        fbo.clear(0.12, 0.12, 0.14, 1.0)
        prog["uFlipV"].value = flip
        vao.render(moderngl.TRIANGLES)
        raw = np.frombuffer(fbo.read(components=3), dtype=np.uint8).reshape(SIZE, SIZE, 3)
        Image.fromarray(np.flipud(raw)).save(f"{OUTDIR}/{name}")  # GL 行序→PIL
        print(f"[C] 渲染 {name}（flipV={flip}）")


if __name__ == "__main__":
    main()
