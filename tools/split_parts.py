#!/usr/bin/env python
"""部件拆切工具（v0.13）—— 从已验收静态立绘中确定性切出可动件。

工艺与决策（对应 版本规划.md §v0.13.0 / 设计思路.md 渲染路线 C 留痕）：
  · **零生成像素**：遮蔽区的填补不用任何生成式模型 —— 部件在运行时渲染于
    核心图**之下**（under_core），摆动露出的接缝由核心图自身边缘遮掩；
    挖除区只发生在紧贴透明背景的"安全域"，保护带（发丝等跨接结构）内的
    像素保留在核心图原样不动，允许少量残影压在摆动件之上（显示档缩放后
    ≈1–2px，不可辨）。
  · 输入：源立绘 + 种子点/包围盒/保护带参数；选取 = α>0 连通域 flood-fill。
  · 输出三件套入 ``assets/rig/{stage}/``：
      parts/{part}.png                 部件图（bbox 裁剪）
      figs/{figure_key}.png            派生核心图（挖除部件像素；原件不动）
      manifest.json                    合并更新 figures/parts 条目
  · ``--qa`` 另出放大对照图（spikes/_qa/）供人工目检。

用法示例（final 两分支的鲸尾）::

    python tools/split_parts.py --stage final --branch neglected \\
        --seed 950 800 --protect "x<880,y<830"
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from PIL import Image, ImageDraw, ImageFilter

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _parse_protect(expr: str):
    """比较式解析：逗号分隔单组内 AND；分号分隔多组屏障（任一命中即屏障）。

    返回 [(group_checks, ...)]：extract_component 判墙时各组内全部成立才算
    命中该组，任意组命中即为墙。
    """
    groups = []
    for seg in (expr or "").split(";"):
        checks = []
        for term in seg.split(","):
            term = term.strip()
            if not term:
                continue
            for op in ("<", ">"):
                if op in term:
                    k, v = term.split(op)
                    checks.append((k.strip(), op, float(v)))
                    break
            else:
                raise ValueError(f"--protect/--block 片段无法解析: {term}")
        if checks:
            groups.append(checks)
    return groups


def _in_group(x: int, y: int, checks) -> bool:
    """单组内全部比较式命中（AND）→ True。"""
    for coord_name, op, v in checks:
        coord = x if coord_name == "x" else y
        if op == "<" and not (coord < v):
            return False
        if op == ">" and not (coord > v):
            return False
    return True


def _hit_any(x: int, y: int, groups) -> bool:
    return any(_in_group(x, y, g) for g in groups)


def extract_component(alpha_img: Image.Image, seed: tuple[int, int],
                      barriers=()) -> set:
    """α>0 连通域 flood-fill（4 邻接）。``barriers`` 为屏障组列表
    （_parse_protect 输出）：任一组判定命中即视为墙，防止顺连进头发/裙主体。
    返回像素坐标集合（不含墙内像素）。"""
    w, h = alpha_img.size
    px = alpha_img.load()
    sx, sy = seed

    def is_wall(x: int, y: int) -> bool:
        return _hit_any(x, y, barriers)

    if not (0 <= sx < w and 0 <= sy < h) or px[sx, sy] == 0:
        raise SystemExit(f"种子 {seed} 不在 α>0 区域内")
    if is_wall(sx, sy):
        raise SystemExit(f"种子 {seed} 落在屏障区内")
    seen = set()
    stack = [(sx, sy)]
    while stack:
        x, y = stack.pop()
        if (x, y) in seen or not (0 <= x < w and 0 <= y < h):
            continue
        if px[x, y] == 0 or is_wall(x, y):
            continue
        seen.add((x, y))
        stack.extend(((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)))
    return seen


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", required=True,
                    choices=["young", "adult", "final"])
    ap.add_argument("--branch", required=True,
                    choices=["healthy", "neglected"])
    ap.add_argument("--mood", default="neutral")
    ap.add_argument("--part", default="tail")
    ap.add_argument("--seed", nargs=2, type=int, required=True,
                    metavar=("X", "Y"), help="部件内部的像素种子点")
    ap.add_argument("--min-px", type=int, default=20000,
                    help="连通域最小像素数（防误选碎屑）")
    ap.add_argument("--max-px", type=int, default=400000,
                    help="连通域最大像素数（防顺连到头发/裙）")
    ap.add_argument("--protect", default="",
                    help="保护带比较式 x<N / x>N,y<N —— 命中者保留在核心图")
    ap.add_argument("--block", default="",
                    help="屏障比较式（洪泛禁区，AND 语义；如 x<880,y<820）")
    ap.add_argument("--pivot", nargs=2, type=float, required=True,
                    metavar=("PX", "PY"), help="旋转轴（源图像素坐标）")
    ap.add_argument("--amp-deg", type=float, default=4.0)
    ap.add_argument("--period-ms", type=float, default=2600.0)
    ap.add_argument("--phase-ms", type=float, default=0.0)
    args = ap.parse_args()

    src_rel = f"assets/ai/{args.stage}_{args.branch}_{args.mood}.png"
    src_path = os.path.join(REPO, src_rel)
    im = Image.open(src_path).convert("RGBA")
    alpha = im.getchannel("A")

    barriers = _parse_protect(args.block)
    comp = extract_component(alpha, (args.seed[0], args.seed[1]), barriers)
    print(f"连通域像素数={len(comp)}")
    if not (args.min_px <= len(comp) <= args.max_px):
        print(f"❌ 像素数超出 [{args.min_px},{args.max_px}]——大概率误连到 "
              f"头发/裙子主体，请调整种子或保护带后再试", file=sys.stderr)
        return 2

    checks = _parse_protect(args.protect)
    protected = {(x, y) for (x, y) in comp if _hit_any(x, y, checks)
                 } if checks else set()
    cutting = comp - protected
    print(f"其中受保护保留={len(protected)} 实际挖除={len(cutting)}")

    # 部件 bbox（含保护带全量——部件层多带一点无害：它在核心图之下）
    xs = [p[0] for p in comp]
    ys = [p[1] for p in comp]
    bx0, by0, bx1, by1 = min(xs), min(ys), max(xs) + 1, max(ys) + 1

    part = im.crop((bx0, by0, bx1, by1))
    # 轻羽化切缘（仅软化 polygon 挖除直线感；外轮廓本就带原图抗锯齿边）
    a = part.getchannel("A").filter(ImageFilter.GaussianBlur(0.7))
    part.putalpha(a)

    core = im.copy()
    cpx = core.load()
    for (x, y) in cutting:
        r, g, b, _a = cpx[x, y]
        cpx[x, y] = (r, g, b, 0)

    out_dir = os.path.join(REPO, "assets", "rig", args.stage)
    parts_dir = os.path.join(out_dir, "parts")
    figs_dir = os.path.join(out_dir, "figs")
    os.makedirs(parts_dir, exist_ok=True)
    os.makedirs(figs_dir, exist_ok=True)

    figure_key = f"{args.branch}_{args.mood}"
    part_path = os.path.join(parts_dir, f"{args.part}_{figure_key}.png")
    fig_path = os.path.join(figs_dir, f"{figure_key}.png")
    part.save(part_path)
    core.save(fig_path)

    manifest_path = os.path.join(out_dir, "manifest.json")
    manifest = {}
    if os.path.isfile(manifest_path):
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
    figures = manifest.setdefault("figures", {})
    rel_fig = os.path.relpath(fig_path, out_dir).replace("\\\\", "/")
    figures[figure_key] = rel_fig           # 派生核心图取代整图入口

    part_id = f"{args.part}_{figure_key}"
    parts = [p for p in manifest.setdefault("parts", [])
             if p.get("id") != part_id]     # 同部件重复运行覆盖（顺清陈档）
    parts.append({
        "id": part_id,
        "file": os.path.relpath(part_path, out_dir).replace("\\\\", "/"),
        "source_figure": figure_key,
        "px_rect": [bx0, by0, bx1, by1],
        "pivot": list(args.pivot),
        "z": "under_core",
        "sway": {"amp_deg": args.amp_deg, "period_ms": args.period_ms,
                 "phase_ms": args.phase_ms},
    })
    manifest["spec"] = 1
    manifest["parts"] = parts
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    # ---- QA 对照图 ----
    qa_dir = os.path.join(REPO, "spikes", "_qa")
    os.makedirs(qa_dir, exist_ok=True)
    panel = Image.new("RGBA", (im.width * 2 // 3 * 3, im.height),
                      (24, 24, 28, 255))
    th = im.resize((im.width // 3, im.height // 3))
    co = core.resize((im.width // 3, im.height // 3))
    pt = part.resize((max(1, (bx1 - bx0) // 3), max(1, (by1 - by0) // 3)))
    panel.paste(th, (0, 0), th)
    panel.paste(co, (th.width + 8, 0), co)
    panel.paste(pt, ((th.width + 8) * 2, 0), pt)
    d = ImageDraw.Draw(panel)
    d.line([args.pivot, (bx0, by0)], fill=(255, 80, 80, 255), width=2)
    qa_path = os.path.join(
        qa_dir, f"split_{args.stage}_{figure_key}_{args.part}.png")
    panel.save(qa_path)
    print(f"✅ 部件={os.path.relpath(part_path, REPO)}\n"
          f"   派生核心={os.path.relpath(fig_path, REPO)}\n"
          f"   manifest 已合并更新（figures[{figure_key}]→figs）\n"
          f"   QA 对照={os.path.relpath(qa_path, REPO)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
