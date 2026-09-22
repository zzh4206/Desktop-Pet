"""Minimally balance the viewer-left hand and shoe using existing pixels."""
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter
from scipy import ndimage


ROOT = Path(__file__).resolve().parents[1]
LAYERS = ROOT / "assets/rig_young/layers"


def _premultiplied_resize(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    return image.convert("RGBa").resize(
        size, Image.Resampling.LANCZOS
    ).convert("RGBA")


def enlarge_left_hand() -> None:
    src = Image.open(LAYERS / "arm_l_cuff_v2.png").convert("RGBA")
    arr = np.asarray(src)
    rgb = arr[..., :3].astype(np.int16)
    alpha = arr[..., 3]
    skin = ((alpha > 20) & (rgb[..., 0] > 180)
            & (rgb[..., 0] > rgb[..., 1] + 10)
            & (rgb[..., 0] > rgb[..., 2] + 10))
    labels, count = ndimage.label(skin)
    sizes = np.bincount(labels.ravel())
    sizes[0] = 0
    hand = labels == sizes.argmax()
    # Include the hand outline but stop before taking over the surrounding cuff.
    hand = ndimage.binary_dilation(hand, iterations=3) & (alpha > 0)
    yy, xx = np.where(hand)
    box = (int(xx.min()), int(yy.min()), int(xx.max()+1), int(yy.max()+1))
    patch = src.crop(box)
    mask = Image.fromarray((hand[box[1]:box[3], box[0]:box[2]] * 255).astype("uint8"))
    patch.putalpha(Image.composite(patch.getchannel("A"), Image.new("L", patch.size), mask))
    new_size = (round(patch.width * 1.28), round(patch.height * 1.16))
    patch = _premultiplied_resize(patch, new_size)
    cx, cy = (box[0]+box[2])/2, (box[1]+box[3])/2
    pos = (round(cx-patch.width/2), round(cy-patch.height/2))
    out = src.copy()
    out.alpha_composite(patch, pos)
    out.save(LAYERS / "arm_l_balanced_v3.png")
    print("hand", box, "->", new_size, "at", pos)


def enlarge_and_brighten_left_shoe() -> None:
    src = Image.open(LAYERS / "leg_l.png").convert("RGBA")
    arr = np.asarray(src)
    alpha = arr[..., 3]
    rows = np.indices(alpha.shape)[0]
    shoe = (alpha > 0) & (rows >= 1080)
    yy, xx = np.where(shoe)
    box = (int(xx.min()), int(yy.min()), int(xx.max()+1), int(yy.max()+1))
    patch = src.crop(box)
    mask = Image.fromarray((shoe[box[1]:box[3], box[0]:box[2]] * 255).astype("uint8"))
    patch.putalpha(Image.composite(patch.getchannel("A"), Image.new("L", patch.size), mask))

    rgba = np.asarray(patch).copy()
    rgb = rgba[..., :3]
    mean = rgb.mean(axis=2)
    navy = ((rgba[..., 3] > 8) & (mean > 30) & (mean < 160)
            & (rgb[..., 2].astype(float) > rgb[..., 0] * 1.05))
    rgb[navy] = np.clip(rgb[navy].astype(float) * 1.15, 0, 255).astype("uint8")
    patch = Image.fromarray(rgba, "RGBA")

    new_size = (round(patch.width * 1.04), round(patch.height * 1.04))
    patch = _premultiplied_resize(patch, new_size)
    # Keep the sole on the existing ground line and scale around its center.
    cx = (box[0]+box[2])/2
    pos = (round(cx-patch.width/2), box[3]-patch.height)
    out = src.copy()
    out.alpha_composite(patch, pos)
    out.save(LAYERS / "leg_l_balanced_v2.png")
    print("shoe", box, "->", new_size, "at", pos)


if __name__ == "__main__":
    enlarge_left_hand()
    enlarge_and_brighten_left_shoe()
