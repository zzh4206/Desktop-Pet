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

# 批次F/C1（REVIEW-2026-08-28）：同 figure 部件两两 α 交叠上限——静止
# 叠加恰好复原原图，静止门禁对"同一连通域拆两遍"结构性失明（C1：final/
# neglected 双腿件 27k 像素 100% 互含仍 PASS）；摆动时两件绕不同 pivot
# 各转 = 四腿重影。羽化切缘 ~1-2px 共享，阈值留 256px 余量。
_OVERLAP_MAX_PX = 256


def part_overlap_px(rig_dir: str, manifest: dict, figure: str) -> list:
    """同 figure 部件两两 α>0 交叠像素数（跨 bbox 互含也计入）。

    返回 [(id_a, id_b, overlap_px), ...]（仅同 figure 部件对）。
    批次K：部件 split.overlap_ok_with 声明的**结构遮挡对**（如发缕在尾前，
    两件不同 pivot 各自摆动=正确前后关系，非 C1 同域拆两遍）不计入——
    静止合成仍由内部门禁把关，摆动观感由 --qa-swing 条带人工目检把关。
    """
    parts = [p for p in manifest["parts"]
             if p["source_figure"] == figure and p.get("kind") != "blink"]
    if len(parts) < 2:
        return []

    def _waived(a: dict, b: dict) -> bool:
        ok_a = (a.get("split", {}) or {}).get("overlap_ok_with") or []
        ok_b = (b.get("split", {}) or {}).get("overlap_ok_with") or []
        return a["id"] in ok_b or b["id"] in ok_a

    # C3（REVIEW-2026-09-04）：豁免引用存在性校验——拼错的 id 静默不生效
    ids = {p["id"] for p in parts}
    for p in parts:
        for ref in (p.get("split", {}) or {}).get("overlap_ok_with") or []:
            if ref not in ids:
                print(f"⚠️ overlap_ok_with 引用不存在的部件 "
                      f"{p['id']}→{ref}（豁免不生效，疑似拼错）")

    loaded = []
    max_w = max_h = 0
    for p in parts:
        img = Image.open(os.path.join(rig_dir, p["file"])).convert("RGBA")
        x0, y0 = int(p["px_rect"][0]), int(p["px_rect"][1])
        # 公共画布：各部件 bbox 不同，必须贴回同一尺寸才能逐对比对
        max_w = max(max_w, x0 + img.width)
        max_h = max(max_h, y0 + img.height)
        loaded.append((p, img, x0, y0))
    alphas = []
    for p, img, x0, y0 in loaded:
        full = Image.new("L", (max_w, max_h), 0)
        full.paste(img.getchannel("A").point(lambda v: 255 if v > 0 else 0),
                   (x0, y0))
        alphas.append((p, full))
    out = []
    for i in range(len(alphas)):
        for j in range(i + 1, len(alphas)):
            pa, fa = alphas[i]
            pb, fb = alphas[j]
            if _waived(pa, pb):
                continue
            inter = ImageChops.multiply(fa, fb)
            out.append((pa["id"], pb["id"],
                        sum(inter.histogram()[1:]) if inter.getbbox() else 0))
    return out


def evaluate(stage: str, branch: str, mood: str, alpha_thr: int = 32,
             max_pct: float = 0.5, exclude: str = "",
             write_qa: bool = True) -> dict:
    """单 figure 静止合成门禁（可编程调用版）。

    返回 dict(ok, inter_pct, mism, inter_mism, overlap_rows, figure, reason)。
    ``exclude`` 为空时自动读 manifest ``qa.exclude[figure]``
    （批次G/rL6：已知工艺残留基线随 manifest 留档，不再靠口头 --exclude）。
    """
    figure = f"{branch}_{mood}"
    rig_dir = os.path.join(REPO, "assets", "rig", stage)
    with open(os.path.join(rig_dir, "manifest.json"), "r", encoding="utf-8") as f:
        manifest = json.load(f)
    if not exclude:
        exclude = (manifest.get("qa", {}) or {}).get("exclude", {}) \
            .get(figure, "")

    orig_path = os.path.join(REPO, "assets", "ai", f"{stage}_{figure}.png")
    core_path = manifest["figures"].get(figure)
    if not core_path or not os.path.isfile(orig_path):
        return {"ok": False, "figure": f"{stage}/{figure}",
                "reason": "manifest 无此 figure（未拆件）或原图缺失"}
    orig = Image.open(orig_path).convert("RGBA")

    # M5（REVIEW-2026-09-04）：blink 豁免护栏——blink 件不参与静止/交叠
    # 门禁，无约束的 kind=blink 是双盲后门。约束：必须 over_core
    # （under_core 恒不可见=资产错误）+ px_rect 面积 ≤ 全图 2%
    # （现眼睑贴片 ~0.6%）
    for p in manifest["parts"]:
        if p.get("kind") != "blink" or p["source_figure"] != figure:
            continue
        if p.get("z") != "over_core":
            return {"ok": False, "figure": f"{stage}/{figure}",
                    "reason": f"blink 件 {p['id']} 必须 over_core（under_core 恒不可见）"}
        bxa0, bya0, bxa1, bya1 = p["px_rect"]
        if (bxa1 - bxa0) * (bya1 - bya0) > 0.02 * orig.width * orig.height:
            return {"ok": False, "figure": f"{stage}/{figure}",
                    "reason": f"blink 件 {p['id']} 面积 {(bxa1-bxa0)*(bya1-bya0)}px 超全图 2%"}

    # C3（REVIEW-2026-09-04）：exclude 格式校验——坏值直接崩门禁（ValueError
    # 抛进 evaluate），T10 常驻跑全灭
    ex = None
    if exclude:
        try:
            vals = [int(v) for v in exclude.split(",")]
            if len(vals) != 4 or any(v < 0 for v in vals):
                raise ValueError
            ex = tuple(vals)
        except ValueError:
            return {"ok": False, "figure": f"{stage}/{figure}",
                    "reason": f"exclude 格式非法 {exclude!r}（须 x0,y0,x1,y1 非负整数）"}

    core = Image.open(os.path.join(rig_dir, core_path)).convert("RGBA")

    layer = Image.new("RGBA", orig.size, (0, 0, 0, 0))
    n_parts = 0
    for p in manifest["parts"]:
        if p["source_figure"] != figure:
            continue
        if p.get("kind") == "blink":
            # 批次K：blink 是瞬态覆盖件（闭眼贴片），静止合成=睁眼原图，
            # 不参与 0° 复原比对
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
    mism = sum(hist[alpha_thr + 1:])
    opaque = sum(a_o.histogram()[1:])
    pct = mism * 100.0 / max(1, opaque)
    max_da = max(i for i, n in enumerate(hist) if n)

    # 内部失配 = 实心区（α>128 二值化后腐蚀 2px）内的失配。两点原因：
    # ① 切缘羽化在轮廓外缘留 ~1px α 环（工艺固有、显示档不可辨）→ 只计环；
    # ② 本批素材 α 带半调抖动，必须先二值化再腐蚀（直接腐蚀抖动区全灭）。
    # 真问题（缺块/错位/烤死件）必然出现在内部。
    bin_o = a_o.point(lambda v: 255 if v > 128 else 0)
    interior = bin_o.filter(ImageFilter.MinFilter(5))
    da_strong = da.point(lambda v: 255 if v > alpha_thr else 0)
    # M6（REVIEW-2026-09-04）：豁免预算——final/healthy_neutral 的 exclude
    # 恰等于尾件全 bbox（半调抖动残留散布全尾段，无窄带可收敛），尾件回归
    # （挖除错位/PNG 损坏）会被静默掩掉；被豁免的失配本身也受 manifest
    # qa.exclude_budget_px 上限约束，超预算=FAIL
    inter_full = ImageStat.Stat(da_strong, interior).sum[0] // 255
    inter_mism, excluded_mism = inter_full, 0
    if ex is not None:
        da_strong.paste(0, box=ex)
        interior.paste(0, box=ex)
        inter_mism = ImageStat.Stat(da_strong, interior).sum[0] // 255
        excluded_mism = inter_full - inter_mism
    inter_opaque = sum(interior.histogram()[1:])   # 桶值=像素数，不再除 255
    inter_pct = inter_mism * 100.0 / max(1, inter_opaque)
    budget = int((manifest.get("qa", {}) or {}).get("exclude_budget_px", {})
                 .get(figure, 0) or 0)
    budget_ok = True
    if ex is not None and budget > 0:
        budget_ok = excluded_mism <= budget
    elif ex is not None:
        print(f"⚠️ {figure} 豁免区无 exclude_budget_px 预算（建议补档防回归静默）")

    # 不透明双区（α>128）RGB 平均差（ImageStat 支持 L 掩码）
    mask = ImageChops.multiply(
        a_o.point(lambda v: 255 if v > 128 else 0),
        a_c.point(lambda v: 255 if v > 128 else 0))
    rgb_diff = ImageChops.difference(orig.convert("RGB"), comp.convert("RGB"))
    rgb_mean = ImageStat.Stat(rgb_diff, mask).mean if mask.getbbox() else [0.0]
    rgb_d = sum(rgb_mean) / len(rgb_mean)

    qa_rel = ""
    if write_qa:
        # 差异热区图（红=α 失配，暗底）
        heat = Image.new("RGBA", orig.size, (24, 24, 28, 255))
        red = Image.new("RGBA", orig.size, (255, 60, 60, 255))
        heat.paste(red, (0, 0), da.point(lambda v: 255 if v > alpha_thr else 0))

        tw, thh = orig.width // 3, orig.height // 3
        panel = Image.new("RGBA", (tw * 3 + 16, thh), (24, 24, 28, 255))
        for i, img in enumerate((orig, comp, heat)):
            t = img.resize((tw, thh))
            panel.paste(t, (i * (tw + 8), 0), t)
        qa_dir = os.path.join(REPO, "spikes", "_qa")
        os.makedirs(qa_dir, exist_ok=True)
        qa_path = os.path.join(qa_dir, f"restcomp_{stage}_{figure}.png")
        panel.save(qa_path)
        qa_rel = os.path.relpath(qa_path, REPO)

    # 批次F/C1：部件交叠门禁（静止合成对"同域拆两遍"失明，必须独立挡）
    rows = part_overlap_px(rig_dir, manifest, figure)
    overlap_ok = all(n <= _OVERLAP_MAX_PX for _a, _b, n in rows)
    ok = inter_pct <= max_pct and overlap_ok and budget_ok
    return {
        "ok": ok, "figure": f"{stage}/{figure}", "n_parts": n_parts,
        "inter_pct": inter_pct, "inter_mism": inter_mism, "mism": mism,
        "pct": pct, "max_da": max_da, "rgb_d": rgb_d, "exclude": exclude,
        "excluded_mism": excluded_mism, "exclude_budget": budget,
        "budget_ok": budget_ok,
        "overlap_rows": rows, "overlap_ok": overlap_ok, "qa": qa_rel,
        "reason": "",
    }


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
                    help="已知存量差异区 x0,y0,x1,y1（缺省自动读 manifest "
                         "qa.exclude 基线段）")
    args = ap.parse_args()

    r = evaluate(args.stage, args.branch, args.mood,
                 alpha_thr=args.alpha_thr, max_pct=args.max_pct,
                 exclude=args.exclude)
    if r["reason"]:
        print(f"❌ {r['reason']}")
        return 2
    print(f"figure={r['figure']} parts={r['n_parts']}"
          + (f"  exclude={r['exclude']}" if r["exclude"] else ""))
    print(f"全图: |Δα|>{args.alpha_thr} 失配={r['mism']} "
          f"({r['pct']:.3f}%)  maxΔα={r['max_da']}  平均ΔRGB={r['rgb_d']:.2f}")
    print(f"内部(腐蚀2px): 失配={r['inter_mism']} ({r['inter_pct']:.3f}%)"
          + (f"  豁免区失配={r['excluded_mism']}/预算{r['exclude_budget']}"
             f"{'❌超' if not r['budget_ok'] else '✅'}"
             if r["exclude"] else ""))
    print(f"部件交叠检查（≤{_OVERLAP_MAX_PX}px）：")
    for (a, b, n) in r["overlap_rows"]:
        flag = "❌" if n > _OVERLAP_MAX_PX else ("⚠️ " if n > 0 else "  ")
        print(f"  {flag}{a} × {b}: 交叠 {n}px")
    print(f"{'✅ PASS' if r['ok'] else '❌ FAIL'}（内部门禁 ≤{args.max_pct}%"
          f" + 交叠≤{_OVERLAP_MAX_PX}px）  QA 三联图={r['qa']}")
    return 0 if r["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
