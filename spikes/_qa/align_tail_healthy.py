"""一次性装配:healthy 尾生成件重摆入画布 + manifest 更新 + QA。

流程(对应 v0.13.1 产线):
  D  = 原α>0 且 补丁核α=0   —— 尾露出裙摆进入虚空的"删除足迹"
  目标鳍区 = D 中 stem 收窄点以上部分 → 质心/面积
  生成件最大连通域同理取鳍区 → 缩放比 s=sqrt(面积比)
  整尾按 s 缩放、鳍质心对齐贴回画布 → 裁 bbox → parts/tail_healthy_neutral.png
  pivot = D 最底行中心(入裙点)微下移
QA: 核(底)+件(上)静置合成 vs 原图 α IoU
"""

from __future__ import annotations

import json
import math
import os
import sys

from PIL import Image

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
O = Image.open(os.path.join(REPO, 'assets/ai/final_healthy_neutral.png')).convert('RGBA')
C = Image.open(os.path.join(REPO, 'assets/rig/final/figs/healthy_neutral.png')).convert('RGBA')
G = Image.open(os.path.join(REPO, 'spikes/_qa/_tail_extract_raw.png')).convert('RGBA')
w, h = O.size
op, cp, gp = O.load(), C.load(), G.load()

# ---- D 足迹 ----
D = [[1 if op[x, y][3] > 0 and cp[x, y][3] == 0 else 0 for x in range(w)] for y in range(h)]
rows = [(y, sum(D[y])) for y in range(h)]
vis_rows = [y for y, c in rows if c > 0]
y_top, y_bot = vis_rows[0], vis_rows[-1]
peak_w = max(c for _, c in rows)
# stem 判据:先出现 ≥60% 峰宽的"宽行段",其后的首个"长薄行段"起点
# (薄=宽<25% 峰宽;长=连续 ≥60 行)——鳍尖天然细长,不满足"先宽后薄"
wide_seen = False
stem_y = None
run = 0
for y in range(y_top, y_bot + 1):
    c = rows[y][1]
    if c >= peak_w * 0.6:
        wide_seen = True
        run = 0
        continue
    if wide_seen and 0 < c < peak_w * 0.25:
        run += 1
        if run == 1:
            cand = y
        if run >= 60:
            stem_y = cand
            break
    else:
        run = 0
print(f'D y∈[{y_top},{y_bot}] 峰值行宽={peak_w} stem_y={stem_y}')

cut = stem_y if stem_y is not None else y_bot
t_pix = [(x, y) for y in range(h) for x in range(w) if D[y][x] and y < cut]
tcx = sum(p[0] for p in t_pix) / len(t_pix)
tcy = sum(p[1] for p in t_pix) / len(t_pix)
print(f'目标鳍 质心=({tcx:.1f},{tcy:.1f}) 面积={len(t_pix)}')

# ---- 生成件最大域 ----
seen = [[False] * w for _ in range(h)]
best = []
for y0 in range(h):
    for x0 in range(w):
        if gp[x0, y0][3] > 64 and not seen[y0][x0]:
            stack = [(x0, y0)]
            seen[y0][x0] = True
            comp = []
            while stack:
                x, y = stack.pop(); comp.append((x, y))
                for nx, ny in ((x+1, y), (x-1, y), (x, y+1), (x, y-1)):
                    if 0 <= nx < w and 0 <= ny < h and gp[nx, ny][3] > 64 and not seen[ny][nx]:
                        seen[ny][nx] = True; stack.append((nx, ny))
            if len(comp) > len(best):
                best = comp
rowsg = {}
for (x, y) in best:
    rowsg[y] = rowsg.get(y, 0) + 1
ysg = sorted(rowsg)
stemg = next((y for y in ysg[len(ysg) // 4:] if rowsg[y] < 40), ysg[-1])
g_pix = [(x, y) for (x, y) in best if y < stemg]
gcx = sum(p[0] for p in g_pix) / len(g_pix)
gcy = sum(p[1] for p in g_pix) / len(g_pix)
print(f'生成鳍 质心=({gcx:.1f},{gcy:.1f}) 面积={len(g_pix)}')

s = math.sqrt(len(t_pix) / len(g_pix))
if not (0.25 <= s <= 2.5) or len(t_pix) < 5000:
    raise SystemExit(f'缩放比/目标面积异常 s={s:.4f} t_area={len(t_pix)} —— 掩码判定失效,中止')
print(f'缩放比 s={s:.4f}')

# ---- 重摆 ----
gw, gh = G.size
nw, nh = max(1, int(gw * s)), max(1, int(gh * s))
Gs = G.resize((nw, nh), Image.LANCZOS)
sp = Gs.load()
g2x, g2y = gcx * s, gcy * s
canvas = Image.new('RGBA', (w, h), (0, 0, 0, 0))
cpn = canvas.load()
ox = int(round(tcx - g2x)); oy = int(round(tcy - g2y))
n_pasted = 0
for y in range(nh):
    ty = oy + y
    if not (0 <= ty < h):
        continue
    for x in range(nw):
        tx = ox + x
        if 0 <= tx < w:
            px = sp[x, y]
            if px[3] > 0:
                cpn[tx, ty] = px
                n_pasted += 1
print(f'贴回像素={n_pasted} offset=({ox},{oy}) 缩后尺寸={nw}x{nh}')
bb = canvas.getbbox()
print(f'件 bbox={bb}')
part = canvas.crop(bb)
part_path = os.path.join(REPO, 'assets/rig/final/parts/tail_healthy_neutral.png')
part.save(part_path)

# ---- pivot: D 最底可见行中心 ----
last = max(y for y, c in rows if c > 0)
xs = [x for x in range(w) if D[last][x]]
px_join = sum(xs) / len(xs)
pivot = [round(px_join), min(last + 8, h - 1)]
print('pivot=', pivot)

# ---- manifest 更新 ----
mpath = os.path.join(REPO, 'assets/rig/final/manifest.json')
m = json.load(open(mpath, encoding='utf-8'))
m['figures']['healthy_neutral'] = 'figs/healthy_neutral.png'
m['parts'] = [p for p in m['parts'] if p.get('id') != 'tail_healthy_neutral']
m['parts'].append({
    'id': 'tail_healthy_neutral',
    'file': 'parts/tail_healthy_neutral.png',
    'source_figure': 'healthy_neutral',
    'px_rect': list(bb),
    'pivot': pivot,
    'z': 'under_core',
    'sway': {'amp_deg': 4, 'period_ms': 2600, 'phase_ms': 1300},
})
json.dump(m, open(mpath, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
print('manifest 已更新:', m['figures'], [p['id'] for p in m['parts']])

# ---- QA: 静置合成 IoU ----
comp = Image.new('RGBA', (w, h), (0, 0, 0, 0))
comp.alpha_composite(canvas)
comp.alpha_composite(C)          # 件在下、核在上 = 运行时层序
res = comp.getchannel('A').point(lambda v: 255 if v else 0)
orig_a = O.getchannel('A').point(lambda v: 255 if v else 0)
rb, ob = res.tobytes(), orig_a.tobytes()
inter = sum(1 for a, b in zip(rb, ob) if a and b)
union = sum(1 for a, b in zip(rb, ob) if a or b)
iou = inter / union
missing = sum(1 for a, b in zip(rb, ob) if b and not a)
print(f'静置合成 IoU={iou:.4f}  原图有而合成缺={missing}px')
qa = Image.new('RGB', ((1024 - 620) * 2 + 12, 1360 - 460), (24, 24, 28))
z = (620, 460, 1024, 1360)
def dark(im):
    bg = Image.new('RGBA', im.size, (30, 30, 36, 255))
    bg.alpha_composite(im.crop(z))
    return bg.crop(z).convert('RGB')
qa.paste(dark(O), (0, 0))
qa.paste(dark(comp), (z[2] - z[0] + 12, 0))
qa.save(os.path.join(REPO, 'spikes/_qa/healthy_tail_rest_composite.png'))
print('QA 已存 healthy_tail_rest_composite.png')
