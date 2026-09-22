"""Widen the existing sclerae inward and move both irises 3 px inward."""
from pathlib import Path

import numpy as np
from PIL import Image
from scipy import ndimage


ROOT = Path(__file__).resolve().parents[1]
LAYERS = ROOT / "assets/rig_young/layers"


def _largest_white_component(arr: np.ndarray, roi: tuple[int, int, int, int]) -> np.ndarray:
    x0, y0, x1, y1 = roi
    rgb = arr[y0:y1, x0:x1, :3]
    alpha = arr[y0:y1, x0:x1, 3]
    white = (alpha > 20) & (rgb.min(axis=2) > 220)
    labels, count = ndimage.label(white)
    sizes = np.bincount(labels.ravel())
    sizes[0] = 0
    local = labels == sizes.argmax()
    mask = np.zeros(arr.shape[:2], dtype=bool)
    mask[y0:y1, x0:x1] = local
    return mask


def widen_sclerae() -> None:
    src = Image.open(LAYERS / "face_base_clean.png").convert("RGBA")
    arr = np.asarray(src)
    out = src.copy()
    eyes = [((410, 535, 545, 670), 3), ((635, 520, 775, 655), -3)]
    for roi, shift_x in eyes:
        mask = _largest_white_component(arr, roi)
        yy, xx = np.where(mask)
        box = (int(xx.min()), int(yy.min()), int(xx.max()+1), int(yy.max()+1))
        patch = src.crop(box)
        patch_mask = Image.fromarray(
            (mask[box[1]:box[3], box[0]:box[2]] * 255).astype("uint8")
        )
        patch.putalpha(Image.composite(
            patch.getchannel("A"), Image.new("L", patch.size), patch_mask
        ))
        width = round(patch.width * 1.04)
        patch = patch.convert("RGBa").resize(
            (width, patch.height), Image.Resampling.LANCZOS
        ).convert("RGBA")
        cx = (box[0] + box[2]) / 2 + shift_x
        out.alpha_composite(patch, (round(cx-width/2), box[1]))
        print("sclera", box, "->", (width, patch.height), "shift", shift_x)
    # The face silhouette and every non-eye pixel remain byte-identical.
    out.putalpha(src.getchannel("A"))
    out.save(LAYERS / "face_base_eye_spacing_v2.png")


def shift_irises() -> None:
    for side, dx in (("l", 3), ("r", -3)):
        src = Image.open(LAYERS / f"pupil_{side}_aligned.png").convert("RGBA")
        out = Image.new("RGBA", src.size)
        out.alpha_composite(src, (dx, 0))
        out.save(LAYERS / f"pupil_{side}_inset_v3.png")
        print("pupil", side, "shift", dx)


if __name__ == "__main__":
    widen_sclerae()
    shift_irises()
