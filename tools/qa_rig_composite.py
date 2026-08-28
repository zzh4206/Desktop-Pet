#!/usr/bin/env python
"""静止合成门禁（v0.14 Phase A）—— 验证「核心图+部件@0°」与原图不可辨差异。

paper-doll 铁律的量化形态：拆件只允许改变*运动时*的画面，静止复原图必须
≈ 原图（差异只允许来自切缘 0.7px 羽化）。逐像素比对 alpha 与 RGB，打印
指标并出三联 QA 图（原图 | 复合 | 差异热区），存 spikes/_qa/。
纯 PIL 实现（运行环境无 numpy）。

用法::

    python tools/qa_rig_composite.py --stage final --branch healthy --mood neutral
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from PIL import Image, ImageChops, ImageFilter, ImageStat

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", required=True,
                    choices=["young", "adult", "final"])
    ap.add_argument("--branch", required=True,
                    choices=["healthy", "neglected"])
    ap.add_argument("--mood", default="neutral")
    ap.add_argument("--alpha-thr", type=int, default=32,
                    help="α 差超过该值计入失配（0-255）")
    ap.add_argument("--max-pct", type=float, default=0.5,
                    help="失配像素占原图不透明像素百分比门禁")
    ap.add_argument("--exclude", default="",
                    help="已知存量差异区 x0,y0,x1,y1（不计门禁，如 v0.13 尾件"
                         "保护带残影基线）")
    args = ap.parse_args()

    figure = f"{args.branch}_{args.mood}"
    rig_dir = os.path.join(REPO, "assets", "rig", args.stage)
    with open(os.path.join(rig_dir, "manifest.json"), "r", encoding="utf-8") as f:
        manifest = json.load(f)

    orig = Image.open(os.path.join(
        REPO, "assets", "ai", f"{args.stage}_{figure}.png")).convert("RGBA")
    core_path = manifest["figures"].get(figure)
    if not core_path:
        print(f"❌ manifest 无 figure {figure}（未拆件）")
        return 2
    core = Image.open(os.path.join(rig_dir, core_path)).convert("RGBA")

    layer = Image.new("RGBA", orig.size, (0, 0, 0, 0))
    n_parts = 0
    for p in manifest["parts"]:
        if p["source_figure"] != figure:
            continue
        part = Image.open(os.path.join(rig_dir, p["file"])).convert("RGBA")
        x0, y0, _x1, _y1 = p["px_rect"]
        layer.paste(part, (x0, y0))
        n_parts += 1
    comp = Image.alpha_composite(layer, core)   # 核心图压在部件之上

    a_o = orig.getchannel("A")
    a_c = comp.getchannel("A")
    da = ImageChops.difference(a_o, a_c)
    hist = da.histogram()
    mism = sum(hist[args.alpha_thr + 1:])
    opaque = sum(a_o.histogram()[1:])
    pct = mism * 100.0 / max(1, opaque)
    max_da = max(i for i, n in enumerate(hist) if n)

    # 内部失配 = 实心区（α>128 二值化后腐蚀 2px）内的失配。两点原因：
    # ① 切缘羽化在轮廓外缘留 ~1px α 环（工艺固有、显示档不可辨）→ 只计环；
    # ② 本批素材 α 带半调抖动，必须先二值化再腐蚀（直接腐蚀抖动区全灭）。
    # 真问题（缺块/错位/烤死件）必然出现在内部。
    bin_o = a_o.point(lambda v: 255 if v > 128 else 0)
    interior = bin_o.filter(ImageFilter.MinFilter(5))
    da_strong = da.point(lambda v: 255 if v > args.alpha_thr else 0)
    if args.exclude:
        ex = tuple(int(v) for v in args.exclude.split(","))
        da_strong.paste(0, box=ex)
        interior.paste(0, box=ex)
    inter_mism = ImageStat.Stat(da_strong, interior).sum[0] // 255
    inter_opaque = sum(interior.histogram()[1:])   # 桶值=像素数，不再除 255
    inter_pct = inter_mism * 100.0 / max(1, inter_opaque)

    # 不透明双区（α>128）RGB 平均差（ImageStat 支持 L 掩码）
    mask = ImageChops.multiply(
        a_o.point(lambda v: 255 if v > 128 else 0),
        a_c.point(lambda v: 255 if v > 128 else 0))
    rgb_diff = ImageChops.difference(orig.convert("RGB"), comp.convert("RGB"))
    rgb_mean = ImageStat.Stat(rgb_diff, mask).mean if mask.getbbox() else [0.0]
    rgb_d = sum(rgb_mean) / len(rgb_mean)

    print(f"figure={args.stage}/{figure} parts={n_parts}")
    print(f"全图: 不透明={opaque}  |Δα|>{args.alpha_thr} 失配={mism} "
          f"({pct:.3f}%)  maxΔα={max_da}  平均ΔRGB={rgb_d:.2f}")
    print(f"内部(腐蚀2px): 不透明={inter_opaque}  失配={inter_mism} "
          f"({inter_pct:.3f}%)   轮廓环失配={mism - inter_mism}")

    # 差异热区图（红=α 失配，暗底）
    heat = Image.new("RGBA", orig.size, (24, 24, 28, 255))
    red = Image.new("RGBA", orig.size, (255, 60, 60, 255))
    heat.paste(red, (0, 0), da.point(lambda v: 255 if v > args.alpha_thr else 0))

    tw, thh = orig.width // 3, orig.height // 3
    panel = Image.new("RGBA", (tw * 3 + 16, thh), (24, 24, 28, 255))
    for i, img in enumerate((orig, comp, heat)):
        t = img.resize((tw, thh))
        panel.paste(t, (i * (tw + 8), 0), t)
    qa_dir = os.path.join(REPO, "spikes", "_qa")
    os.makedirs(qa_dir, exist_ok=True)
    qa_path = os.path.join(qa_dir, f"restcomp_{args.stage}_{figure}.png")
    panel.save(qa_path)

    ok = inter_pct <= args.max_pct
    print(f"{'✅ PASS' if ok else '❌ FAIL'}（内部门禁 ≤{args.max_pct}%）"
          f"  QA 三联图={os.path.relpath(qa_path, REPO)}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
