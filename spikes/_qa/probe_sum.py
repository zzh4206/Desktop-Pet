"""腿部拓扑汇总器——自动输出 5 张 neutral 的切件取参：
裙摆底缘（最后一个"窗口横满"行）、双腿 x 区间（中部行）、鞋区是否 α 连通、
腿底 y。供批量切割定 --seed/--pivot/--block。
"""
import os

from PIL import Image

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FIGS = [
    ("final", "neglected"),
    ("young", "healthy"),
    ("young", "neglected"),
    ("adult", "healthy"),
    ("adult", "neglected"),
]

for stage, branch in FIGS:
    im = Image.open(os.path.join(
        REPO, "assets", "ai", f"{stage}_{branch}_neutral.png")).convert("RGBA")
    px = im.load()
    W, H = im.size

    bottom = 0
    for y in range(H - 1, 0, -1):
        if any(px[x, y][3] > 0 for x in range(0, W, 2)):
            bottom = y
            break

    def runs(y, x0=300, x1=760):
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

    # 裙摆底缘：最后一个"单段横跨 ≥250px"的行（裙摆整幅），其下即双腿区
    hem_y = None
    for y in range(bottom - 1, max(0, bottom - 500), -1):
        r = runs(y)
        if len(r) == 1 and r[0][1] - r[0][0] >= 250:
            hem_y = y
            break
    mid = (hem_y + bottom) // 2 if hem_y else bottom - 100
    rm = runs(mid)
    # 鞋区连通性：扫 hem→bottom 全部行，记录最少段数
    min_segs = 99
    merge_rows = []
    for y in range(hem_y or 0, bottom + 1):
        n = len(runs(y))
        if n < min_segs:
            min_segs = n
        if n == 1:
            merge_rows.append(y)
    print(f"{stage}/{branch}: bottom={bottom} hem={hem_y} "
          f"mid={mid} runs@mid={rm} 鞋区最少段数={min_segs}"
          f"{(' 合并行数=' + str(len(merge_rows)) + ' 首行=' + str(merge_rows[0]) if merge_rows else '')}")
