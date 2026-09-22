"""Apply the locally generated Qwen eyebrow repair without repainting the face.

Input: 416x448 edit of core_inpaint_v1/face_base.png crop (384,304,800,752).
Only the two brow regions are blended. Original alpha and all other pixels stay
byte-identical. Run with the accepted Qwen crop path as the sole argument.
"""
from pathlib import Path
import sys

import numpy as np
from PIL import Image, ImageFilter

ROOT = Path(__file__).resolve().parents[1]


def patch(generated: Path) -> Path:
    base = Image.open(ROOT / 'assets/rig_young/core_inpaint_v1/face_base.png').convert('RGBA')
    edit = Image.open(generated).convert('RGBA')
    if edit.size != (416, 448):
        raise ValueError('Qwen crop must retain the 416x448 source framing')
    source = np.asarray(base).copy()
    result = source.copy()
    for x0, y0, x1, y1 in [(460, 510, 550, 558), (633, 507, 696, 563)]:
        mask = Image.new('L', (x1 - x0, y1 - y0))
        mask.paste(255, (5, 5, mask.width - 5, mask.height - 5))
        mask = np.asarray(mask.filter(ImageFilter.GaussianBlur(2)), dtype=float) / 255
        old = source[y0:y1, x0:x1, :3]
        new = np.asarray(edit)[y0-304:y1-304, x0-384:x1-384, :3]
        # Eye whites must not change even if the generative edit shifted them.
        mask[np.min(old, axis=2) > 240] = 0
        result[y0:y1, x0:x1, :3] = np.rint(old * (1-mask[..., None]) + new * mask[..., None])
    out = ROOT / 'assets/rig_young/layers/face_base_clean.png'
    Image.fromarray(result).save(out)
    assert np.array_equal(result[:, :, 3], source[:, :, 3])
    return out


if __name__ == '__main__':
    print(patch(Path(sys.argv[1])))
