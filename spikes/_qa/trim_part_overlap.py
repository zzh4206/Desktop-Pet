"""部件冗余遮蔽裁除（v0.13.2）—— 修"摆尾连带裙角"。

成因：parts 里混入的裙料/蕾丝/发丝像素在静置时被核心图覆盖不可见，
旋转时就从核心轮廓下滑出来随尾同摆（healthy 件白蕾丝角距转轴 160-265px，
±4° 位移 3-6 显示像素，肉眼可辨）。

修法：部件画布位姿上，凡落在"核心 α>0 膨胀 3px"内的像素一律裁除——
它们静置时本就冗余（核心自带同位内容），裁除后摆动无物可露；尾-裙交界
处旋转让出的细缝由核心的"无尾重建"边缘天然补位。

重裁后 px_rect 沿用 manifest 原值不变（内容只减不增）。
"""

from __future__ import annotations

import json
import os

from PIL import Image, ImageFilter

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
STAGE = os.path.join(REPO, "assets", "rig", "final")
MANIFEST = json.load(open(os.path.join(STAGE, "manifest.json"), encoding="utf-8"))

for part_info in MANIFEST["parts"]:
    pid = part_info["id"]
    fig_name = part_info["source_figure"]
    part_path = os.path.join(STAGE, part_info["file"])
    core_path = os.path.join(STAGE, MANIFEST["figures"][fig_name])

    part = Image.open(part_path).convert("RGBA")
    core = Image.open(core_path).convert("RGBA")
    x0, y0, x1, y1 = [int(v) for v in part_info["px_rect"]]
    pw, ph = x1 - x0, y1 - y0
    if part.size != (pw, ph):
        raise SystemExit(f"{pid} 部件尺寸 {part.size} ≠ px_rect {(pw,ph)}")

    # 核心不透明掩码（全画布）+ 膨胀 1px（抗 AA 边即可；过大加剧旋离侧凹口）
    core_a = core.getchannel("A").point(lambda v: 255 if v > 8 else 0)
    covered = core_a.filter(ImageFilter.MaxFilter(3))

    # 部件摆到画布位姿 → 与 covered 相交处清零
    layer = Image.new("RGBA", core.size, (0, 0, 0, 0))
    layer.alpha_composite(part, (x0, y0))
    lp, cp = layer.load(), covered.load()
    erased = kept = 0
    for y in range(core.height):
        for x in range(core.width):
            if lp[x, y][3] and cp[x, y]:
                lp[x, y] = (0, 0, 0, 0)
                erased += 1
            elif lp[x, y][3]:
                kept += 1
    new_part = layer.crop((x0, y0, x1, y1))
    # bbox 边缘 1px 清零（裁切矩形边界残留会渲染成悬浮亮框）
    np_ = new_part.load()
    for x in range(new_part.width):
        np_[x, 0] = (0, 0, 0, 0)
        np_[x, new_part.height - 1] = (0, 0, 0, 0)
    for y in range(new_part.height):
        np_[0, y] = (0, 0, 0, 0)
        np_[new_part.width - 1, y] = (0, 0, 0, 0)
    new_part.save(part_path)
    print(f"{pid}: 裁除 {erased} 保留 {kept} → {os.path.relpath(part_path, REPO)}")

print("done")
