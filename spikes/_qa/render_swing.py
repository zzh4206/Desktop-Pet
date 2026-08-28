"""trim 后摆角条带重渲（v0.14 Phase A）——split_parts --qa-swing 的条带
取的是 trim 前部件像素；本脚本按 manifest 现存（已裁除）部件重渲条带，
供人工目检实际入库件的摆动全程接缝/出界。

用法： python spikes/_qa/render_swing.py [stage] [figure] [amp_deg]
"""
import json
import os
import sys

from PIL import Image, ImageDraw

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
STAGE = sys.argv[1] if len(sys.argv) > 1 else "final"
FIGURE = sys.argv[2] if len(sys.argv) > 2 else "healthy_neutral"
AMP = float(sys.argv[3]) if len(sys.argv) > 3 else 10.0

stage_dir = os.path.join(REPO, "assets", "rig", STAGE)
manifest = json.load(open(os.path.join(stage_dir, "manifest.json"),
                          encoding="utf-8"))
core = Image.open(os.path.join(stage_dir,
                               manifest["figures"][FIGURE])).convert("RGBA")

parts = [p for p in manifest["parts"] if p["source_figure"] == FIGURE]
th_w, th_h = core.width // 3, core.height // 3
angles = [-AMP, -AMP / 2, 0.0, AMP / 2, AMP]
strip = Image.new("RGBA", ((th_w * len(parts) + 8 * (len(parts) - 1))
                           * len(angles), th_h), (24, 24, 28, 255))
d = ImageDraw.Draw(strip)
col = 0
for p in parts:
    part = Image.open(os.path.join(stage_dir, p["file"])).convert("RGBA")
    x0, y0 = int(p["px_rect"][0]), int(p["px_rect"][1])
    for i, ang in enumerate(angles):
        layer = Image.new("RGBA", core.size, (0, 0, 0, 0))
        layer.paste(part, (x0, y0))
        layer = layer.rotate(ang, resample=Image.BICUBIC,
                             center=tuple(float(v) for v in p["pivot"]))
        comp = Image.alpha_composite(layer, core)
        strip.paste(comp.resize((th_w, th_h)),
                    ((col * len(angles) + i) * (th_w + 8), 0))
    d.text(((col * len(angles)) * (th_w + 8) + 4, 4),
           f"{p['id']} ±{AMP}", fill=(255, 255, 255, 255))
    col += 1

out = os.path.join(REPO, "spikes", "_qa",
                   f"swing_{STAGE}_{FIGURE}_trimmed.png")
strip.save(out)
print(os.path.relpath(out, REPO))
