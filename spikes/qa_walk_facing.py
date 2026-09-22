"""Verify tail placement, mirrored walking, gaze, and action fallback in Qt."""
from pathlib import Path
import sys
from unittest.mock import patch

import numpy as np
from PIL import Image, ImageDraw
from PySide6.QtCore import QPoint, QTimer
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pet.asset_provider import SpriteRef
from pet.rig.motion import MotionInputs
from pet.rig.presenter import RigWindow
from pet.rig.spec import load_rig_spec


def main():
    out = ROOT / 'spikes/_qa/walk_facing'
    out.mkdir(parents=True, exist_ok=True)
    app = QApplication([])
    spec = load_rig_spec(str(ROOT / 'assets/rig/young'), 'young')
    win = RigWindow(SpriteRef(path=spec.figures['healthy_neutral'], width=640, height=642), spec)
    win._motion_timer.stop()
    win.show()
    errors, frames, completed = [], {}, []

    def safe(fn):
        def run():
            try:
                fn()
            except Exception:
                import traceback
                errors.append(traceback.format_exc())
                print(errors[-1])
                win.close(); app.quit()
        return run

    def capture(name):
        im = win._quick.grabFramebuffer().convertToFormat(QImage.Format_RGBA8888)
        im.save(str(out / f'{name}.png'))
        return np.frombuffer(im.constBits(), np.uint8).reshape(im.height(), im.width(), 4).copy()

    def start():
        win.set_motion_params(walking=True, walk_hz=1.5)
        for _ in range(25):
            frame = win._engine.step(MotionInputs(walking=True, walk_hz=1.5), 33)
        win._push_frame(frame)
        win.set_facing(1)
        assert win._root.property('visualFacing') == -1
        QTimer.singleShot(160, safe(right))

    def right():
        frames['right'] = capture('right')
        win.set_facing(-1)
        # Facing is synchronous, without waiting for a motion-engine tick.
        assert win._root.property('visualFacing') == 1
        QTimer.singleShot(160, safe(left))

    def left():
        frames['left'] = capture('left')
        diff = np.abs(frames['right'].astype(float) - frames['left'][:, ::-1].astype(float))
        assert diff.mean() < 1, f'mirror mismatch: {diff.mean()}'
        # At this height the outer source-art silhouette is the whale tail.
        for name, side in [('right', 'left'), ('left', 'right')]:
            a = frames[name][..., 3]
            h, w = a.shape
            left_tail = (a[int(h*.61):int(h*.76), :int(w*.14)] > 100).sum()
            right_tail = (a[int(h*.61):int(h*.76), int(w*.86):] > 100).sum()
            assert (left_tail > right_tail) == (side == 'left')
        print(f'PASS: rightward tail=left, leftward tail=right; mirror MAE={diff.mean():.4f}')
        for d in (-1, 1):
            win.set_facing(d)
            win._engine._look_x = win._engine._look_y = 0
            with patch('PySide6.QtGui.QCursor.pos', return_value=QPoint(win.x()+win.width()+300, win.y()+250)):
                win._motion_tick()
            assert win._motion_inputs.source_facing == -1
            assert win._skinned_item._look_x * win._root.property('visualFacing') > 0
        win.set_facing(1)
        fall = SpriteRef(path=str(ROOT / 'assets/frames/young_fall_air.png'), width=640, height=642)
        win.play_frames([fall])
        assert win._root.property('sourceFacing') == 1
        assert win._root.property('visualFacing') == 1
        win.stop_frames()
        assert win._root.property('visualFacing') == -1
        print('PASS: cursor follows screen direction for both facings; legacy action and mesh restore')
        sheet = Image.new('RGB', (960, 520), '#eeeeee')
        draw = ImageDraw.Draw(sheet)
        for i, name in enumerate(('left', 'right')):
            im = Image.fromarray(frames[name]).resize((480, 482))
            sheet.paste(im, (i*480, 30), im)
            draw.text((i*480+15, 10), 'Walk '+name+'; tail '+('right' if name=='left' else 'left'), fill='black')
        sheet.save(out / 'comparison.jpg', quality=95)
        completed.append(True)
        win.close(); app.quit()

    QTimer.singleShot(250, safe(start))
    QTimer.singleShot(8000, app.quit)
    app.exec()
    assert completed and not errors, errors


if __name__ == '__main__':
    main()
