"""Patch the missing viewer-left navy/gold cuff from the existing right cuff.

The source arm layers remain intact. Only dark/gold cuff pixels are mirrored,
slightly rotated, and composited onto a versioned left-arm texture.
"""
from pathlib import Path

import numpy as np
from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
LAYERS = ROOT / "assets/rig_young/layers"


def main() -> None:
    left = Image.open(LAYERS / "arm_l.png").convert("RGBA")
    right = Image.open(LAYERS / "arm_r.png").convert("RGBA")

    # The donor rectangle contains the viewer-right cuff. Select only its
    # navy outline/fabric and gold piping, leaving the hand and white sleeve.
    donor_box = (728, 785, 790, 884)
    donor = right.crop(donor_box)
    rgba = np.asarray(donor).copy()
    rgb = rgba[..., :3].astype(np.int16)
    alpha = rgba[..., 3]
    dark = (rgb.max(axis=2) < 155) & (rgb[..., 2] > rgb[..., 0] * 0.72)
    gold = (rgb[..., 0] > 130) & (rgb[..., 0] > rgb[..., 2] * 1.18)
    keep = (alpha > 8) & (dark | gold)
    rgba[..., 3] = np.where(keep, alpha, 0).astype(np.uint8)
    cuff = Image.fromarray(rgba, "RGBA").transpose(Image.Transpose.FLIP_LEFT_RIGHT)

    # Match the slightly steeper viewer-left forearm without disturbing the
    # original palm or white sleeve pixels outside this small cuff patch.
    cuff = cuff.rotate(-4.0, resample=Image.Resampling.BICUBIC,
                       expand=True, center=(cuff.width / 2, cuff.height / 2))
    # Clean interpolation specks while retaining antialiased edges.
    a = cuff.getchannel("A")
    a = a.point(lambda v: 0 if v < 5 else v)
    cuff.putalpha(a)

    overlay = Image.new("RGBA", left.size)
    overlay.alpha_composite(cuff, (407, 786))
    over = np.asarray(overlay).copy()
    base = np.asarray(left)
    # Preserve the exact arm silhouette and the original skin pixels: the cuff
    # sits behind the palm and only recolors the already-existing white sleeve.
    skin = ((base[..., 0] > 180) & (base[..., 1] > 115)
            & (base[..., 2] > 105) & (base[..., 3] > 40)
            & (base[..., 0] > base[..., 1] + 10)
            & (base[..., 0] > base[..., 2] + 10))
    over[..., 3] = np.minimum(over[..., 3], base[..., 3])
    over[..., 3][skin] = 0
    output = left.copy()
    output.alpha_composite(Image.fromarray(over, "RGBA"))
    output.putalpha(left.getchannel("A"))
    output.save(LAYERS / "arm_l_cuff_v2.png")
    print("saved", LAYERS / "arm_l_cuff_v2.png")


if __name__ == "__main__":
    main()
