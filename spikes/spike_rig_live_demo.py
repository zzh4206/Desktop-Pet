"""v0.13 实机目检演示窗（人工冒烟，不入回归）—— RigWindow 直驱 9s 自关。

序列：idle 呼吸(常驻) → walk 律动+尾摆循环交叉淡化 → 表情切换
(sad 整帧淡化) → 回中性 → 落地 squash。期间截图供目检。
"""

from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QUICK_BACKEND", "")   # 实机走默认 RHI(D3D11)
sys.path.insert(0, ".")

from PySide6.QtCore import QTimer
from PySide6.QtGui import QScreen, QGuiApplication
from PySide6.QtWidgets import QApplication

from pet.asset_provider import AIArtProvider, SpriteRef
from pet.rig.presenter import RigWindow
from pet.rig.spec import load_rig_spec


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    provider = AIArtProvider()
    base = os.path.abspath("assets/ai/final_neglected_neutral.png")
    spec = load_rig_spec("assets/rig/final", "final")
    win = RigWindow(SpriteRef(path=base, width=320, height=320), spec)
    assert win.rig_active
    if os.environ.get("RIG_KEEP_LABEL"):
        # 合成层诊断：label 需真的画出非空像素（空文本透明背景仍可能整层
        # 不提交）——放一个暗色小点，运行时被置顶场景遮住大半
        win._label.setStyleSheet("background:transparent;color:#101012;")
        win._label.setText("·")
        win._label.show()
    scr: QScreen = QGuiApplication.primaryScreen()
    g = scr.availableGeometry()
    win.move_bottom_center(g.x() + g.width() * 0.62,
                           g.y() + int(g.height() * 0.86))
    win.show()

    t = [0]

    def phase():
        t[0] += 1
        n = t[0]
        if n == 1:
            print("[demo] idle 呼吸 + 尾摆（2s）")
        elif n == 2:
            print("[demo] walk 律动循环（3s）")
            win.set_motion_params(tilt_deg=2.5, walking=True, airborne=False)
            win.play_frames(provider.frames_for("final", "walk"),
                            loop=True, interval_ms=180)
            def snap():
                print(f"[demo] visible={win.isVisible()} geo={win.geometry()}")
                img = win.grab()
                img.save(os.path.abspath("spikes/_qa/demo_grab.png"))
                print("[demo] 自照已存 spikes/_qa/demo_grab.png")
            QTimer.singleShot(1200, snap)
        elif n == 3:
            print("[demo] 情绪整帧切换 sad→neutral（淡化）")
            win.set_motion_params(walking=False)
            win.stop_frames()
            win.set_sprite(SpriteRef(
                path=os.path.abspath("assets/ai/final_neglected_sad.png"),
                width=320, height=320))
        elif n == 4:
            win.set_sprite(SpriteRef(path=base, width=320, height=320))
            # 落地压扁演示
            win.set_motion_params(airborne=True)
        elif n == 5:
            win.set_motion_params(airborne=False)   # 触发 squash
            print("[demo] squash 完成，保持 2s 后退出")

    timer = QTimer()
    timer.timeout.connect(phase)
    timer.start(2000)
    QTimer.singleShot(11_000, app.quit)
    rc = app.exec()
    print(f"[demo] done rc={rc}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
