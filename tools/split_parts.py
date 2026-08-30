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

from PIL import Image, ImageChops, ImageDraw, ImageFilter

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


def _parse_claim(expr: str):
    """批次F/C1：归属分界式解析——x<N / x<=N / x>N / x>=N（单条）。"""
    e = (expr or "").replace(" ", "")
    for op in ("<=", ">=", "<", ">"):
        if e.startswith("x" + op):
            try:
                return (op, float(e[1 + len(op):]))
            except ValueError:
                break
    raise SystemExit(f"--claim 无法解析: {expr!r}（支持 x<N / x<=N / x>N / x>=N）")


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
    ap.add_argument("--kind", choices=["sway", "limb"], default="sway",
                    help="sway=常驻正弦摆（尾/呆毛）；limb=行走驱动肢体（v0.14，"
                         "引擎按 kind 分驱动，旧引擎视 limb 为 sway 兼容）")
    ap.add_argument("--base-deg", type=float, default=0.0,
                    help="limb 单侧摆偏置：摆动范围 [base, base+2·amp]，"
                         "用于裙摆遮挡不对称时把摆动锁在安全方向")
    ap.add_argument("--claim", default="",
                    help="批次F/C1（REVIEW-2026-08-28）：归属分界比较式"
                         "（x<N 或 x>N，单条）——连通域先按此过滤再切件。"
                         "双腿粘连成单一连通域时，leg_l 用 x<N、leg_r 用"
                         " x≥N 各取一半，根除\"同一双腿拆两遍、paperdoll"
                         " 行走四腿重影\"的资产事故")
    ap.add_argument("--skip-core", action="store_true",
                    help="只重出部件图+manifest，不动核心图（核心已按全"
                         "连通域挖除过的重切场景；保护带语义保持原样）")
    ap.add_argument("--qa-swing", type=float, default=0.0, metavar="DEG",
                    help=">0 时另出摆角条带 QA 图：部件绕 pivot 转 "
                         "[-DEG,-DEG/2,0,DEG/2,DEG] 叠核心图合成（预检接缝/出界）")
    args = ap.parse_args()

    src_rel = f"assets/ai/{args.stage}_{args.branch}_{args.mood}.png"
    src_path = os.path.join(REPO, src_rel)
    im = Image.open(src_path).convert("RGBA")
    alpha = im.getchannel("A")

    barriers = _parse_protect(args.block)
    comp = extract_component(alpha, (args.seed[0], args.seed[1]), barriers)
    print(f"连通域像素数={len(comp)}")
    if args.claim:
        # 批次F/C1：归属过滤在 min/max 检查前——粘连连通域按 x 分界
        # 各取一半（leg_l x<N / leg_r x>=N），根除"同一双腿拆两遍"
        op, v = _parse_claim(args.claim)
        before = len(comp)
        comp = {(x, y) for (x, y) in comp
                if ((x < v) if op == "<" else
                    (x <= v) if op == "<=" else
                    (x > v) if op == ">" else (x >= v))}
        print(f"claim x{op}{v:g}: {before} → {len(comp)} 像素")
        if not comp:
            print("❌ claim 过滤后为空——分界线可能不在连通域范围内",
                  file=sys.stderr)
            return 2
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

    # 批次F/C1（REVIEW-2026-08-28）：部件按连通域掩模生成——旧版 bbox
    # 直接 crop，bbox 内的非本件像素（claim 另一半腿/邻近构件）一并被
    # 带入部件图，是"final/neglected 双腿件 100% 互含"事故的直接放大器。
    # 掩模取全 comp（含保护带像素，与旧语义一致：部件层多带无害）。
    mask = Image.new("L", im.size, 0)
    mload = mask.load()
    for (x, y) in comp:
        mload[x, y] = 255
    part = im.crop((bx0, by0, bx1, by1))
    part.putalpha(ImageChops.multiply(
        part.getchannel("A"), mask.crop((bx0, by0, bx1, by1))))
    # 轻羽化切缘（仅软化 polygon 挖除直线感；外轮廓本就带原图抗锯齿边）
    a = part.getchannel("A").filter(ImageFilter.GaussianBlur(0.7))
    part.putalpha(a)

    figure_key = f"{args.branch}_{args.mood}"
    out_dir = os.path.join(REPO, "assets", "rig", args.stage)
    parts_dir = os.path.join(out_dir, "parts")
    figs_dir = os.path.join(out_dir, "figs")
    os.makedirs(parts_dir, exist_ok=True)
    os.makedirs(figs_dir, exist_ok=True)
    part_path = os.path.join(parts_dir, f"{args.part}_{figure_key}.png")
    fig_path = os.path.join(figs_dir, f"{figure_key}.png")

    core = None
    if args.skip_core:
        # 批次F/C1：只重出部件+manifest——核心图已按全连通域挖除过的
        # 重切场景（claim 重切），保护带语义保持原样不动
        if os.path.isfile(fig_path):
            core = Image.open(fig_path).convert("RGBA")  # 仅供 QA 对照
    else:
        core = im.copy()
        # 多部件累计挖除：同 figure 已有派生核心图（先前切件产物）则在其上
        # 继续挖，避免本件覆盖丢掉先前件的挖除（核心图残留烤死件=摆动双影）。
        # 从头重切需先删 figs/{figure}.png。
        if os.path.isfile(fig_path):
            core = Image.open(fig_path).convert("RGBA")
            if core.size != im.size:
                raise SystemExit(f"已有核心图尺寸 {core.size} ≠ 源图 {im.size}，"
                                 f"请删除 {fig_path} 重切")
        cpx = core.load()
        for (x, y) in cutting:
            r, g, b, _a = cpx[x, y]
            cpx[x, y] = (r, g, b, 0)
        core.save(fig_path)

    part.save(part_path)

    manifest_path = os.path.join(out_dir, "manifest.json")
    manifest = {}
    if os.path.isfile(manifest_path):
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
    figures = manifest.setdefault("figures", {})
    # 批次F/rM2（REVIEW-2026-08-28）：源码 "\\\\"=运行时两个反斜杠，
    # relpath 产出单反斜杠 → 替换永不命中，manifest 混入 "figs\\x.png"。
    # Windows 靠 normpath 兜住；非 Windows 端（mac 产线工具）静默弃件。
    rel_fig = os.path.relpath(fig_path, out_dir).replace("\\", "/")
    figures[figure_key] = rel_fig           # 派生核心图取代整图入口

    part_id = f"{args.part}_{figure_key}"
    parts = [p for p in manifest.setdefault("parts", [])
             if p.get("id") != part_id]     # 同部件重复运行覆盖（顺清陈档）
    parts.append({
        "id": part_id,
        "file": os.path.relpath(part_path, out_dir).replace("\\", "/"),
        "source_figure": figure_key,
        "px_rect": [bx0, by0, bx1, by1],
        "pivot": list(args.pivot),
        "z": "under_core",
        "kind": args.kind,
        "sway": {"amp_deg": args.amp_deg, "period_ms": args.period_ms,
                 "phase_ms": args.phase_ms},
        # 批次G/rL5（REVIEW-2026-08-31）：拆件参数留痕——重切不再依赖
        # 逆向工程（上轮 C1 重切即靠逆向重建屏障踩坑）。运行期不消费
        "split": {k: v for k, v in {
            "seed": [args.seed[0], args.seed[1]],
            "block": args.block or None,
            "protect": args.protect or None,
            "claim": args.claim or None,
            "min_px": args.min_px,
            "max_px": args.max_px,
        }.items() if v is not None},
    })
    manifest["spec"] = 1
    manifest["parts"] = parts
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    # ---- QA 对照图 ----
    qa_dir = os.path.join(REPO, "spikes", "_qa")
    os.makedirs(qa_dir, exist_ok=True)
    # 批次G/rL2（REVIEW-2026-08-31）：面板宽旧版 w*2//3*3 放不下三张
    # 1/3 缩略图（第三张被截）；pivot 连线旧版拿全分辨率坐标画在 1/3
    # 缩略图上（线飞出面板）。按缩略图坐标系缩放绘制
    tw3, th3 = im.width // 3, im.height // 3
    panel = Image.new("RGBA", (tw3 * 3 + 16, th3), (24, 24, 28, 255))
    th = im.resize((tw3, th3))
    # skip-core 且无既有核心图时 core=None——QA 对照以原图代位
    co_src = core if core is not None else im
    co = co_src.resize((tw3, th3))
    pt = part.resize((max(1, (bx1 - bx0) // 3), max(1, (by1 - by0) // 3)))
    panel.paste(th, (0, 0), th)
    panel.paste(co, (tw3 + 8, 0), co)
    panel.paste(pt, ((tw3 + 8) * 2, 0), pt)
    d = ImageDraw.Draw(panel)
    d.line([(args.pivot[0] / 3 + (tw3 + 8) * 2, args.pivot[1] / 3),
            ((bx0) / 3 + (tw3 + 8) * 2, (by0) / 3)],
           fill=(255, 80, 80, 255), width=2)
    qa_path = os.path.join(
        qa_dir, f"split_{args.stage}_{figure_key}_{args.part}.png")
    panel.save(qa_path)

    # ---- QA 摆角条带（--qa-swing DEG）----
    # 部件层全画布贴回 bbox 原位 → 绕 pivot 旋转 → 核心图压在其上
    # （under_core 语义），五档角度横排——上引擎前人工预检接缝与出界。
    if args.qa_swing > 0 and core is not None:
        a_max = args.qa_swing
        angles = [-a_max, -a_max / 2, 0.0, a_max / 2, a_max]
        th_w, th_h = im.width // 3, im.height // 3
        strip = Image.new("RGBA", (th_w * len(angles) + 8 * (len(angles) - 1),
                                   th_h), (24, 24, 28, 255))
        for i, ang in enumerate(angles):
            layer = Image.new("RGBA", im.size, (0, 0, 0, 0))
            layer.paste(part, (bx0, by0))
            layer = layer.rotate(ang, resample=Image.BICUBIC,
                                 center=tuple(args.pivot))
            comp = Image.alpha_composite(layer, core)
            th = comp.resize((th_w, th_h))
            strip.paste(th, (i * (th_w + 8), 0), th)
        d = ImageDraw.Draw(strip)
        d.text((4, 4), f"{args.part} swing ±{a_max}deg", fill=(255, 255, 255, 255))
        swing_path = os.path.join(
            qa_dir, f"swing_{args.stage}_{figure_key}_{args.part}.png")
        strip.save(swing_path)
        print(f"   QA 摆角条带={os.path.relpath(swing_path, REPO)}")
    elif args.qa_swing > 0:
        # 批次G/rL4：skip-core 且核心图不在 → 无合成对象，跳过摆角条带
        print("⚠️ --skip-core 且无既有核心图，--qa-swing 摆角条带跳过",
              file=sys.stderr)

    print(f"✅ 部件={os.path.relpath(part_path, REPO)}\n"
          f"   派生核心={os.path.relpath(fig_path, REPO)}\n"
          f"   manifest 已合并更新（figures[{figure_key}]→figs）\n"
          f"   QA 对照={os.path.relpath(qa_path, REPO)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
