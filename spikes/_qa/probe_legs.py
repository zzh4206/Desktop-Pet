"""腿部 α 拓扑探针（v0.14 Phase C 泛化版）——任一 neutral 立绘的取参工具。

用法： python spikes/_qa/probe_legs.py <stage> <branch> [x0 x1 y_top]
输出： 内容底 y、指定窗口内逐行 α>0 连续段（每 8 行+变化行），
供 split_parts 的 --seed/--pivot/--block 取参。
"""
import os
import sys

from PIL import Image

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
stage, branch = sys.argv[1], sys.argv[2]
x0, x1 = (int(sys.argv[3]), int(sys.argv[4])) if len(sys.argv) > 4 else (350, 680)
y_top = int(sys.argv[5]) if len(sys.argv) > 5 else 1100

im = Image.open(os.path.join(REPO, "assets", "ai",
                             f"{stage}_{branch}_neutral.png")).convert("RGBA")
print("size:", im.size)
px = im.load()
W, H = im.size


def runs(y):
    out = []
    cur = None
    for x in range(x0, x1):
        on = px[x, y][3] > 0
        if on and cur is None:
            cur = x
        elif not on and cur is not None:
            out.append((cur, x - 1))
            cur = None
    if cur is not None:
        out.append((cur, x1 - 1))
    return out


bottom = 0
for y in range(H - 1, 0, -1):
    if any(px[x, y][3] > 0 for x in range(0, W, 2)):
        bottom = y
        break
print("content bottom y =", bottom)

prev = None
for y in range(y_top, min(H, bottom + 4)):
    r = runs(y)
    if y % 8 == 0 or r != prev:
        print(f"y={y}: {r}")
    prev = r
