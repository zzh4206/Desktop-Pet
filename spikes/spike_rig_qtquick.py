"""P0 渲染载体 Spike（v0.13 分层绑骨渲染器前置，不占正式版本号——对齐 §0.7 并行 Spike 惯例）。

验证 Qt Quick 承载"丝滑展示层"的两个硬前提：
  A. offscreen 平台 + QT_QUICK_BACKEND=software 下 QQuickWidget 能否渲染
     （双 Image 交叉淡化 + 变换）→ 决定自动化测试是否可用 offscreen 全绿；
     软件后端不支持 ShaderEffect/GridMesh 自定义着色器 → 形变类特效运行时
     需按 GraphicsApi 能力降级（spike 结论写死在 presenter 的能力探测里）。
  B. 实机（windows 平台）透明置顶窗内嵌 QQuickWidget：桌面合成是否黑底/闪边、
     winId 是否可取（register_own_windows 排除表兼容）、DPR 与图形 API 报告。
     —— B 的画质结论需人工目检，脚本只打印事实并定时自关。

用法：
  python spikes/spike_rig_qtquick.py off    # A：无窗口无交互，断言出图
  python spikes/spike_rig_qtquick.py live   # B：弹 6s 演示窗后自关
  python spikes/spike_rig_qtquick.py        # 默认 off
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, ".")

QML_SCENE = """
import QtQuick
Item {
    id: root
    property real figOpacity: 1.0
    Image {
        id: baseFig
        source: figA
        fillMode: Image.PreserveAspectFit
        anchors.fill: parent
    }
    Image {
        id: altFig
        source: figB
        fillMode: Image.PreserveAspectFit
        anchors.fill: parent
        opacity: 1.0 - root.figOpacity
    }
    // 模拟"部件件"：呆毛独立旋转（paper-doll 可行性）
    Image {
        id: part
        source: partSrc
        width: parent.width * 0.18
        fillMode: Image.PreserveAspectFit
        x: parent.width * 0.42 - width / 2
        y: parent.height * 0.04
        rotation: Math.sin(swing.t * 0.004) * 8
        transformOrigin: Item.Bottom
    }
    Timer { id: swing; interval: 16; repeat: true; property real t: 0
            onTriggered: t += 16 }
    SequentialAnimation on figOpacity {
        loops: Animation.Infinite
        NumberAnimation { to: 0.0; duration: 700 }
        NumberAnimation { to: 1.0; duration: 700 }
    }
}
"""

PASS, FAIL = [], []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("  ✅ " if cond else "  ❌ ") + name)


def run_offscreen() -> int:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("QT_QUICK_BACKEND", "software")
    from PySide6.QtCore import QTimer, QUrl
    from PySide6.QtGui import QImage
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(sys.argv)

    # 软件渲染下 QQuickWidget 只认本地文件 source；把 QML 写临时文件最稳
    import tempfile

    qml_path = os.path.join(tempfile.gettempdir(), "spike_rig_scene.qml")
    with open(qml_path, "w", encoding="utf-8") as f:
        f.write(QML_SCENE)

    from PySide6.QtQuickWidgets import QQuickWidget
    from PySide6.QtCore import Qt

    w = QQuickWidget()
    w.setClearColor(Qt.transparent)
    w.setAttribute(Qt.WA_TranslucentBackground, True)
    w.setAttribute(Qt.WA_AlwaysStackOnTop, True)
    w.resize(192, 192)
    ctx = w.rootContext()
    ctx.setContextProperty("figA",
        QUrl.fromLocalFile(os.path.abspath("assets/ai/final_healthy_neutral.png")).toString())
    ctx.setContextProperty("figB",
        QUrl.fromLocalFile(os.path.abspath("assets/ai/final_neglected_neutral.png")).toString())
    ctx.setContextProperty("partSrc",
        QUrl.fromLocalFile(os.path.abspath("assets/ai/final_neglected_sad.png")).toString())
    # 根 Item 无显式尺寸，默认 SizeViewToRootObject 会把控件缩成 0×0
    w.setResizeMode(QQuickWidget.ResizeMode.SizeRootObjectToView)
    w.setSource(QUrl.fromLocalFile(qml_path))
    check("A1 QML 加载无错误", w.status() == QQuickWidget.Ready
          or not w.errors())
    w.show()   # offscreen 平台可"显示"到内存表面；不 show 则场景图不渲染

    def finish():
        ok_loaded = w.rootObject() is not None
        check("A2 rootObject 就绪", ok_loaded)
        img = w.grab().toImage()   # QQuickWidget 走 QWidget 渲染快照
        check("A3 grab 快照出图非空",
              img is not None and not img.isNull() and img.width() > 0)
        # 数非零像素（软件渲染 grab 应含立绘内容）
        nz = sum(1 for y in range(0, img.height(), 7)
                 for x in range(0, img.width(), 7)
                 if img.pixel(x, y) != 0)
        check("A4 内容像素占比>10%（真实绘制）", nz > (img.width()//7)*(img.height()//7)*0.10)
        print(f"\n[off] 图形API={_api(w)} size={img.width()}x{img.height()} "
              f"DPR={w.devicePixelRatioF()}")
        print(f"Spike-offscreen: {len(PASS)} 通过, {len(FAIL)} 失败")
        app.exit(1 if FAIL else 0)

    QTimer.singleShot(1200, finish)
    return app.exec() or (1 if FAIL else 0)


def run_live() -> int:
    from PySide6.QtCore import QTimer, QUrl, Qt
    from PySide6.QtWidgets import QApplication
    from PySide6.QtQuickWidgets import QQuickWidget

    import tempfile

    app = QApplication.instance() or QApplication(sys.argv)
    qml_path = os.path.join(tempfile.gettempdir(), "spike_rig_scene.qml")
    with open(qml_path, "w", encoding="utf-8") as f:
        f.write(QML_SCENE)

    # 载体一：WindowBase 式 QWidget 置顶透明窗
    from pet.window import WindowBase
    from pet.asset_provider import SpriteRef

    host = WindowBase(SpriteRef(path="🐱", width=320, height=320))
    child = QQuickWidget(host)
    child.setClearColor(Qt.transparent)
    child.setAttribute(Qt.WA_TranslucentBackground, True)
    child.setAttribute(Qt.WA_AlwaysStackOnTop, True)
    child.setGeometry(0, 0, 320, 320)
    ctx = child.rootContext()
    ctx.setContextProperty("figA",
        QUrl.fromLocalFile(os.path.abspath("assets/ai/final_neglected_neutral.png")).toString())
    ctx.setContextProperty("figB",
        QUrl.fromLocalFile(os.path.abspath("assets/ai/final_neglected_sad.png")).toString())
    ctx.setContextProperty("partSrc",
        QUrl.fromLocalFile(os.path.abspath("assets/ai/final_neglected_sad.png")).toString())
    child.setResizeMode(QQuickWidget.ResizeMode.SizeRootObjectToView)
    child.setSource(QUrl.fromLocalFile(qml_path))
    host.show()

    print(f"[live] 图形API={_api(child)} winId(host)={int(host.winId())} "
          f"DPR={child.devicePixelRatioF()} qml_status={child.status()}")
    QTimer.singleShot(14000, app.quit)   # 留足人工目检窗(自动关)
    rc = app.exec()
    print(f"Spike-live: {len(PASS)} 通过, {len(FAIL)} 失败")
    return rc


def _api(widget) -> str:
    try:
        from PySide6.QtQuick import QQuickWindow, QSGRendererInterface
        win = widget.quickWindow()
        ri = win.rendererInterface() if win else None
        api = ri.graphicsApi() if ri else None
        names = {0: "Unknown", 1: "Software", 2: "OpenVG", 3: "OpenGL",
                 4: "Direct3D11", 5: "Direct3D12", 6: "Metal",
                 7: "Vulkan", 8: "Rhi"}
        try:
            n = int(api)
        except (TypeError, ValueError):
            return str(api)
        return f"{n}({names.get(n,'?')})"
    except Exception as e:
        return f"N/A({e})"


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "off"
    sys.exit(run_offscreen() if mode == "off" else run_live())
