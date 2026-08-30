#!/usr/bin/env python
"""部件顶部延伸（v0.14.10）—— 修摆动结合部"断裂"。

原理：部件（腿）顶边在裙摆墙线上，旋转时顶边某段下沉脱离裙摆下缘=可见
缝隙。把部件逐列向上延伸 N px（每列复制其最顶不透明像素并横向羽化），
延伸段常驻于核心图裙料之后（under_core 不可见），摆动时恰好填补结合部
位移——延伸长度 > sin(最大摆角)·结合部半宽 即永不露缝。

仅延伸、不改 manifest pivot；px_rect y0 上扩 N（内容坐标随之平移）。

用法： python tools/extend_part.py --stage final --part leg_front_healthy_side --up 90
"""
from __future__ import annotations

import argparse
import json
import os

from PIL import Image

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", required=True)
    ap.add_argument("--part", required=True, help="manifest 中的部件 id")
    ap.add_argument("--up", type=int, default=90, help="向上延伸像素数")
    args = ap.parse_args()

    stage_dir = os.path.join(REPO, "assets", "rig", args.stage)
    manifest_path = os.path.join(stage_dir, "manifest.json")
    manifest = json.load(open(manifest_path, encoding="utf-8"))
    info = next(p for p in manifest["parts"] if p["id"] == args.part)
    part_path = os.path.join(stage_dir, info["file"])
    part = Image.open(part_path).convert("RGBA")
    w, h = part.size
    px = part.load()

    # 逐列找最顶不透明行；仅延伸"顶部到达墙线"的列（min_top+10 以内），
    # 跳过只有鞋/碎屑的列（否则鞋的深色像素被拖出悬浮色条）
    new_h = h + args.up
    out = Image.new("RGBA", (w, new_h), (0, 0, 0, 0))
    opx = out.load()
    top_src = [None] * w
    for x in range(w):
        for y in range(h):
            if px[x, y][3] > 0:
                top_src[x] = min(y + 2, h - 1)
                break
    valid = [t for t in top_src if t is not None]
    min_top = min(valid) if valid else 0
    for x in range(w):
        ts = top_src[x]
        if ts is not None and ts <= min_top + 10:
            seed = px[x, ts]
            # 延伸段：复制种子色，最后 8px 线性淡出（软边防硬线）
            for ny in range(args.up):
                fade = min(1.0, (args.up - ny) / 8.0)
                r, g, b, a = seed
                opx[x, ny] = (r, g, b, int(a * fade))
        for y in range(h):
            opx[x, y + args.up] = px[x, y]

    out.save(part_path)
    info["px_rect"][1] -= args.up
    # 批次G/rL5（REVIEW-2026-08-31）：延伸量留痕（累计）——重切/复验
    # 不再依赖逆向 px_rect 差值
    sp = info.setdefault("split", {})
    sp["extended_up"] = int(sp.get("extended_up", 0)) + args.up
    json.dump(manifest, open(manifest_path, "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    print(f"✅ {args.part} 顶延 {args.up}px（px_rect.y0→{info['px_rect'][1]}，"
          f"画布 {w}x{h}→{w}x{new_h}）")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
