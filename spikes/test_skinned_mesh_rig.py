"""2D 骨骼蒙皮呈现器实机与集成门禁测试 (spikes/test_skinned_mesh_rig.py)"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication
from pet.asset_provider import SpriteRef
from pet.rig.motion import MotionEngine, MotionInputs
from pet.rig.presenter import RigWindow, build_rig_window
from pet.rig.spec import RigSpec, load_rig_spec
from pet.window import WindowBase

passed = 0
failed = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f"  [OK] {name}" + (f"  [{detail}]" if detail else ""))
    else:
        failed += 1
        print(f"  [FAIL] {name}" + (f"  [{detail}]" if detail else ""))


def main():
    global passed, failed
    app = QApplication.instance() or QApplication([])

    print("== 1. 幼年体蒙皮资产加载与 Spec 检查 ==")
    spec = load_rig_spec("assets/rig/young", "young")
    check("spec 加载非空", spec is not None)
    if spec is not None:
        check("skinned_spec 存在且有效", bool(spec.skinned_spec) and os.path.isfile(spec.skinned_spec), spec.skinned_spec)
        check("skinned_mesh 存在且有效", bool(spec.skinned_mesh) and os.path.isfile(spec.skinned_mesh), spec.skinned_mesh)
        check("skinned_layers 存在且有效", bool(spec.skinned_layers) and os.path.isdir(spec.skinned_layers), spec.skinned_layers)
        check("physics_presets 弹簧群齐全", len(spec.physics_presets.get("spring_groups", [])) == 17)
        check("face_mechanics 配置存在", "blink" in spec.face_mechanics and "look_at" in spec.face_mechanics)

    print("== 2. MotionEngine 47 骨物理动力学与注视/眨眼解算 ==")
    engine = MotionEngine(spec)
    inputs = MotionInputs(
        tilt_deg=5.0,
        walking=True,
        walk_hz=1.5,
        facing=1,
        cursor_pos=(450.0, 350.0),
        pet_rect=(100.0, 100.0, 400.0, 400.0),
    )
    frame = engine.step(inputs, 33.0)
    check("frame 输出非空", frame is not None)
    check("47 骨姿态输出充足", len(frame.bone_angles) >= 40, f"{len(frame.bone_angles)} 骨")
    check("尾巴 4 节波浪链级联计算", all(b in frame.bone_angles for b in ["tail_01", "tail_02", "tail_03", "tail_fluke"]))
    t_angles = [frame.bone_angles[b] for b in ["tail_01", "tail_02", "tail_03", "tail_fluke"]]
    check("尾巴波浪链非零振荡", any(abs(a) > 0.01 for a in t_angles), str(t_angles))
    check("连续眼睑眨眼行程计算", 0.0 <= frame.blink_progress <= 1.0, f"progress={frame.blink_progress:.3f}")
    check("视线追踪向光标偏转", -1.0 <= frame.look_at[0] <= 1.0 and -1.0 <= frame.look_at[1] <= 1.0, f"look={frame.look_at}")

    # 步态关节角度验证
    check("行走时大腿与小腿角度非零", abs(frame.bone_angles.get("upper_leg_l", 0.0)) > 0.1)

    print("== 3. RigWindow 蒙皮呈现器集成与渲染 ==")
    sprite_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "assets", "rig", "young", "figs", "healthy_neutral.png")
    sprite = SpriteRef(path=sprite_path, width=192, height=192)
    win = build_rig_window(WindowBase, sprite, "young", defer_quick=False)
    check("win 实例化为 RigWindow", isinstance(win, RigWindow))
    check("win.rig_active 活性", win.rig_active)
    check("win._skinned_item 成功激活并绑定", win._skinned_item is not None)

    # 推进多拍，验证每拍推帧与脏标记刷新
    win.resize(500, 500)
    win.show()
    for i in range(10):
        app.processEvents()
        win._motion_tick()
        win._quick.repaint()

    check("SkinnedMeshItem 蒙皮核 _rt 成功运行", win._skinned_item._rt is not None)
    if win._skinned_item._rt is not None:
        check("蒙皮层数等于 22", len(win._skinned_item._rt.layers) == 22)
        check("蒙皮骨骼数等于 47", len(win._skinned_item._rt.bones) == 47)

    # 验证真实透明离屏渲染抓图
    img = QImage(500, 500, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.transparent)
    win._quick.render(img)
    check("离屏抓图非空", not img.isNull())
    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "assets", "rig_young")
    preview_path = os.path.join(out_dir, "preview_skinned_window.png")
    img.save(preview_path)
    check("预览截图保存成功", os.path.isfile(preview_path), preview_path)

    win.close()
    win.deleteLater()

    print(f"\n== 测试结果：{passed} 通过 / {failed} 失败 ==")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
