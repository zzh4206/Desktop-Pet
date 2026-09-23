"""Capture the production Qt Quick mesh on the requested rendering backend.

python -X utf8 spikes/qa_skinned_visual.py --backend d3d11
python -X utf8 spikes/qa_skinned_visual.py --backend metal
python -X utf8 spikes/qa_skinned_visual.py --backend software

缺省按平台自动选：Windows=d3d11、macOS=metal、其余=gl。
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _default_backend() -> str:
    if sys.platform == "win32":
        return "d3d11"
    if sys.platform == "darwin":
        return "metal"
    return "gl"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--backend', default='')
    parser.add_argument('--output', default=str(ROOT / 'spikes/_qa/v016_review'))
    args = parser.parse_args()
    backend = args.backend or _default_backend()
    software = backend == 'software'
    if software:
        os.environ['QT_QPA_PLATFORM'] = 'offscreen'
        os.environ['QT_QUICK_BACKEND'] = 'software'
    else:
        # 硬件后端：走平台原生 QPA（cocoa/windows/xcb），不硬编码 windows
        os.environ.pop('QT_QPA_PLATFORM', None)
        os.environ['QT_QUICK_BACKEND'] = ''
    os.environ['QSG_RHI_BACKEND'] = backend
    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import QTimer
    from PySide6.QtGui import QImage
    import numpy as np
    from pet.asset_provider import SpriteRef
    from pet.rig.presenter import RigWindow
    from pet.rig.spec import load_rig_spec
    from pet.rig.motion import MotionInputs

    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    app = QApplication([])
    spec = load_rig_spec(str(ROOT / 'assets/rig/young'), 'young')
    sprite = SpriteRef(path=spec.figures['healthy_neutral'], width=640, height=642)
    win = RigWindow(sprite, spec)
    win._motion_timer.stop()
    win.setWindowTitle('Desktop Pet visual QA')
    win.show()
    errors = []
    images = {}
    animation = []
    frame_index = [0]

    def capture(name):
        img = win._quick.grabFramebuffer().convertToFormat(QImage.Format_RGBA8888)
        assert not img.isNull()
        arr = np.frombuffer(img.constBits(), np.uint8).reshape(img.height(), img.width(), 4).copy()
        assert (arr[:, :, 3] > 20).sum() > 10000, 'empty framebuffer'
        img.save(str(out / (name + '.png')))
        images[name] = arr

    def finish():
        win.close()
        app.quit()

    def safe(fn):
        def run():
            try:
                fn()
            except Exception as exc:
                import traceback
                traceback.print_exc()
                errors.append(str(exc))
                finish()
        return run

    def rest():
        print('backend:', win._quick.quickWindow().rendererInterface().graphicsApi(), flush=True)
        if software:
            assert not win._root.property('skinnedMeshVisible')
            capture('software_fallback')
            finish()
            return
        assert win._root.property('skinnedMeshVisible'), 'mesh did not activate'
        capture('rest')
        win._skinned_item.setBlink(1.0)
        QTimer.singleShot(120, safe(blink))

    def blink():
        capture('blink')
        assert not np.array_equal(images['rest'], images['blink'])
        scale = images['rest'].shape[1] / 1280
        for x0, y0, x1, y1 in [(461, 589, 511, 638), (676, 571, 730, 625)]:
            a, b, c, d = [int(v * scale) for v in (x0, y0, x1, y1)]
            # Eye ROIs are recorded in source-art coordinates, before facing.
            opened_image, closed_image = images['rest'], images['blink']
            if win._root.property('visualFacing') < 0:
                opened_image, closed_image = opened_image[:, ::-1], closed_image[:, ::-1]
            opened, closed = opened_image[b:d, a:c], closed_image[b:d, a:c]
            assert ((opened[:, :, 3] > 240) & (closed[:, :, 3] < 220)).sum() < 4, 'blink tears the face'
            assert (closed[:, :, 2].astype(int) > closed[:, :, 0].astype(int) + 25).sum() < 4, 'closed iris remains visible'
        win._skinned_item.setBlink(0.0)
        inputs = MotionInputs(walking=True, walk_hz=1.5)
        for _ in range(40):
            frame = win._engine.step(inputs, 33)
        win._push_frame(frame)
        QTimer.singleShot(120, safe(walk))

    def walk():
        capture('walk')
        item = win._skinned_item
        for bone in item._rt.bones:
            item.setBonePose(bone.name, 0.0)
        item.setLookAt(0, 0)
        item.setBlink(0)
        for key, value in [('bodyAngle', 0), ('bodyY', 0), ('bodyScaleX', 1), ('bodyScaleY', 1)]:
            win._root.setProperty(key, value)
        QTimer.singleShot(250, safe(reset))

    def reset():
        capture('reset')
        assert np.array_equal(images['rest'], images['reset']), 'old pose trails remain'
        # A real action path must restore the image renderer immediately.
        win.play_frames([sprite], loop=False)
        assert not win._root.property('skinnedMeshVisible')
        win.stop_frames()
        assert win._root.property('skinnedMeshVisible')
        import dataclasses
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            broken = Path(tmp) / 'broken.json'
            broken.write_text('{broken', encoding='utf-8')
            win._spec = dataclasses.replace(spec, skinned_mesh=str(broken))
            win._setup_skinned_mesh()
            assert not win._root.property('skinnedMeshVisible'), 'broken mesh hides fallback'
        win._spec = spec
        win._setup_skinned_mesh()
        win.set_stage('adult')
        assert not win._root.property('skinnedMeshEnabled'), 'adult uses young mesh'
        win.set_stage('young')
        win.set_sprite(sprite)
        win._engine.reset()
        QTimer.singleShot(150, safe(animate))

    def animate():
        from PIL import Image
        item = win._skinned_item
        index = frame_index[0]
        img = win._quick.grabFramebuffer().convertToFormat(QImage.Format_RGBA8888)
        if index == 0:
            images['node_ids'] = {key: id(sg.verts) for key, sg in item._layer_sgs.items()}
        else:
            assert images['node_ids'] == {key: id(sg.verts) for key, sg in item._layer_sgs.items()}, 'geometry rebuilt per frame'
        rgba = Image.frombytes('RGBA', (img.width(), img.height()), bytes(img.constBits()))
        bg = Image.new('RGBA', rgba.size, '#eef1f6')
        bg.alpha_composite(rgba)
        animation.append(bg.convert('RGB').resize((480, 482)))
        if index == 45:
            animation[0].save(out / 'motion.gif', save_all=True, append_images=animation[1:],
                              duration=66, loop=0, disposal=2)
            finish()
            return
        inputs = MotionInputs(walking=16 <= index < 34, walk_hz=1.5,
                              cursor_pos=(1000 if index < 23 else -1000, 300),
                              pet_rect=(0, 0, 640, 642))
        frame = win._engine.step(inputs, 66)
        win._push_frame(frame)
        frame_index[0] += 1
        QTimer.singleShot(66, safe(animate))

    QTimer.singleShot(500, safe(rest))
    QTimer.singleShot(15000, safe(lambda: (_ for _ in ()).throw(TimeoutError('capture timeout'))))
    app.exec()
    return int(bool(errors))


if __name__ == '__main__':
    raise SystemExit(main())
