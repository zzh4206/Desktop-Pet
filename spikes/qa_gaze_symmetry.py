"""Measure iris shape/clipping and capture nine gaze directions without asset edits."""
from pathlib import Path
import json
import sys
import numpy as np
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTimer
from pet.asset_provider import SpriteRef
from pet.rig.presenter import RigWindow
from pet.rig.spec import load_rig_spec

OUT = ROOT / 'spikes/_qa/gaze_symmetry'
DIRECTIONS = [(x, y) for y in [-1, 0, 1] for x in [-1, 0, 1]]


def measure():
    spec = json.loads((ROOT / 'assets/rig_young/spec.json').read_text(encoding='utf8'))
    results = {}
    for layer in spec['layers']:
        if layer['id'] not in ('pupil_l', 'pupil_r'):
            continue
        im = Image.open(ROOT / 'assets/rig_young/layers' / layer.get('texture', layer['id'] + '.png'))
        alpha = np.asarray(im)[..., 3]
        yy, xx = np.indices(alpha.shape)
        cx, cy, rx, ry = layer['gaze_ellipse']
        full = alpha > 20
        sy, sx = np.nonzero(full)
        row = {'source_bbox': [int(sx.min()), int(sy.min()), int(sx.max()+1), int(sy.max()+1)],
               'source_centroid': [float(sx.mean()), float(sy.mean())],
               'clip_ellipse': [cx, cy, rx, ry], 'gaze': {}}
        for name, dx, dy in [('neutral', 0, 0), ('left', -10, 0), ('right', 10, 0),
                             ('up', 0, -7), ('down', 0, 7)]:
            # Test translated texture pixels against the fixed eye-opening ellipse.
            inside = ((sx+dx-cx)/rx)**2 + ((sy+dy-cy)/ry)**2 <= 1
            row['gaze'][name] = {'clipped_percent': round(100*(1-inside.mean()), 2),
                                 'visible_centroid': [round(float((sx+dx)[inside].mean()), 3),
                                                      round(float((sy+dy)[inside].mean()), 3)]}
        results[layer['id']] = row
    (OUT / 'measurements.json').write_text(json.dumps(results, indent=2), encoding='utf8')
    print(json.dumps(results, indent=2), flush=True)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    measure()
    app = QApplication([])
    spec = load_rig_spec(str(ROOT / 'assets/rig/young'), 'young')
    win = RigWindow(SpriteRef(path=spec.figures['healthy_neutral'], width=640, height=642), spec)
    win._motion_timer.stop()
    win.show()
    frames = []
    errors = []
    index = [0]

    def pose():
        try:
            assert win.skinned_motion_active()
            x, y = DIRECTIONS[index[0]]
            win._skinned_item.setLookAt(x, y)
            QTimer.singleShot(120, capture)
        except Exception as exc:
            errors.append(str(exc)); app.quit()

    def capture():
        try:
            i = index[0]
            path = OUT / f'gaze_{i}.png'
            win._quick.grabFramebuffer().save(str(path))
            im = Image.open(path).convert('RGBA')
            scale = im.width / 1280
            crop = im.crop(tuple(int(v*scale) for v in (390, 505, 795, 670)))
            crop = crop.resize((486, 198))
            panel = Image.new('RGB', (506, 234), '#eeeeee')
            panel.paste(crop, (10, 28), crop)
            ImageDraw.Draw(panel).text((10, 8), f'gaze x={DIRECTIONS[i][0]}, y={DIRECTIONS[i][1]}', fill='black')
            frames.append(panel)
            index[0] += 1
            if index[0] < len(DIRECTIONS):
                pose()
            else:
                sheet = Image.new('RGB', (1518, 702), 'white')
                for j, frame in enumerate(frames):
                    sheet.paste(frame, ((j%3)*506, (j//3)*234))
                sheet.save(OUT / 'gaze_grid.jpg', quality=95)
                win.close(); app.quit()
        except Exception as exc:
            errors.append(str(exc)); app.quit()

    QTimer.singleShot(250, pose)
    QTimer.singleShot(10000, app.quit)
    app.exec()
    assert not errors and len(frames) == 9, errors


if __name__ == '__main__':
    main()
