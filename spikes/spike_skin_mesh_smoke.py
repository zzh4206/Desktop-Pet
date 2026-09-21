"""SkinnedMeshItem 冒烟（offscreen + software）——数学核直测 + 真渲染像素验证。

两个互不依赖的通过门：
  A. RigRuntime 纯数学核（无窗口）：FK 恒等性、层级旋转传播、look-at 椭圆
     限幅、blink 比例挤压、坏件降级（坏 json / 未知骨权重）。
  B. QQuickWidget 真渲染：22 层 quad 网格 + 真实层图 PNG，offscreen 出图，
     非背景像素数与姿态驱动的像素差双断言；顶点缓冲视图跨帧稳定性。

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
    print("== B. QQuickWidget 真渲染 ==")
    from PySide6.QtCore import QTimer
    from PySide6.QtQuickWidgets import QQuickWidget
    from pet.rig.skinned_mesh_item import SkinnedMeshItem, _DIRTY_SUPPORTED
    print(f"  [diag] 脏标记通道: {'主路径(就地刷新)' if _DIRTY_SUPPORTED else '回退路径(整树重建)'}")

    tmp = tempfile.mkdtemp(prefix="skin_mesh_qml_")
    mesh_path = os.path.join(tmp, "mesh.json")
    build_quad_mesh(mesh_path)
    qml_path = os.path.join(tmp, "scene.qml")
    with open(qml_path, "w", encoding="utf-8") as f:
        f.write(f"""
import QtQuick
import PetRig 1.0
Item {{
    SkinnedMeshItem {{
        objectName: "skin"
        anchors.fill: parent
        specFile: "{SPEC_FILE.replace(os.sep, '/')}"
        meshDataFile: "{mesh_path.replace(os.sep, '/')}"
        layersDir: "{LAYERS_DIR.replace(os.sep, '/')}"
    }}
}}
""")

    app = QApplication.instance() or QApplication(sys.argv)
    widget = QQuickWidget()
    widget.resize(360, 480)
    widget.setResizeMode(QQuickWidget.ResizeMode.SizeRootObjectToView)
    widget.setSource(QUrl.fromLocalFile(qml_path))
    check("QML 加载无错误", widget.status() == QQuickWidget.Status.Ready
          or not widget.errors())
    widget.show()     # offscreen 平台"显示"到内存表面；不 show 场景图不渲染

    def pixels(img):
        img = img.convertToFormat(QImage.Format.Format_ARGB32)
        return np.frombuffer(img.constBits(), np.uint8,
                             count=img.sizeInBytes()).copy()

    def count_fg(img) -> int:
        raw = pixels(img).reshape(img.height(), img.bytesPerLine())
        arr = raw[:, :img.width() * 4].reshape(img.height(), img.width(), 4)
        bg = arr[2, 0].astype(np.int16)          # 左上角像素当背景参照
        diff = np.abs(arr.astype(np.int16) - bg)
        return int((diff.max(axis=2) > 16).sum())

    def run_checks() -> None:
        try:
            root_obj = widget.rootObject()
            item = (root_obj.findChild(SkinnedMeshItem) if root_obj else None) \
                or widget.findChild(SkinnedMeshItem)
            if item is None:
                print("  [diag] findChild 为 None；errors:",
                      [e.toString() for e in widget.errors()])
            else:
                print(f"  [diag] item._rt={item._rt is not None} "
                      f"failed={item._load_failed} "
                      f"spec='{item._spec_file}' mesh='{item._mesh_file}' "
                      f"layers='{item._layers_dir}'")
            check("QML 实例化并完成加载", item is not None
                  and item._rt is not None and not item._load_failed)
            img0 = widget.grab().toImage()
            check("grab 出图非空", not img0.isNull())
            if img0.isNull() or item is None:
                app.quit()
                return
            fg0 = count_fg(img0)
            check("出图前景像素充足", fg0 > 2000, f"{fg0}px")
            img_store.append((img0, item))

            def force_grab(retries: int = 3):
                """强制渲染后抓图：update→processEvents→repaint 循环，吸收
                缺陷构建（回退路径）下 grab 与帧调度的竞态。"""
                for _ in range(retries):
                    app.processEvents()
                    widget.repaint()
                    img = widget.grab().toImage()
                    if not img.isNull():
                        return img
                return img

            def check_round2() -> None:
                img0b, item2 = img_store[0]
                item2.setBonePose("hair_back_l_02", -12.0, 0.0, 4.0)
                item2.setLookAt(1.0, 1.0)      # 定格一个稳定非零姿态
                img1 = force_grab()
                verts_after = {k: id(v.verts)
                               for k, v in item2._layer_sgs.items()}
                if _DIRTY_SUPPORTED:
                    check("顶点视图零重建", verts_before == verts_after
                          and len(verts_after) == 22, f"{len(verts_after)} 层")
                else:
                    # 绑定缺陷回退路径按设计整树重建：验证层树完整 + 顶点仍直写
                    check("回退路径层树完整（整树重建设计）",
                          len(verts_after) == 22)
                check("姿态驱动画面变化", count_fg(img1) > 2000
                      and not np.array_equal(pixels(img0b), pixels(img1)))
                # 全部姿态归零后应精确回到初始帧（同输入 → 同栅格化输出）
                item2.setBonePose("tail_02", 0.0)
                item2.setBonePose("hair_back_l_02", 0.0, 0.0, 0.0)
                item2.setBlink(0.0)
                item2.setLookAt(0.0, 0.0)
                app.processEvents()
                QTimer.singleShot(400, check_round3)

            def check_round3() -> None:
                img0c, _ = img_store[0]
                img2 = force_grab()
                check("姿态归零画面回归",
                      np.array_equal(pixels(img0c), pixels(img2)))
                widget.setSource(QUrl())
                widget.close()
                app.processEvents()
                app.quit()

            # 顶点缓冲视图跨帧稳定（无重建/无重分配）
            verts_before = {k: id(v.verts) for k, v in item._layer_sgs.items()}
            # 生产式驱动：33ms 定时推姿态 ~0.4s（对齐 motion/presenter 的
            # 驱动节奏；单发 setPose 在缺陷构建上会撞上帧调度竞态）
            drive_state = {"t": 0.0}
            drive_timer = QTimer()
            drive_timer.setInterval(33)

            def drive_step() -> None:
                drive_state["t"] += 0.033
                item.setBonePose("tail_02", 18.0 * math.sin(drive_state["t"] * 3.0))
                item.setBlink(0.5 + 0.5 * math.sin(drive_state["t"]))

            drive_timer.timeout.connect(drive_step)
            drive_timer.start()
            QTimer.singleShot(420, check_round2)

            def stop_drive() -> None:
                drive_timer.stop()

            QTimer.singleShot(410, stop_drive)
        except Exception:                      # noqa: BLE001 —— 冒烟要打全迹
            import traceback
            traceback.print_exc()
            FAIL.append("B 部分异常")
            app.quit()

    img_store: list = []
    QTimer.singleShot(900, run_checks)
    app.exec()


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
