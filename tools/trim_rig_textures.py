"""一次性资产脚本：裁掉 rig 20 层纹理的透明边 + 回写 spec trim_offset_px。

P1（纹理 trim，无损）——按 alpha>0 bbox 留 2px 透明 margin 裁透明 padding；
瞳 2 层按 gaze_ellipse ± axis_limits_px ± 2px 裁（保 look-at 边缘，防 CLAMP_TO_EDGE
破相）。裁后 raw RGBA 124.32MB → ~17MB（−86%）。顶点/骨骼/权重活在画布空间
不动，运行时只改采样 UV（mesh_generator 两分支 + spec trim_offset_px）。

用法：
    python tools/trim_rig_textures.py --dry-run     # 只打印 bbox/节省，不落盘
    python tools/trim_rig_textures.py --apply        # 裁 PNG + 回写 spec

原图受 git 跟踪，`git checkout -- assets/rig_young/` 可回退。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import numpy as np
from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPEC_PATH = os.path.join(ROOT, "assets", "rig_young", "spec.json")
LAYERS_DIR = os.path.join(ROOT, "assets", "rig_young", "layers")
MARGIN = 2


def clamp_bbox(x0: int, y0: int, x1: int, y1: int, w: int, h: int) -> tuple[int, int, int, int]:
    x0 = max(0, int(x0)); y0 = max(0, int(y0))
    x1 = min(w, int(x1)); y1 = min(h, int(y1))
    return x0, y0, x1, y1


def layer_bbox(layer: dict, img: Image.Image, axis_limits: list) -> tuple[int, int, int, int]:
    w, h = img.size
    gaze = layer.get("gaze_ellipse")
    if gaze:
        # 瞳层：按 gaze 椭圆 ± 注视轴 ± margin 裁，保 look-at UV 移出范围时
        # 边缘不粘（CLAMP_TO_EDGE）。gaze_ellipse 为 [cx, cy, rx, ry]（画布像素）。
        cx, cy, rx, ry = (float(v) for v in gaze)
        ax, ay = (float(v) for v in axis_limits)
        return clamp_bbox(cx - rx - ax - MARGIN, cy - ry - ay - MARGIN,
                          cx + rx + ax + MARGIN, cy + ry + ay + MARGIN, w, h)
    alpha = np.asarray(img)[:, :, 3]
    ys, xs = np.where(alpha > 0)
    if not len(xs):
        raise ValueError(f"{layer['id']}: 无 alpha>0 内容")
    return clamp_bbox(xs.min() - MARGIN, ys.min() - MARGIN,
                      xs.max() + 1 + MARGIN, ys.max() + 1 + MARGIN, w, h)


def load_spec() -> tuple[dict, str]:
    # 先读全文再 json 解析：json.load(f) 会把文件指针推到 EOF，再 f.read() 只能拿到空串。
    with open(SPEC_PATH, encoding="utf-8") as f:
        raw_text = f.read()
    return json.loads(raw_text), raw_text


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="实际裁切 + 回写（缺省 dry-run）")
    args = ap.parse_args()

    spec, raw_text = load_spec()
    axis_limits = (spec.get("face_mechanics", {}).get("look_at", {})
                   .get("axis_limits_px", [10, 7]))

    total_before = 0
    total_after = 0
    plan: list[tuple[str, str, tuple[int, int, int, int], tuple[int, int]]] = []

    for layer in spec["layers"]:
        lid = layer["id"]
        texture = layer.get("texture", f"{lid}.png")
        path = os.path.join(LAYERS_DIR, texture)
        if not os.path.isfile(path):
            print(f"[SKIP] {lid}: 缺纹理 {texture}")
            continue
        img = Image.open(path).convert("RGBA")
        w, h = img.size
        bbox = layer_bbox(layer, img, axis_limits)
        x0, y0, x1, y1 = bbox
        if (x1 - x0) <= 0 or (y1 - y0) <= 0:
            print(f"[SKIP] {lid}: 空 bbox")
            continue
        before = w * h * 4
        after = (x1 - x0) * (y1 - y0) * 4
        total_before += before
        total_after += after
        plan.append((lid, texture, bbox, (w, h)))
        print(f"  {lid:14s} {texture:28s} {w}x{h} -> {x1-x0}x{y1-y0}  "
              f"offset=[{x0},{y0}]  省 {100*(1-after/before):5.1f}%")

    print(f"\n合计 raw RGBA: {total_before/1024/1024:.2f}MB -> "
          f"{total_after/1024/1024:.2f}MB（省 {100*(1-total_after/total_before):.1f}%）")

    if not args.apply:
        print("\n[dry-run] 未落盘。加 --apply 执行裁切 + 回写 spec。")
        return 0

    # 落盘：裁 PNG + 文本级插入 trim_offset_px（不整体重排 spec 减少 diff）
    for lid, texture, bbox, _ in plan:
        path = os.path.join(LAYERS_DIR, texture)
        img = Image.open(path).convert("RGBA")
        img.crop(bbox).save(path)
        # 文本插入：在 `"id": "<lid>",` 后追加同缩进的 trim_offset_px
        anchor = f'      "id": "{lid}",'
        if anchor not in raw_text:
            print(f"[WARN] {lid}: 未找到锚点 {anchor!r}")
            continue
        rest = raw_text.split(anchor, 1)[1]
        if '"trim_offset_px"' in rest.split("\n      }", 1)[0]:
            print(f"[WARN] {lid}: 已有 trim_offset_px，跳过插入")
            continue
        insert = f'{anchor}\n      "trim_offset_px": [{bbox[0]}, {bbox[1]}],'
        raw_text = raw_text.replace(anchor, insert, 1)

    with open(SPEC_PATH, "w", encoding="utf-8") as f:
        f.write(raw_text)
    print(f"\n[apply] 已裁 {len(plan)} 层 + 回写 spec trim_offset_px")
    return 0


if __name__ == "__main__":
    sys.exit(main())
