"""v0.10 立绘后处理（mac 版）—— 832×1216 白底 RGBA → 入仓透明成品。

⚠️ 已废弃（REVIEW-2026-08-28 T6，勿再直接运行）：
  · rembg(u2net) 去背管线已被云端 BiRefNet/ToonOut 抠图取代（见
    《本地LoRA产线方案》与 ai-art-pipeline 记录）；
  · STAGE_SIZE 64/96/128 是 v0.10.12 之前的旧档——现行显示档为
    young 192 / adult 256 / final 320（pet/asset_provider._STAGE_SIZE），
    今天重跑会把成品缩到旧尺寸=放大 3 倍发糊（当年用户投诉的回归）；
  · DST 是 mac 专用路径。留档仅作历史工艺参考；如需复刻流程，参照
    tools/split_parts.py + 云端产线重写。

忠实复刻 win 端 `pet_v2_postprocess.py` + `pet_v2_gap_fix.py`（交接文档§四）：
rembg(u2net) 去背 → 连通背景白填充(两轮 flood) → 1px alpha 腐蚀 →
alpha bbox 裁切 → gap_fix(腿间/袜间灰白净化) → 长边 LANCZOS 缩到
stage 尺寸 → 方形画布底对齐粘贴 → assets/ai/。

构建期工具，app 运行期不 import（只读成品 PNG）。依赖：rembg/onnxruntime/
numpy/scipy/Pillow，装在 .venv（不入 runtime requirements）。
"""
from __future__ import annotations

import os
import sys

import numpy as np
from PIL import Image, ImageFilter
from scipy import ndimage
from rembg import new_session, remove

STAGE_SIZE = {"young": 64, "adult": 96, "final": 128}  # 对齐 asset_provider._STAGE_SIZE
SRC = "/tmp/pet_source_unzip/source"
DST = os.path.expanduser("~/Desktop_Pet/assets/ai")


def _whiteish(rgb: np.ndarray, lo: int = 245, sat_max: int = 10) -> np.ndarray:
    """白底候选：三通道均 ≥lo 且低饱和(max-min≤sat_max)。"""
    mn = rgb.min(axis=-1)
    mx = rgb.max(axis=-1)
    return (mn >= lo) & ((mx - mn) <= sat_max)


def _grayish(rgb: np.ndarray, alpha: np.ndarray,
             lo: int = 210, spread_max: int = 20) -> np.ndarray:
    """gap_fix 目标：低饱和灰白(min≥lo & 色差≤spread_max & alpha>128)。"""
    mn = rgb.min(axis=-1)
    mx = rgb.max(axis=-1)
    return (mn >= lo) & ((mx - mn) <= spread_max) & (alpha > 128)


def _clear_border_components(clear_mask: np.ndarray, keep_alpha: np.ndarray,
                              conn: int = 1) -> np.ndarray:
    """clear_mask 中连通到图像边界的组件 → 标记清除。

    返回 boolean：True=该像素应置透明。conn: 1=4连通 2=8连通。
    """
    h, w = clear_mask.shape
    labels, n = ndimage.label(clear_mask, structure=np.ones((3, 3)) if conn == 2
                             else ndimage.generate_binary_structure(2, 1))
    if n == 0:
        return np.zeros_like(clear_mask, dtype=bool)
    border = set()
    border.update(labels[0, :].tolist())
    border.update(labels[-1, :].tolist())
    border.update(labels[:, 0].tolist())
    border.update(labels[:, -1].tolist())
    border.discard(0)
    return np.isin(labels, list(border))


def _flood_white_fill(rgb: np.ndarray, alpha: np.ndarray) -> np.ndarray:
    """两轮连通白填充：轮1 边界白组件；轮2 邻透明白块。

    rembg 漏判的白底（仍 alpha>0）按连通性清掉。"""
    out = alpha.copy()
    white = _whiteish(rgb) & (alpha > 0)
    # 轮1：边界连通白
    border_clear = _clear_border_components(white, alpha, conn=2)
    out[border_clear] = 0
    # 轮2：剩余白组件若邻接已透明区 → 清（处理被透明包围的白块，如腿间开口）
    white2 = _whiteish(rgb) & (out > 0)
    # 邻透明：对 white2 组件，若其 8 邻域含 alpha==0 像素 → 清
    labels, n = ndimage.label(white2, structure=np.ones((3, 3)))
    transp = out == 0
    # 膨胀透明区一像素，与组件求交
    transp_dil = ndimage.binary_dilation(transp, structure=np.ones((3, 3)))
    for li in range(1, n + 1):
        comp = labels == li
        if (comp & transp_dil).any():
            out[comp] = 0
    return out


def _gap_fix(rgb: np.ndarray, alpha: np.ndarray) -> np.ndarray:
    """腿间/袜间灰白残留净化：bbox 下部 35% × 水平 28-72% 带内【完整包含】
    的低饱和灰白连通组件(min≥210 & 色差≤20 & alpha>128)置透明。

    几何窗口法（交接文档§四）——flood 不可达的夹缝（阴影断链）用窗口兜底。"""
    out = alpha.copy()
    gray = _grayish(rgb, alpha)
    if not gray.any():
        return out
    labels, n = ndimage.label(gray, structure=np.ones((3, 3)))
    if n == 0:
        return out
    h, w = gray.shape
    y0 = int(h * 0.65)            # 下部 35%
    x0 = int(w * 0.28); x1 = int(w * 0.72)
    for li in range(1, n + 1):
        comp = labels == li
        ys, xs = np.where(comp)
        # 完整包含于窗口
        if (ys.min() >= y0 and xs.min() >= x0 and xs.max() <= x1
                and ys.max() < h):
            out[comp] = 0
    return out


def process(name: str, session) -> Image.Image:
    src = os.path.join(SRC, name + ".png")
    im = Image.open(src).convert("RGBA")
    out_im = remove(im, session=session)   # 1. rembg u2net 去背
    arr = np.array(out_im)
    rgb = arr[..., :3]
    alpha = arr[..., 3].astype(np.int16)

    alpha = _flood_white_fill(rgb, alpha)              # 2. 两轮连通白填充
    alpha = _gap_fix(rgb, alpha)                       # 3. gap_fix 灰白净化

    # 4. 1px alpha 腐蚀（MinFilter，防白边）
    a_img = Image.fromarray(alpha.astype(np.uint8))
    a_img = a_img.filter(ImageFilter.MinFilter(3))
    alpha = np.array(a_img, dtype=np.int16)

    # 5. alpha bbox 裁切
    ys, xs = np.where(alpha > 0)
    if len(ys) == 0:
        raise RuntimeError(f"{name}: 全空（去背后无前景）")
    arr = arr[ys.min():ys.max() + 1, xs.min():xs.max() + 1].copy()
    alpha = alpha[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
    arr[..., 3] = alpha.astype(np.uint8)

    # 6. 长边 LANCZOS 缩到 stage 尺寸
    stage = name.split("_")[0]   # young/adult/final
    target = STAGE_SIZE[stage]
    h, w = arr.shape[:2]
    longest = max(h, w)
    scale = target / longest
    nw, nh = max(1, int(round(w * scale))), max(1, int(round(h * scale)))
    im2 = Image.fromarray(arr, mode="RGBA").resize((nw, nh), Image.LANCZOS)

    # 7. 方形画布底对齐（bottom_center 锚点：脚底贴下沿、水平居中）
    canvas = Image.new("RGBA", (target, target), (0, 0, 0, 0))
    px = (target - nw) // 2
    py = target - nh    # 底对齐
    canvas.alpha_composite(im2, (px, py))

    # 8. 二次 gap_fix（成品级）：缩放后子阈值灰白经 LANCZOS 均化升为
    # 灰ish 全不透明残留，在成品画布上再扫一遍窗口清掉（交接文档§四
    # "必须先于入仓跑"——win 端在成品上跑 gap_fix 的等价）。
    c_arr = np.array(canvas)
    c_rgb = c_arr[..., :3]
    c_alpha = c_arr[..., 3].astype(np.int16)
    c_alpha = _gap_fix(c_rgb, c_alpha)
    c_arr[..., 3] = c_alpha.astype(np.uint8)
    return Image.fromarray(c_arr, mode="RGBA")


def main():
    # 批次C/P3-20（REVIEW-2026-09-05）：global 声明前置——旧版 global 在
    # SRC/DST 使用之后，SyntaxError 文件不可运行
    global SRC, DST
    import sys
    src = sys.argv[1] if len(sys.argv) > 1 else SRC
    dst = sys.argv[2] if len(sys.argv) > 2 else DST
    SRC = src; DST = dst
    os.makedirs(DST, exist_ok=True)
    session = new_session("u2net")
    names = sorted(f[:-4] for f in os.listdir(SRC) if f.endswith(".png"))
    print(f"处理 {len(names)} 张 → {DST}")
    for i, name in enumerate(names, 1):
        img = process(name, session)
        img.save(os.path.join(DST, name + ".png"))
        print(f"  [{i}/{len(names)}] {name} {img.size}")
    print("done")


if __name__ == "__main__":
    main()
