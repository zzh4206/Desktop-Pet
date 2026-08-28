#!/usr/bin/env python
"""批次F/C1 一次性重切驱动——final/neglected 与 young/neglected 腿件归属修复。

REVIEW-2026-08-28 C1：final/neglected 双腿粘连成单一连通域被拆了两遍
（leg_l 的 27114 个 α>0 像素 100% 落在 leg_r 内，paperdoll 行走四腿重影）；
young/neglected 鞋尖 bbox 渗血 723px。用 split_parts 新增的 --claim 按
两腿 pivot 中线分界 + --skip-core（核心图已按全连通域挖除过，不动）重出
四张部件图与 manifest 条目，最后复验两两交叠。

用法（仓库根）：python spikes/_qa/resplit_legs_claim.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PY = sys.executable

TARGETS = [
    # (stage, branch, claim_split)——claim_split=True：双腿粘连成单一连通域
    # （final/neglected 两腿件 bbox 几乎相同=同域拆两遍），按 pivot 中线
    # 分界各取一半；False：双腿本为独立连通域（young/neglected 的 723px
    # 交叠是旧版 bbox 直接 crop 的渗血），连通域掩模即可修复，claim 反而
    # 会误截 x 分界线一侧的真腿像素。
    ("final", "neglected", True),
    ("young", "neglected", False),
]


def find_seed(alpha, x: int, y: int) -> tuple[int, int]:
    """pivot 附近螺旋找 α>0 像素（pivot 一般就在腿上，偶落空心则近搜）。"""
    w, h = alpha.size
    px = alpha.load()
    for r in range(0, 40):
        for dx in range(-r, r + 1):
            for dy in (-r, r):
                for (cx, cy) in ((x + dx, y + dy), (x + dx, y - dy)):
                    if 0 <= cx < w and 0 <= cy < h and px[cx, cy] > 0:
                        return (cx, cy)
        for dy in range(-r + 1, r):
            for dx in (-r, r):
                for (cx, cy) in ((x + dx, y + dy), (x - dx, y + dy)):
                    if 0 <= cx < w and 0 <= cy < h and px[cx, cy] > 0:
                        return (cx, cy)
    raise SystemExit(f"pivot ({x},{y}) 附近 40px 无 α>0 像素")


def main() -> int:
    sys.path.insert(0, os.path.join(REPO, "tools"))
    from PIL import Image
    from qa_rig_composite import part_overlap_px

    for stage, branch, use_claim in TARGETS:
        rig_dir = os.path.join(REPO, "assets", "rig", stage)
        with open(os.path.join(rig_dir, "manifest.json"), encoding="utf-8") as f:
            manifest = json.load(f)
        legs = {p["id"]: p for p in manifest["parts"]
                if p["id"] in (f"leg_l_{branch}_neutral",
                               f"leg_r_{branch}_neutral")}
        assert len(legs) == 2, f"{stage}/{branch} 缺腿件: {list(legs)}"
        pl = legs[f"leg_l_{branch}_neutral"]
        pr = legs[f"leg_r_{branch}_neutral"]
        x_split = round((pl["pivot"][0] + pr["pivot"][0]) / 2)
        src = os.path.join(
            REPO, "assets", "ai", f"{stage}_{branch}_neutral.png")
        alpha = Image.open(src).convert("RGBA").getchannel("A")
        print(f"\n== {stage}/{branch} 分界 x={x_split} claim={use_claim} "
              f"(pivots {pl['pivot'][0]:.0f}/{pr['pivot'][0]:.0f}) ==")

        for part in (pl, pr):
            sway = part.get("sway", {})
            seed = find_seed(alpha, int(part["pivot"][0]),
                             int(part["pivot"][1]))
            # 屏障=该件原始 px_rect 四边——原切件正是靠屏障在髋线截断洪泛
            # （无屏障会顺连躯干：实测 642k 像素）；盒内从种子洪泛=精确
            # 复现原连通域，再走 claim/掩模归属修复（_parse_protect 只支持
            # 单字符比较，px_rect 右/下开边界用 x>N 整数等价式）
            bx0, by0, bx1, by1 = part["px_rect"]
            block = f"x<{bx0};y<{by0};x>{bx1 - 1};y>{by1 - 1}"
            cmd = [PY, os.path.join(REPO, "tools", "split_parts.py"),
                   "--stage", stage, "--branch", branch, "--mood", "neutral",
                   "--part", part["id"].rsplit("_", 2)[0],
                   "--seed", str(seed[0]), str(seed[1]),
                   "--pivot", str(part["pivot"][0]), str(part["pivot"][1]),
                   "--kind", part.get("kind", "limb"),
                   "--amp-deg", str(sway.get("amp_deg", 7)),
                   "--period-ms", str(sway.get("period_ms", 320)),
                   "--phase-ms", str(sway.get("phase_ms", 0)),
                   "--block", block,
                   "--skip-core", "--min-px", "2000"]
            if use_claim:
                cmd += ["--claim",
                        (f"x<{x_split}" if "leg_l" in part["id"]
                         else f"x>={x_split}")]
            if part.get("base_deg"):
                cmd += ["--base-deg", str(part["base_deg"])]
            print(">>", " ".join(cmd[1:]))
            r = subprocess.run(cmd, cwd=REPO)
            if r.returncode != 0:
                return r.returncode

        # 复验
        with open(os.path.join(rig_dir, "manifest.json"), encoding="utf-8") as f:
            manifest = json.load(f)
        rows = part_overlap_px(rig_dir, manifest, f"{branch}_neutral")
        worst = max((n for _, _, n in rows), default=0)
        for (a, b, n) in rows:
            print(f"   {a} × {b}: {n}px")
        status = "✅" if worst <= 256 else "❌"
        print(f"{status} {stage}/{branch} 交叠峰値 {worst}px")
        if worst > 256:
            return 1
    print("\n全部重切完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
