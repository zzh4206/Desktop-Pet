"""尾尖差异定向探针——区分「缺失」（原图有复合无）与「多余」（原图无复合有），
并对照 git HEAD 版本资产判断是否本次切腿引入。"""
import json
import os
import subprocess
from PIL import Image

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
STAGE = os.path.join(REPO, "assets", "rig", "final")


def composite(figs_path, manifest):
    orig = Image.open(os.path.join(REPO, "assets", "ai",
                                   "final_healthy_neutral.png")).convert("RGBA")
    core = Image.open(figs_path).convert("RGBA")
    layer = Image.new("RGBA", orig.size, (0, 0, 0, 0))
    for p in manifest["parts"]:
        if p["source_figure"] != "healthy_neutral":
            continue
        part = Image.open(os.path.join(STAGE, p["file"])).convert("RGBA")
        layer.paste(part, (p["px_rect"][0], p["px_rect"][1]))
    return orig, Image.alpha_composite(layer, core)


def report(tag, orig, comp, region=None):
    po, pc = orig.load(), comp.load()
    missing = extra = 0
    mbox = ebox = None
    x0, y0, x1, y1 = region or (0, 0, orig.width, orig.height)
    for y in range(y0, y1):
        for x in range(x0, x1):
            ao, ac = po[x, y][3], pc[x, y][3]
            if ao > 128 and ac < 64:      # 缺失
                missing += 1
                mbox = (x, y) if mbox is None else (min(mbox[0], x), min(mbox[1], y), max(mbox[0], x), max(mbox[1], y))
            elif ao < 64 and ac > 128:    # 多余
                extra += 1
                ebox = (x, y) if ebox is None else (min(ebox[0], x), min(ebox[1], y), max(ebox[0], x), max(ebox[1], y))
    print(f"[{tag}] 缺失={missing} bbox={mbox}  多余={extra} bbox={ebox}")


manifest = json.load(open(os.path.join(STAGE, "manifest.json"), encoding="utf-8"))
orig, comp = composite(os.path.join(STAGE, "figs", "healthy_neutral.png"), manifest)
report("工作区(现状)", orig, comp, region=(700, 550, 1024, 1150))
report("工作区(现状)腿区", orig, comp, region=(400, 1250, 620, 1536))

# git HEAD 版本（切腿前）
for name, side in (("figs/healthy_neutral.png", "figs"),):
    blob = subprocess.run(
        ["git", "-C", REPO, "show", f"HEAD:assets/rig/final/{name}"],
        capture_output=True).stdout
    tmp = os.path.join(REPO, "spikes", "_qa", "_git_figs.png")
    open(tmp, "wb").write(blob)
    mani_blob = subprocess.run(
        ["git", "-C", REPO, "show", "HEAD:assets/rig/final/manifest.json"],
        capture_output=True).stdout
    old_manifest = json.loads(mani_blob)
    orig2, comp2 = composite(tmp, old_manifest)
    report("git HEAD(切腿前) 尾区", orig2, comp2, region=(700, 550, 1024, 1150))
