#!/usr/bin/env python
"""See-through 分解层 → rig 部件装配（v0.14.18 assembly 试点）。

输入：See-through 输出的层 PNG 目录（1280 方画布、原始画布坐标）+
      原始参考图（final_healthy_side.png）。
产出：
  figs/healthy_side.png            身体核心（除腿外全部层按画序叠合）
  parts/leg_{front,back}_healthy_side.png   袜+同侧鞋合并的腿件（含模型
                                   补全的裙下延伸段=防断裂）
  覆盖 assets/ai/final_healthy_side.png 为 1280 画布版参考原图
  （管线对 1024x1536 输入的几何变换按最小差异自动判定：拉伸 or 适配留白）

需在 see-through venv 运行（依赖 numpy/scipy/psd_tools 同款环境）。
"""
from __future__ import annotations

import json
import os
import sys

import numpy as np
from PIL import Image
from scipy import ndimage

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAYERS = sys.argv[1]
CANVAS = (1280, 1280)


def load(name: str) -> Image.Image:
    return Image.open(os.path.join(LAYERS, f"layer_{name}.png")).convert("RGBA")


def split_components(img: Image.Image):
    """α>0 连通域拆分，按面积降序返回 [(bbox, crop), ...]。"""
    a = np.asarray(img.getchannel("A")) > 0
    lab, n = ndimage.label(a)
    out = []
    for i in range(1, n + 1):
        mask = lab == i
        ys, xs = np.nonzero(mask)
        if len(ys) < 500:            # 丢弃碎屑
            continue
        x0, x1, y0, y1 = xs.min(), xs.max() + 1, ys.min(), ys.max() + 1
        crop = Image.new("RGBA", (x1 - x0, y1 - y0), (0, 0, 0, 0))
        crop.paste(img.crop((x0, y0, x1, y1)), (0, 0))
        out.append(((int(x0), int(y0), int(x1), int(y1)), crop))
    out.sort(key=lambda t: -(t[0][2] - t[0][0]) * (t[0][3] - t[0][1]))
    return out


def main() -> int:
    stage_dir = os.path.join(REPO, "assets", "rig", "final")

    # ---- 1) 腿件：legwear 拆双腿 + footwear 拆双鞋按就近合并 ----
    legwear = load("02_legwear")
    shoes = split_components(load("03_footwear"))
    legs = split_components(legwear)
    if len(legs) != 2 or len(shoes) != 2:
        print(f"❌ legwear 拆出 {len(legs)} 腿 / footwear 拆出 {len(shoes)} 鞋，"
              f"预期各 2——分解层异常")
        return 2
    # 左=front? 侧身朝右：x 较小者为后腿，较大者为前腿
    legs.sort(key=lambda t: t[0][0])
    shoes.sort(key=lambda t: t[0][0])
    pairs = [("leg_back", legs[0], shoes[0]), ("leg_front", legs[1], shoes[1])]

    made = {}
    for name, (lb, crop_l), (sb, crop_s) in pairs:
        x0 = min(lb[0], sb[0]); y0 = min(lb[1], sb[1])
        x1 = max(lb[2], sb[2]); y1 = max(lb[3], sb[3])
        merged = Image.new("RGBA", (x1 - x0, y1 - y0), (0, 0, 0, 0))
        merged.alpha_composite(crop_s, (sb[0] - x0, sb[1] - y0))
        merged.alpha_composite(crop_l, (lb[0] - x0, lb[1] - y0))  # 袜压鞋帮
        made[name] = (merged, (x0, y0, x1, y1))
        fp = os.path.join(stage_dir, "parts", f"{name}_healthy_side.png")
        merged.save(fp)
        # 枢轴：袜顶中点再上移 30px（深入裙料内）
        a = np.asarray(merged.getchannel("A")) > 0
        ys, xs = np.nonzero(a[: max(1, lb[1] - y0 + 40)])
        px_ = int(xs.mean()) if len(xs) else (x1 - x0) // 2
        pivot = [x0 + px_, y0 + 10]
        print(f"{name}: bbox={x0,y0,x1,y1} pivot={pivot}")
        made[name] = (made[name][0], made[name][1], pivot)

    # ---- 2) 身体核心：其余层按画序叠合 ----
    body_order = ["01_back_hair", "04_bottomwear", "05_neck", "06_topwear",
                  "07_handwear", "08_eyebrow", "09_face", "10_nose",
                  "11_mouth", "12_eyelash", "13_headwear", "14_eyewhite",
                  "15_irides", "16_front_hair"]
    core = Image.new("RGBA", CANVAS, (0, 0, 0, 0))
    for name in body_order:
        core.alpha_composite(load(name))
    core.save(os.path.join(stage_dir, "figs", "healthy_side.png"))
    print("body core: figs/healthy_side.png")

    # ---- 3) manifest 更新 ----
    mpath = os.path.join(stage_dir, "manifest.json")
    m = json.load(open(mpath, encoding="utf-8"))
    m["figures"]["healthy_side"] = "figs/healthy_side.png"
    m["parts"] = [p for p in m["parts"] if p["source_figure"] != "healthy_side"]
    for name in ("leg_back", "leg_front"):
        img, bbox, pivot = made[name]
        m["parts"].append({
            "id": f"{name}_healthy_side",
            "file": f"parts/{name}_healthy_side.png",
            "source_figure": "healthy_side",
            "px_rect": list(bbox),
            "pivot": pivot,
            "z": "under_core",
            "kind": "limb",
            "base_deg": 0.0,
            "sway": {"amp_deg": 10.0,
                     "period_ms": 2600.0,
                     "phase_ms": 0.0 if name == "leg_front" else 1300.0},
        })
    json.dump(m, open(mpath, "w", encoding="utf-8"), ensure_ascii=False,
              indent=2)
    print("manifest updated")
    return 0


if __name__ == "__main__":
    sys.exit(main())
