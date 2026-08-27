#!/usr/bin/env python
"""walk 中间步姿生成 + 对齐（v0.13.5）。gpt-image-2 产线，须 QUANZIL_API_KEY。

对每个分支：以 {stage}_walk_0/walk_1 两张已验收步姿为参考，生成
walk_0b（0→1 过渡）与 walk_1b（1→0 过渡）两张通过步姿；随后按 walk_0 的
α 包围盒做"等比缩放钳幅 + 底对齐 + 水平居中"门禁（脚底基线一致，防走动
垂直跳动）；产出 QA 条带图 [0,0b,1,1b]×分支。
"""

from __future__ import annotations

import base64
import io
import json
import os
import sys

import requests
from PIL import Image

BASE = "https://quanzil.com/v1"
KEY = os.environ.get("QUANZIL_API_KEY", "").strip()
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FR = os.path.join(REPO, "assets", "frames")
QA = os.path.join(REPO, "spikes", "_qa")
PROMPT = (
    "These are two frames of the SAME anime character walking in side view: "
    "IMAGE 1 is stride pose A, IMAGE 2 is the opposite stride pose B. "
    "Generate ONE new frame: the exact mid-step passing pose between A and B "
    "- both legs passing under the skirt, one foot flat on the ground below "
    "the body, arms swinging through the body line, hair/skirt/tail in "
    "transit motion. IDENTICAL character design, colors, proportions, line "
    "style and shading as the references; same ground line and same scale; "
    "fully transparent background; a single full-body frame, no text, no "
    "watermark."
)


def gen_two_refs(ref_a: str, ref_b: str) -> Image.Image:
    files = [
        ("model", (None, "gpt-image-2")),
        ("prompt", (None, PROMPT)),
        ("image[]", ("a.png", open(ref_a, "rb").read(), "image/png")),
        ("image[]", ("b.png", open(ref_b, "rb").read(), "image/png")),
        ("size", (None, "1024x1536")),
        ("quality", (None, "high")),
        ("background", (None, "transparent")),
    ]
    r = requests.post(f"{BASE}/images/edits",
                      headers={"Authorization": f"Bearer {KEY}"},
                      files=files, timeout=600)
    if r.status_code == 200:
        d = r.json()
        return Image.open(io.BytesIO(
            base64.b64decode(d["data"][0]["b64_json"]))).convert("RGBA")
    print(f"  多参考失败 HTTP {r.status_code}: {r.text[:200]} —— 降级单参考")
    files = [
        ("model", (None, "gpt-image-2")),
        ("prompt", (None, PROMPT.replace(
            "IMAGE 1 is stride pose A, IMAGE 2 is the opposite stride pose B. ",
            "IMAGE 1 is stride pose A; the opposite stride pose B is described "
            "as: the other leg forward. "))),
        ("image[]", ("a.png", open(ref_a, "rb").read(), "image/png")),
        ("size", (None, "1024x1536")),
        ("quality", (None, "high")),
        ("background", (None, "transparent")),
    ]
    r = requests.post(f"{BASE}/images/edits",
                      headers={"Authorization": f"Bearer {KEY}"},
                      files=files, timeout=600)
    if r.status_code != 200:
        raise SystemExit(f"单参考也失败 {r.status_code}: {r.text[:300]}")
    d = r.json()
    return Image.open(io.BytesIO(
        base64.b64decode(d["data"][0]["b64_json"]))).convert("RGBA")


def align_to(ref: Image.Image, new: Image.Image) -> Image.Image:
    """等比缩放使 bbox 高度=参考 bbox 高度（钳幅 ±20%），底对齐+水平居中。"""
    canvas = Image.new("RGBA", ref.size, (0, 0, 0, 0))
    rb, nb = ref.getbbox(), new.getbbox()
    if not nb:
        raise SystemExit("生成件全透明")
    rh, nh = rb[3] - rb[1], nb[3] - nb[1]
    s = rh / nh
    if not (0.8 <= s <= 1.25):
        print(f"  ⚠️ 高度比 {s:.3f} 超钳幅，取边界值")
        s = max(0.8, min(1.25, s))
    nw, nh2 = int(new.width * s), int(new.height * s)
    new = new.resize((nw, nh2), Image.LANCZOS)
    nb = new.getbbox()
    cx_ref = (rb[0] + rb[2]) // 2
    cx_new = (nb[0] + nb[2]) // 2
    dx = cx_ref - cx_new
    dy = rb[3] - nb[3]          # 底对齐（脚贴同一基线）
    canvas.alpha_composite(new, (dx, dy))
    return canvas


def main() -> int:
    if not KEY:
        raise SystemExit("缺少 QUANZIL_API_KEY")
    os.makedirs(QA, exist_ok=True)
    strips = []
    for branch in ("neglected", "healthy"):
        stage = "final"
        p0 = os.path.join(FR, f"{stage}_walk_0.png")
        p1 = os.path.join(FR, f"{stage}_walk_1.png")
        ref0, ref1 = Image.open(p0).convert("RGBA"), Image.open(p1).convert("RGBA")
        for name, (ra, rb_) in (("walk_0b", (p0, p1)),
                                ("walk_1b", (p1, p0))):
            out_path = os.path.join(FR, f"{stage}_{name}.png")
            if os.path.isfile(out_path) and "--force" not in sys.argv:
                print(f"{branch}/{name} 已存在，跳过（--force 重生成）")
            else:
                print(f"生成 {stage}_{name} ...")
                img = gen_two_refs(ra, rb_)
                img.save(os.path.join(QA, f"_{stage}_{name}_raw.png"))
                aligned = align_to(ref0, img)
                aligned.save(out_path)
    # QA 条带：每分支一排 [w0, 0b, w1, 1b, w0]（收口验证循环连贯性）
    TH = 420
    def th(im):
        t = im.copy(); t.thumbnail((10000, TH)); return t
    for branch in ("neglected", "healthy"):
        seq = ["walk_0", "walk_0b", "walk_1", "walk_1b", "walk_0"]
        row = [th(Image.open(os.path.join(FR, f"final_{n}.png")).convert("RGBA"))
               for n in seq]
        strip = Image.new("RGB", (sum(p.width for p in row) + 8 * len(row),
                                  TH), (30, 30, 36, 255))
        x = 0
        for p in row:
            strip.paste(p, (x, 0), p)
            x += p.width + 8
        strip.save(os.path.join(QA, f"walk_cycle_{branch}.png"))
        print(f"QA 条带 walk_cycle_{branch}.png（顺序 w0,0b,w1,1b,w0）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
