"""Apply the regenerated symmetric fluke; preserve the original curved shaft."""
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]


def main():
    original = Image.open(ROOT / 'assets/rig_young/layers/tail_continuous.png').convert('RGBA')
    generated = Image.open(ROOT / 'assets/rig_young/repair_v016/tail_symmetric_generated.png').convert('RGBA')
    assert generated.size == original.size
    a, b = np.asarray(original), np.asarray(generated).copy()
    # Align both neck edges before feathering to avoid a doubled dark outline
    # where the generated neck is a few pixels wider than the original.
    for row in range(1010, 1050):
        old_x = np.flatnonzero(a[row, :, 3] > 15)
        new_x = np.flatnonzero(b[row, :, 3] > 15)
        t = min(1., (row-1010)/20)
        t = t*t*(3-2*t)
        left = new_x[0]*(1-t)+old_x[0]*t
        right = new_x[-1]*(1-t)+old_x[-1]*t
        sx = new_x[0]+(np.arange(a.shape[1])-left)*(new_x[-1]-new_x[0])/(right-left)
        values = b[row].astype(float)/255
        values[:, :3] *= values[:, 3:4]
        values = np.column_stack([np.interp(sx, np.arange(a.shape[1]), values[:, c], left=0, right=0)
                                  for c in range(4)])
        values[:, :3] /= np.maximum(values[:, 3:4], 1e-9)
        b[row] = np.clip(np.rint(values*255), 0, 255).astype('uint8')
    y, x = np.indices(a.shape[:2])
    # Replace both terminal lobes, feathering at the narrow neck only.
    # Preserve all pixels of the curved shaft and its pale underside below y=1050.
    mask = np.clip(np.minimum.reduce([(x-720)/12, (1120-x)/8,
                                     (y-740)/12, (1050-y)/20]), 0, 1)[..., None]
    af, bf = a.astype(float)/255, b.astype(float)/255
    aa, ba = af[..., 3:4], bf[..., 3:4]
    alpha = aa*(1-mask)+ba*mask
    rgb = (af[..., :3]*aa*(1-mask)+bf[..., :3]*ba*mask)/np.maximum(alpha, 1e-9)
    result = np.clip(np.rint(np.concatenate([rgb, alpha], axis=2)*255), 0, 255).astype('uint8')
    result[mask[..., 0] == 0] = a[mask[..., 0] == 0]
    Image.fromarray(result).save(ROOT / 'assets/rig_young/layers/tail_fork_symmetric_v3.png')
    diff = np.any(result != a, axis=2)
    yy, xx = np.where(diff)
    print('changed bounding box:', (xx.min(), yy.min(), xx.max()+1, yy.max()+1))
    assert np.array_equal(a[1050:], result[1050:])
    # Updated bounds must retain the previous pixels-to-rig mapping; otherwise
    # the extra lobe would shrink/reposition the whole tail during registration.
    yy, xx = np.where(result[..., 3] > 15)
    print('target_bbox_px:', [750+(xx.min()-511)*445/560,
          778+(yy.min()-790)*340/478, 750+(xx.max()+1-511)*445/560,
          778+(yy.max()+1-790)*340/478])


if __name__ == '__main__':
    main()
