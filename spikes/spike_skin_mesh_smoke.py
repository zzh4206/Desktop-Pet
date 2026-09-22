"""SkinnedMeshItem 冒烟（offscreen + software）——数学核直测 + 真渲染像素验证。

两个互不依赖的通过门：
  A. RigRuntime 纯数学核（无窗口）：FK 恒等性、层级旋转传播、look-at 椭圆
     限幅、blink 比例挤压、坏件降级（坏 json / 未知骨权重）。
  B. Software 后端真实截图：必须回退分层位图，不能伪装为支持任意几何。
     硬件姿态/拖影验收另运行 qa_skinned_visual.py --backend d3d11。

用法：
  python spikes/spike_skin_mesh_smoke.py
"""

from __future__ import annotations

import json
import math
import os
import sys
import tempfile

sys.path.insert(0, ".")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")

import numpy as np

from PySide6.QtCore import QUrl
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPEC_FILE = os.path.join(ROOT, "assets", "reference", "young_rig_spec.json")
LAYERS_DIR = os.path.join(ROOT, "assets", "rig_young", "layers")

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(("  [OK] " if cond else "  [FAIL] ") + name + (f"  [{detail}]" if detail else ""))


def build_quad_mesh(out_path: str) -> dict:
    """按 spec 的 bbox_hint 生成 22 层 quad 网格（tail_seg2 加三骨权重）。"""
    with open(SPEC_FILE, encoding="utf-8") as f:
        spec = json.load(f)
    img_w, img_h = spec["skeleton"]["source_reference"]["image_size_px"]
    layers = []
    for l in spec["layers"]:
        x0, y0, x1, y1 = l["bbox_hint"]
        px = [(x0 * img_w, y0 * img_h), (x1 * img_w, y0 * img_h),
              (x1 * img_w, y1 * img_h), (x0 * img_w, y1 * img_h)]
        # 层图是全画布透明层（内容只占 bbox 区域）：UV 取 bbox 归一化四角，
        # 而非 0..1 铺满（后者会采样到画布四角的透明区 → 整层不可见）
        uv = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
        if l["id"] == "tail_seg2":
            bones = l["influence_bones"][:3]
            vals = [0.5, 0.35, 0.15]
        else:
            bones = [l["bind_bone"]]
            vals = [1.0]
        layers.append({
            "id": l["id"],
            "texture": f"{l['id']}.png",
            "z_order": l["z_order"],
            "vertices": [[round(x, 2), round(y, 2)] for x, y in px],
            "uvs": [[round(u, 6), round(v, 6)] for u, v in uv],
            "triangles": [0, 1, 2, 0, 2, 3],
            "weight_bones": [bones] * 4,
            "weight_values": [vals] * 4,
        })
    mesh = {"spec": 1, "image_size_px": [img_w, img_h], "layers": layers}
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(mesh, f)
    return mesh


def part_a_math() -> None:
    print("== A. 数学核（RigRuntime） ==")
    from pet.rig.skinned_mesh_item import RigRuntime

    tmp = tempfile.mkdtemp(prefix="skin_mesh_smoke_")
    mesh_path = os.path.join(tmp, "mesh.json")
    build_quad_mesh(mesh_path)

    rt = RigRuntime.load(SPEC_FILE, mesh_path, LAYERS_DIR)
    check("加载成功", rt is not None)
    if rt is None:
        return
    check("22 层 / 47 骨", len(rt.layers) == 22 and len(rt.bones) == 47,
          f"{len(rt.layers)}层/{len(rt.bones)}骨")
    check("层按 z 升序",
          all(a.z_order <= b.z_order for a, b in zip(rt.layers, rt.layers[1:])))

    B = len(rt.bones)
    zero = np.zeros(B, np.float32)

    # ---- 零姿态 → M ≡ I ----
    rt.skinning_matrices(zero, zero, zero, 0.0, 0.0)
    check("零姿态 M≡I", bool(np.allclose(rt.M, np.eye(3, dtype=np.float32),
                                        atol=1e-4)))

    # ---- tail_01 旋转 12°（钳内）：关节不动、偏移点绕关节旋转、子关节随动 ----
    j1 = np.array(rt.bones[rt.bone_index["tail_01"]].joint_px, np.float64)
    j2 = np.array(rt.bones[rt.bone_index["tail_02"]].joint_px, np.float64)
    ang = np.zeros(B, np.float32)
    ang[rt.bone_index["tail_01"]] = 12.0
    rt.skinning_matrices(ang, zero, zero, 0.0, 0.0)
    m1 = rt.M[rt.bone_index["tail_01"]].astype(np.float64)
    m2 = rt.M[rt.bone_index["tail_02"]].astype(np.float64)

    def apply(m: np.ndarray, p: np.ndarray) -> np.ndarray:
        return (m @ np.array([p[0], p[1], 1.0]))[:2]

    th = math.radians(12.0)
    rot = np.array([[math.cos(th), -math.sin(th)],
                    [math.sin(th), math.cos(th)]])
    check("关节点不变", np.allclose(apply(m1, j1), j1, atol=1e-2),
          f"{apply(m1, j1)} vs {j1}")
    probe = j1 + np.array([100.0, 0.0])
    want = j1 + rot @ np.array([100.0, 0.0])
    check("偏移点绕关节旋转", np.allclose(apply(m1, probe), want, atol=1e-2),
          f"{apply(m1, probe)} vs {want}")
    want2 = j1 + rot @ (j2 - j1)
    check("子关节随父旋转", np.allclose(apply(m2, j2), want2, atol=1e-2),
          f"{apply(m2, j2)} vs {want2}")

    # ---- 角度钳制：tail_01 clamp ±15，90° 应收敛到 15° ----
    ang[rt.bone_index["tail_01"]] = 90.0
    rt.skinning_matrices(ang, zero, zero, 0.0, 0.0)
    m1c = rt.M[rt.bone_index["tail_01"]].astype(np.float64)
    eff_deg = math.degrees(math.atan2(m1c[1, 0], m1c[0, 0]))
    check("angle_clamp 钳制", abs(eff_deg - 15.0) < 1e-3, f"生效 {eff_deg:.3f}°")

    # ---- look-at 椭圆限幅 ----
    dx, dy = rt.look_offset(1.0, 1.0)
    lx, ly = rt.look.axis_px
    check("椭圆边界钳制", abs((dx / lx) ** 2 + (dy / ly) ** 2 - 1.0) < 1e-9,
          f"({dx:.3f},{dy:.3f})")
    dx, dy = rt.look_offset(0.5, 0.5)
    check("界内原样通过", dx == 0.5 * lx and dy == 0.5 * ly)

    # ---- blink 上/下睑挤压（78% / 22%） ----
    layer = next(l for l in rt.layers if l.layer_id == "eyelid_l")
    check("eyelid_l 挂眨眼配置", layer.blink is not None)
    if layer.blink is not None:
        cfg = layer.blink
        eff = rt.effective_rest(layer, 1.0)
        ok_up = ok_lo = True
        for r in range(layer.rest.shape[0]):
            y0 = float(layer.rest[r, 1])
            y1 = float(eff[r, 1])
            if y0 < cfg.center_y:
                want_y = cfg.upper_piv_y + (y0 - cfg.upper_piv_y) * (1 - 0.78)
            else:
                want_y = cfg.lower_piv_y + (y0 - cfg.lower_piv_y) * (1 - 0.22)
            if abs(y1 - want_y) > 1e-2:
                if y0 < cfg.center_y:
                    ok_up = False
                else:
                    ok_lo = False
        check("上睑 78% 挤压", ok_up)
        check("下睑 22% 挤压", ok_lo)
        check("开眼零形变",
              np.allclose(rt.effective_rest(layer, 0.0), layer.rest, atol=1e-5))

    # ---- LBS 蒙皮（K>1 权重平均） ----
    seg2 = next(l for l in rt.layers if l.layer_id == "tail_seg2")
    rt.skinning_matrices(zero, zero, zero, 0.0, 0.0)
    out = rt.deform(seg2, rt.effective_rest(seg2, 0.0))
    check("零姿态 LBS 恒等", np.allclose(out[:, :2], seg2.rest[:, :2], atol=1e-3))

    # ---- 降级：坏 json / 未知骨权重 ----
    bad = os.path.join(tmp, "bad.json")
    with open(bad, "w", encoding="utf-8") as f:
        f.write("{not json")
    check("坏 json → None", RigRuntime.load(SPEC_FILE, bad, LAYERS_DIR) is None)
    empty = os.path.join(tmp, "empty.json")
    with open(empty, "w", encoding="utf-8") as f:
        json.dump({"spec": 1, "image_size_px": [1280, 1284], "layers": []}, f)
    check("空 layers → None", RigRuntime.load(SPEC_FILE, empty, LAYERS_DIR) is None)

    one = os.path.join(tmp, "one.json")
    with open(one, "w", encoding="utf-8") as f:
        json.dump({"spec": 1, "image_size_px": [1280, 1284], "layers": [{
            "id": "tail_seg1", "vertices": [[700, 900], [900, 900], [900, 1000]],
            "uvs": [[0, 0], [1, 0], [1, 1]], "triangles": [0, 1, 2],
            "weight_bones": [["ghost_bone", "tail_01"]] * 3,
            "weight_values": [[0.5, 0.5]] * 3}]}, f)
    rt2 = RigRuntime.load(SPEC_FILE, one, LAYERS_DIR)
    check("未知骨剔除不崩", rt2 is not None and len(rt2.layers) == 1)
    if rt2 is not None:
        w = rt2.layers[0].weights
        check("权重归一化回退", bool(np.allclose(w.sum(axis=1), 1.0, atol=1e-6)
                                    and w.shape[1] == 1))


def part_b_render() -> None:
    """Software rendering must use the known-good figure/parts fallback."""
    import subprocess
    with tempfile.TemporaryDirectory(prefix="skin_software_") as tmp:
        result = subprocess.run([sys.executable, os.path.join(ROOT, "spikes", "qa_skinned_visual.py"),
                                 "--backend", "software", "--output", tmp], check=False)
        check("software backend renders fallback pixels", result.returncode == 0)


def main() -> int:
    part_a_math()
    app_needed = QApplication.instance() or QApplication(sys.argv)
    part_b_render()
    print(f"\n== 冒烟结果：{len(PASS)} 通过 / {len(FAIL)} 失败 ==")
    for f in FAIL:
        print("  FAIL:", f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
