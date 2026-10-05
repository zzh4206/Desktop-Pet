#!/usr/bin/env python3
"""多视图烘焙门禁（diag_corner_pairing 的 MV 版）。

  1. 表面级死区率：每个面质心按着色器同款权重算 w=Σ，w<0.02 的面占比
     （v9 单视图：整个背半面都是扩散；MV 应显著更低）；
  2. 分朝向颜色校验：解析烘焙产物 OBJ 的角配对，
     nz>0.35 的角 → 对 front 期望色；|nx|>0.35 → 对 side 期望色
     （side 期望色按烘焙时同款 gain/bias 修正后再比，或直接比未修正并容残差）。

用法：
  python diag_mv.py <baked.obj> <atlas.png> [front.png side.png]
"""
from __future__ import annotations

import sys

import numpy as np
from PIL import Image

dflt = "/Users/zzh4206/Desktop_Pet/assets/rig_adult_walk_v1/references"
a = sys.argv[1:]
OBJ = a[0]
ATLAS = a[1]
FRONT = a[2] if len(a) > 2 else f"{dflt}/front_rest.png"
SIDE = a[3] if len(a) > 3 else f"{dflt}/side_rest.png"


def parse_obj(path):
    v_rows, vt_rows, fv, ft = [], [], [], []
    with open(path) as fh:
        for line in fh:
            if line.startswith("v "):
                v_rows.append(np.fromstring(line[2:], sep=" "))
            elif line.startswith("vt "):
                vt_rows.append(np.fromstring(line[3:], sep=" "))
            elif line.startswith("f "):
                fv_t, ft_t = [0, 0, 0], [0, 0, 0]
                for i, tk in enumerate(line[2:].split()):
                    p = tk.split("/")
                    fv_t[i] = int(p[0]) - 1
                    ft_t[i] = int(p[1]) - 1 if len(p) > 1 and p[1] else -1
                fv.append(fv_t)
                ft.append(ft_t)
    return (np.asarray(v_rows), np.asarray(vt_rows),
            np.asarray(fv, dtype=np.int64), np.asarray(ft, dtype=np.int64))


v, vt, fv, ft = parse_obj(OBJ)

# ---- 面法线 & 质心（用焊接前的角顶点均值即可，方向足够准）----
tri = v[fv]                        # (F,3,3)
cent = tri.mean(1)
e1, e2 = tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0]
nrm = np.cross(e1, e2)
nrm /= np.maximum(np.linalg.norm(nrm, axis=1, keepdims=True), 1e-12)
# 统一法线朝外（网格封闭：法线应背离质心）
out = np.sign((nrm * (cent - cent.mean(0))).sum(1))
nrm *= out[:, None]
nz, nx = nrm[:, 2], nrm[:, 0]

front = np.asarray(Image.open(FRONT).convert("RGBA"))
side = np.asarray(Image.open(SIDE).convert("RGBA"))
import glob, json
fitf = ATLAS.replace("_atlas.png", "_fit.json")
FIT = {}
if glob.glob(fitf):
    FIT = json.load(open(fitf))
    print(f"[fit] 应用烘焙色阶配准 gain={FIT.get('gain')} bias={FIT.get('bias')}")
atlas = np.asarray(Image.open(ATLAS).convert("RGB"))
H, W = atlas.shape[:2]


def cbox(img):
    ys, xs = np.where(img[:, :, 3] > 12)
    return xs.min(), ys.min(), xs.max(), ys.max()


fb, sb = cbox(front), cbox(side)
fimg = front[:, :, :3].astype(np.float32)
simg = side[:, :, :3].astype(np.float32)
if FIT.get("gain"):
    for c in range(3):
        simg[:, :, c] = simg[:, :, c] * FIT["gain"][c] + FIT["bias"][c]
    simg = np.clip(simg, 0, 255)
fa, sa = front[:, :, 3], side[:, :, 3]
fh, fw = fa.shape
sh, sw = sa.shape

vmin, vmax = v.min(0), v.max(0)


def proj(p, box, img_a):
    h, w = img_a.shape
    x0, y0, x1, y1 = box
    tx = np.clip((p[:, 0] - vmin[0]) / (vmax[0] - vmin[0]), 0, 1)
    ty = np.clip((p[:, 1] - vmin[1]) / (vmax[1] - vmin[1]), 0, 1)
    tz = np.clip((p[:, 2] - vmin[2]) / (vmax[2] - vmin[2]), 0, 1)
    col = np.clip((x0 + (x1 - x0) * tx).astype(int), 0, w - 1)   # front u<-x
    row = np.clip((y1 + (y0 - y1) * ty).astype(int), 0, h - 1)
    scol = np.clip((x0 + (x1 - x0) * tz).astype(int), 0, w - 1)  # side u<-z
    return col, row, scol


# ---- 1. 表面级死区率（着色器同款权重）----
wf = np.clip(nz, 0, None) ** 1.5
wr = np.clip(nx, 0, None) ** 1.5
wl = np.clip(-nx, 0, None) ** 1.5
wb = np.clip(-nz, 0, None) ** 1.5 * 0.7
fcol, frow, scol = proj(cent, fb, fa)
srow = np.clip((sb[3] + (sb[1] - sb[3]) *
                np.clip((cent[:, 1] - vmin[1]) / (vmax[1] - vmin[1]), 0, 1)).astype(int), 0, sh - 1)
af = (fa[frow, fcol] > 89).astype(np.float32)   # 0.35*255
asr = (sa[srow, scol] > 89).astype(np.float32)
w = af * wf + asr * wr + asr * wl + asr * wb
dead = (w < 0.02).mean()
print(f"[死区] 表面级 w<0.02 面占比: {100*dead:.1f}%  （w 中位 {np.median(w):.2f}）")

# ---- 2. 分朝向角级颜色校验（引擎行序 uv.y=0=图底）----
rng = np.random.default_rng(42)
k = rng.choice(len(fv) * 3, size=min(40000, len(fv) * 3), replace=False)
fi, ci = k // 3, k % 3
pos = v[fv[fi, ci]]
uv = vt[ft[fi, ci]]
arow = np.clip(((1 - uv[:, 1]) * H).astype(int), 0, H - 1)
acol = np.clip((uv[:, 0] * W).astype(int), 0, W - 1)
got = atlas[arow, acol].astype(np.float32)

fnnz = nrm[fi, 2]
fnnx = nrm[fi, 0]
col, row, scol2 = proj(pos, fb, fa)
row2 = np.clip((sb[3] + (sb[1] - sb[3]) *
                np.clip((pos[:, 1] - vmin[1]) / (vmax[1] - vmin[1]), 0, 1)).astype(int), 0, sh - 1)
mF = (fnnz > 0.35) & (fa[row, col] > 200)
sc = scol2.copy()
negm = fnnx < 0
sc[negm] = (sb[0] + sb[2]) - sc[negm]   # -x 朝向吃的是镜像侧图
mS = (np.abs(fnnx) > 0.35) & (fnnz <= 0.35) & (sa[row2, sc] > 200)
if mF.sum() > 100:
    e = np.abs(got[mF] - fimg[row[mF], col[mF]]).mean(1)
    print(f"[正面向] n={mF.sum():>6} 平均色差 {e.mean():6.1f} 中位 {np.median(e):5.1f} <40 占比 {100*(e<40).mean():5.1f}%")
if mS.sum() > 100:
    e = np.abs(got[mS] - simg[row2[mS], sc[mS]]).mean(1)
    print(f"[侧面向] n={mS.sum():>6} 平均色差 {e.mean():6.1f} 中位 {np.median(e):5.1f} <40 占比 {100*(e<40).mean():5.1f}%（已套烘焙同款色阶配准）")
perm = rng.permutation(len(uv))
g2 = atlas[np.clip(((1 - uv[perm, 1]) * H).astype(int), 0, H - 1),
           np.clip((uv[perm, 0] * W).astype(int), 0, W - 1)].astype(np.float32)
e = np.abs(g2[mF] - fimg[row[mF], col[mF]]).mean(1)
print(f"[打乱对照-正面向] <40 占比 {100*(e<40).mean():5.1f}%")
