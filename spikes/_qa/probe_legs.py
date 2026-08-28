"""腿部 α 拓扑探针（v0.14 Phase A）——量出裙摆底缘/双腿间隙/脚底精确坐标。

对 final_healthy_neutral.png 在腿区窗口内逐行扫描 α>0 的 x 连续段，
打印关键行与过渡行，供 --seed/--protect/--block 取参。
"""
import os
from PIL import Image

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
im = Image.open(os.path.join(REPO, "assets", "ai", "final_healthy_neutral.png")).convert("RGBA")
print("size:", im.size)
px = im.load()
W, H = im.size


def runs(y, x0=350, x1=680):
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


# 1) 找整体内容底部（脚底）
bottom = 0
for y in range(H - 1, 0, -1):
    if any(px[x, y][3] > 0 for x in range(0, W, 2)):
        bottom = y
        break
print("content bottom y =", bottom)

# 2) 腿区逐行（每 8 行 + 变化行）
prev = None
for y in range(1100, min(H, bottom + 4)):
    r = runs(y)
    if y % 8 == 0 or r != prev:
        print(f"y={y}: {r}")
    prev = r
