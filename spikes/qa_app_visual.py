"""Exercise the real PetApp startup and animation dispatch with isolated saves.

Uses the installed config/state; disables chat setup and global hotkeys only.
Run with the same Python/Qt environment as app.py.
"""
from pathlib import Path
import json
import sys
import tempfile
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QTimer
from app import PetApp
from pet.platform import get_platform_adapter


def main():
    adapter = get_platform_adapter()
    actual = adapter.get_paths()
    out = ROOT / 'spikes/_qa/v016_review/app'
    out.mkdir(parents=True, exist_ok=True)
    errors = []
    completed = []
    with tempfile.TemporaryDirectory(prefix='desktop-pet-app-qa-') as tmp:
        state = json.loads((Path(actual['data_dir']) / 'pet_state.json').read_text(encoding='utf8'))
        # Ensure startup works even when decay has no reason to notify observers.
        state['last_update'] = time.time()
        (Path(tmp) / 'pet_state.json').write_text(json.dumps(state), encoding='utf8')
        paths = dict(actual, data_dir=tmp, log_dir=tmp, lock_path=str(Path(tmp) / 'qa.lock'))
        with patch.object(adapter, 'get_paths', return_value=paths), \
                patch.object(PetApp, '_setup_chat'), \
                patch.object(PetApp, '_setup_hotkeys'):
            pet = PetApp(['app-visual-qa'], adapter, verbose=False)
        # Keep the production rendering timers; drive behavior deterministically.
        pet._tick_timer.stop()
        pet._proactive_timer.stop()
        pet._chat_emotion_timer.stop()

        def capture(name):
            image = pet.window._quick.grabFramebuffer()
            assert not image.isNull()
            image.save(str(out / (name + '.png')))

        def checked(fn):
            def run():
                try:
                    fn()
                except Exception as exc:
                    import traceback
                    traceback.print_exc()
                    errors.append(str(exc))
                    pet.shutdown()
            return run

        def startup():
            win = pet.window
            print(json.dumps({
                'presentation': pet.cfg['presentation'],
                'stage': pet.store.get().stage.value,
                'branch': pet.store.get().branch.value,
                'figure': win._root.property('activeFigure'),
                'mesh': win.skinned_motion_active(),
                'backend': str(win._quick.quickWindow().rendererInterface().graphicsApi()),
            }), flush=True)
            assert pet.cfg['presentation'] == 'rig'
            assert win.skinned_motion_active(), 'actual startup did not select the mesh'
            capture('startup')
            win.set_motion_params(walking=True, walk_hz=1.5)
            pet._frame_tick(None, 'walk', 'idle')
            assert not win.is_playing(), 'legacy walk sequence replaced the mesh'
            pet._play_animate('blink')
            assert not win.is_playing(), 'legacy blink sequence replaced the mesh'
            QTimer.singleShot(650, checked(walking))

        def walking():
            assert pet.window.skinned_motion_active()
            capture('walking')
            pet.window.set_motion_params(walking=False)
            pet._frame_tick(None, 'idle', 'walk')
            assert pet.window.skinned_motion_active()
            # An actual special action must still use its dedicated frames.
            pet._play_animate('stretch')
            assert pet.window.is_playing()
            assert not pet.window.skinned_motion_active()
            pet._stop_anim()
            assert pet.window.skinned_motion_active()
            print('PASS: startup, walk, blink, special-action fallback and restore', flush=True)
            completed.append(True)
            pet.shutdown()

        QTimer.singleShot(650, checked(startup))
        QTimer.singleShot(10000, pet.shutdown)
        pet.run()
    return bool(errors) or not completed


if __name__ == '__main__':
    raise SystemExit(main())
