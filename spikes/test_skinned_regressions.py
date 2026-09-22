"""Regression tests for v0.16 skinning, mesh coverage and stage selection."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

import numpy as np
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pet.rig.skinned_mesh_item import RigRuntime
from pet.rig.motion import MotionEngine, MotionInputs
from pet.rig.spec import load_rig_spec


class SkinningRegression(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.spec = load_rig_spec(str(ROOT / 'assets/rig/young'), 'young')
        cls.rt = RigRuntime.load(cls.spec.skinned_spec, cls.spec.skinned_mesh, cls.spec.skinned_layers)

    def test_weights_follow_bone_names_even_when_rank_changes(self):
        # Deliberately put the child before its parent in the source file.
        spec = {'skeleton': {'bones': [
            {'bone_name': 'b', 'parent': 'root', 'joint_pos': [0, 0]},
            {'bone_name': 'root', 'joint_pos': [0, 0]},
            {'bone_name': 'c', 'parent': 'root', 'joint_pos': [0, 0]},
        ]}}
        mesh = {'image_size_px': [100, 100], 'layers': [{
            'id': 'probe', 'bind_bone': 'root',
            'vertices': [[0, 0], [20, 0], [20, 20]],
            'uvs': [[0, 0], [.2, 0], [.2, .2]], 'triangles': [0, 1, 2],
            'weight_bones': [['b', 'c'], ['c', 'b'], ['unknown']],
            'weight_values': [[.75, .25], [.75, .25], [1]],
        }]}
        with tempfile.TemporaryDirectory() as tmp:
            sp, mp = Path(tmp) / 'spec.json', Path(tmp) / 'mesh.json'
            sp.write_text(json.dumps(spec), encoding='utf-8')
            mp.write_text(json.dumps(mesh), encoding='utf-8')
            rt = RigRuntime.load(str(sp), str(mp), tmp)
        zero = np.zeros(3, np.float32)
        tx, ty = zero.copy(), zero.copy()
        tx[rt.bone_index['b']] = 10
        ty[rt.bone_index['c']] = 8
        tx[rt.bone_index['root']] = 2
        rt.skinning_matrices(zero, tx, ty, 0, 0)
        actual = rt.deform(rt.layers[0], rt.layers[0].rest)[:, :2]
        np.testing.assert_allclose(actual, [[9.5, 2], [24.5, 6], [22, 20]], atol=1e-5)

    def test_only_young_discovers_young_assets(self):
        self.assertTrue(self.spec.skinned_spec)
        for stage in ('adult', 'final'):
            self.assertFalse(load_rig_spec(str(ROOT / 'assets/rig' / stage), stage).skinned_spec)

    def test_continuous_tail_and_rigid_face(self):
        self.assertEqual(len(self.rt.layers), 20)
        tails = [l for l in self.rt.layers if l.layer_id.startswith('tail')]
        self.assertEqual(len(tails), 1)
        self.assertEqual({self.rt.bones[i].name for i in tails[0].bone_idx},
                         {'tail_01', 'tail_02', 'tail_03', 'tail_fluke'})
        face = next(l for l in self.rt.layers if l.layer_id == 'face_base')
        self.assertEqual([self.rt.bones[i].name for i in face.bone_idx], ['head'])
        bangs = next(l for l in self.rt.layers if l.layer_id == 'bangs')
        self.assertGreater(bangs.rest[:, 1].min(), 210)  # no baked ahoge islands
        root = list(bangs.bone_idx).index(self.rt.bone_index['head'])
        np.testing.assert_allclose(bangs.weights[bangs.rest[:, 1] <= 340, root], 1)

    def test_blink_does_not_move_mouth_or_forehead(self):
        face = next(l for l in self.rt.layers if l.layer_id == 'face_base')
        keep = (face.rest[:, 1] > 685) | (face.rest[:, 1] < 485)
        np.testing.assert_allclose(self.rt.effective_rest(face, 1)[keep], face.rest[keep])
        np.testing.assert_allclose(self.rt.effective_rest(face, 0), face.rest)

    def test_blink_timing_uses_spec(self):
        engine = MotionEngine(self.spec)
        self.assertAlmostEqual(engine.step(MotionInputs(), 70).blink_progress, 1)
        self.assertAlmostEqual(engine.step(MotionInputs(), 30).blink_progress, 1)
        self.assertAlmostEqual(engine.step(MotionInputs(), 60).blink_progress, .5)
        self.assertAlmostEqual(engine.step(MotionInputs(), 60).blink_progress, 0)

    def test_mesh_covers_source_alpha(self):
        spec = json.loads(Path(self.spec.skinned_spec).read_text(encoding='utf-8'))
        by_id = {l['id']: l for l in spec['layers']}
        for layer in self.rt.layers:
            # Generated files contain a few alpha speckles intentionally excluded
            # by their largest_component mask. Test the unchanged source layers.
            if by_id[layer.layer_id].get('largest_component') or layer.gaze_uv:
                continue
            with self.subTest(layer=layer.layer_id):
                im = Image.open(layer.texture_path)
                alpha = np.asarray(im)[:, :, 3]
                coverage = Image.new('L', im.size)
                draw = ImageDraw.Draw(coverage)
                uv_px = layer.uv * im.size
                for triangle in layer.triangles.reshape(-1, 3):
                    draw.polygon([tuple(p) for p in uv_px[triangle]], fill=255)
                missed = (alpha > 15) & (np.asarray(coverage) == 0)
                self.assertLessEqual(int(missed.sum()), 2)

    def test_gaze_is_clipped_to_sclera(self):
        raw = json.loads(Path(self.spec.skinned_spec).read_text(encoding='utf-8'))
        ellipses = {layer['id']: layer['gaze_ellipse']
                    for layer in raw['layers'] if layer.get('gaze_ellipse')}
        for layer in self.rt.layers:
            if layer.gaze_uv:
                x, y = layer.rest[:, 0], layer.rest[:, 1]
                cx, cy, rx, ry = ellipses[layer.layer_id]
                self.assertLessEqual(float(np.max(((x-cx)/rx)**2 + ((y-cy)/ry)**2)), 1.00001)
        self.assertEqual(len(self.rt.pupil_idx), 0)

    def test_neutral_skinning_preserves_vertices(self):
        zero = np.zeros(len(self.rt.bones), np.float32)
        self.rt.skinning_matrices(zero, zero, zero, 0, 0)
        for layer in self.rt.layers:
            np.testing.assert_allclose(self.rt.deform(layer, layer.rest), layer.rest, atol=.001)


if __name__ == '__main__':
    unittest.main(verbosity=2)
