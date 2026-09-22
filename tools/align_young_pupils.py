"""Resample the existing irises into balanced eye openings; keep originals intact."""
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1] / 'assets/rig_young/layers'
# Retain the reference's 14 px eye-height difference and slight width difference.
TARGETS = {'pupil_l': (436, 559, 514, 649), 'pupil_r': (663, 545, 743, 635)}


def main():
    for name, (x0, y0, x1, y1) in TARGETS.items():
        original = Image.open(ROOT / f'{name}.png').convert('RGBA')
        bx0, by0, bx1, by1 = original.getchannel('A').point(lambda a: 255 if a > 20 else 0).getbbox()
        sx, sy = (x1-x0)/(bx1-bx0), (y1-y0)/(by1-by0)
        # Premultiplied alpha avoids dark/color fringes when filtering the cutout.
        aligned = original.convert('RGBa').transform(
            original.size, Image.Transform.AFFINE,
            (1/sx, 0, bx0-x0/sx, 0, 1/sy, by0-y0/sy),
            resample=Image.Resampling.BICUBIC,
        ).convert('RGBA')
        aligned.save(ROOT / f'{name}_aligned.png')
        print(name, 'scale', round(sx, 4), round(sy, 4), 'target', (x0, y0, x1, y1))


if __name__ == '__main__':
    main()
