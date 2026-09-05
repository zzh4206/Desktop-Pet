#!/usr/bin/env python
"""gpt-image-2 补绘工具（v0.13.1）—— quanzil 中转 /v1/images/edits 封装。

安全与产线约定：
  · **密钥只走环境变量** ``QUANZIL_API_KEY``，绝不入仓/入日志。
  · **区域回贴门禁（构造性零漂移）**：``--patch`` 模式把生成图仅按补绘区
    掩码拷回原图其余像素逐字节保留原图 —— 画风一致性由"区域外=原图"构造
    保证，规避 v0.10.19 整图重生成漂移事故的成因。

用法：
  # 探针（验证连通/模型名/响应形态，低成本）
  python tools/gen_fill.py probe

  # 区域补绘：输入带透明洞的原图 + 编辑掩码（洞=透明可编辑），输出整图
  python tools/gen_fill.py fill --image core_hole.png --mask hole_mask.png \\
      --prompt "..." --out filled.png [--size 1024x1536] [--quality high]

  # 区域回贴：把 filled.png 的补绘岛拷回 original.png
  python tools/gen_fill.py patch --original assets/ai/final_healthy_neutral.png \\
      --filled filled.png --mask hole_mask.png --out figs_out.png
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import sys

import requests
from PIL import Image

BASE = "https://quanzil.com/v1"
MODEL = "gpt-image-2"


def _key() -> str:
    key = os.environ.get("QUANZIL_API_KEY", "").strip()
    if not key:
        raise SystemExit("缺少环境变量 QUANZIL_API_KEY（不要把密钥写进命令行历史/文件）")
    return key


def _save_b64(b64: str, out: str) -> None:
    Image.open(io.BytesIO(base64.b64decode(b64))).convert("RGBA").save(out)
    print(f"✅ 已保存 {out}")


def probe(_args=None) -> int:
    r = requests.post(
        f"{BASE}/images/generations",
        headers={"Authorization": f"Bearer {_key()}"},
        json={"model": MODEL, "prompt": "a single small blue dot on white",
              "size": "1024x1024", "quality": "low", "n": 1},
        timeout=300,
    )
    print("HTTP", r.status_code)
    try:
        data = r.json()
    except ValueError:
        print(r.text[:800])
        return 1
    if r.status_code == 200:
        item = (data.get("data") or [{}])[0]
        if item.get("b64_json"):
            _save_b64(item["b64_json"], "spikes/_qa/probe_gen.png")
        else:
            print("data[0] keys:", list(item.keys()), str(item)[:300])
        usage = data.get("usage")
        if usage:
            print("usage:", json.dumps(usage, ensure_ascii=False))
        return 0
    print(json.dumps(data, ensure_ascii=False)[:1500])
    return 1


def mask_png(path: str) -> tuple[bytes, str]:
    with open(path, "rb") as f:
        return f.read(), "image/png"


def file_png(path: str) -> tuple[bytes, str]:
    with open(path, "rb") as f:
        return f.read(), "image/png"


def fill(args) -> int:  # noqa: 接口统一
    im_bytes, _ = file_png(args.image)
    mk_bytes, _ = mask_png(args.mask)
    files = {
        "model": (None, MODEL),
        "prompt": (None, args.prompt),
        "image": ("image.png", im_bytes, "image/png"),
        "mask": ("mask.png", mk_bytes, "image/png"),
        "size": (None, args.size),
    }
    if args.quality:
        files["quality"] = (None, args.quality)
    if args.background:
        files["background"] = (None, args.background)
    r = requests.post(
        f"{BASE}/images/edits",
        headers={"Authorization": f"Bearer {_key()}"},
        files=files,
        timeout=600,
    )
    print("HTTP", r.status_code)
    try:
        data = r.json()
    except ValueError:
        print(r.text[:800])
        return 1
    if r.status_code != 200:
        print(json.dumps(data, ensure_ascii=False)[:1500])
        return 1
    item = (data.get("data") or [{}])[0]
    if not item.get("b64_json"):
        print("无 b64_json：", str(item)[:400])
        return 1
    _save_b64(item["b64_json"], args.out)
    return 0


def patch(args) -> int:
    """回贴：mask 不透明(α>0)处取 filled 像素，其余 = original。"""
    orig = Image.open(args.original).convert("RGBA")
    filled = Image.open(args.filled).convert("RGBA")
    mask = Image.open(args.mask)
    # 批次C/P3-19（REVIEW-2026-09-05）：掩码必须有真实 alpha 通道——
    # RGB/L 掩码 convert("RGBA") 后 α 恒 255，"区域外逐字节=原图"的
    # 构造性零漂移静默失效为全图回贴且仍报成功
    if mask.mode not in ("RGBA", "LA", "PA") \
            and "transparency" not in mask.info:
        raise SystemExit(
            f"掩码 {args.mask} 无 alpha 通道（mode={mask.mode}）——"
            "改用带透明的 PNG 掩码，否则无法界定补绘岛")
    mask = mask.convert("RGBA")
    if mask.size != orig.size or filled.size != orig.size:
        raise SystemExit(f"尺寸不一致 o={orig.size} f={filled.size} m={mask.size}")
    op, fp, mp = orig.load(), filled.load(), mask.load()
    copied = 0
    for y in range(orig.height):
        for x in range(orig.width):
            if mp[x, y][3] > 0:          # 掩码可见 ⇒ 该像素属于补绘岛
                op[x, y] = fp[x, y]
                copied += 1
    total = orig.width * orig.height
    if copied >= total:
        raise SystemExit("回贴覆盖全图（掩码无透明区），拒绝落盘——"
                         "零漂移保证失效，请检查掩码")
    orig.save(args.out)
    print(f"✅ 回贴完成 {args.out} —— 改动 {copied}/{total} 像素"
          f"（{copied * 100.0 / total:.2f}%），区域外逐字节=原图（构造性零漂移）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("probe")
    f = sub.add_parser("fill")
    f.add_argument("--image", required=True)
    f.add_argument("--mask", required=True)
    f.add_argument("--prompt", required=True)
    f.add_argument("--out", required=True)
    f.add_argument("--size", default="1024x1536")
    f.add_argument("--quality", default=None,
                   help="low/medium/high；正式件建议 medium 起")
    f.add_argument("--background", default=None)
    p = sub.add_parser("patch")
    p.add_argument("--original", required=True)
    p.add_argument("--filled", required=True)
    p.add_argument("--mask", required=True)
    p.add_argument("--out", required=True)
    args = ap.parse_args()
    return {"probe": probe, "fill": fill, "patch": patch}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
