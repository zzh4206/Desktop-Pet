"""画风对照画廊 —— 两种候选渲染风格各出一张成品图并排拼接。

  python three_d/spikes/m1/render_gallery.py
产出：gallery_pbr.png / gallery_toon.png / gallery_compare.png（横排对照）
"""

from __future__ import annotations

import os

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
TRELLIS_MESH = os.path.join(REPO, "three_d/spikes/m1/assets/trellis_mesh.qml/meshes/geometry_0_mesh.mesh")
TRELLIS_BASE = os.path.join(REPO, "three_d/spikes/m1/assets/trellis_mesh.qml/maps/textureData.png")
HUNYUAN_MESH = os.path.join(REPO, "three_d/spikes/m1/assets/full_mesh.qml/meshes/geometry_0_mesh.mesh")
OUT_DIR = os.path.dirname(os.path.abspath(__file__))

QML_TEMPLATE = """import QtQuick
import QtQuick3D

Rectangle {{
    color: "transparent"
    View3D {{
        anchors.fill: parent
        environment: SceneEnvironment {{
            backgroundMode: SceneEnvironment.Transparent
            tonemapMode: SceneEnvironment.TonemapModeLinear
        }}
        PerspectiveCamera {{ id: cam; position: Qt.vector3d(0, 110, {camz}); clipNear: 1; clipFar: 3000 }}
        DirectionalLight {{
            eulerRotation.x: -28; eulerRotation.y: 22
            brightness: {brightness}
            ambientColor: Qt.rgba({amb}, {amb}, {amb2}, 1.0)
        }}
        Node {{
            Model {{
                source: "{mesh}"
                scale: Qt.vector3d(150, 150, 150)
                position: Qt.vector3d(0, -{half}, 0)
                {material}
            }}
        }}
    }}
}}
"""

PBR_MATERIAL = """materials: PrincipledMaterial {
                    baseColorMap: Texture { source: "%s"; generateMipmaps: true; mipFilter: Texture.Linear }
                    roughness: 0.85
                    metalness: 0.0
                    cullMode: PrincipledMaterial.NoCulling
                }""" % TRELLIS_BASE.replace("\\", "/")

TOON_MATERIAL = """materials: CustomMaterial {
                    shadingMode: CustomMaterial.Shaded
                    property color uBase: "#8d96c6"
                    property real uStep: 0.45
                    property vector3d uAmbient: Qt.vector3d(0.34, 0.36, 0.46)
                    property vector3d uRim: Qt.vector3d(0.6, 0.65, 0.8)
                    fragmentShader: "toon.frag"
                }"""

VARIANTS = [
    ("gallery_pbr", TRELLIS_MESH, 1.0, PBR_MATERIAL, 130, 0.42, 0.5),
    ("gallery_toon", HUNYUAN_MESH, 1.5, TOON_MATERIAL, 120, 0.45, 0.55),
]


def main() -> None:
    import sys
    from PySide6.QtCore import QTimer, QUrl
    from PySide6.QtQuick import QQuickView
    from PySide6.QtWidgets import QApplication

    only = sys.argv[1] if len(sys.argv) > 1 else None
    app = QApplication([])

    def render_variant(idx: int) -> None:
        name, mesh, height, material, brightness, amb, amb2 = VARIANTS[idx]
        if only and name != only:
            return
        qml = QML_TEMPLATE.format(mesh=QUrl.fromLocalFile(mesh).toString(),
                                  material=material, brightness=brightness,
                                  amb=amb, amb2=amb2, camz=430,
                                  half=height * 150 / 2)
        qml_path = os.path.join(OUT_DIR, f"_{name}.qml")
        with open(qml_path, "w", encoding="utf-8") as f:
            f.write(qml)
        view = QQuickView()
        view.setColor(QColor := __import__("PySide6.QtGui", fromlist=["QColor"]).QColor(0, 0, 0, 0))
        view.setResizeMode(QQuickView.ResizeMode.SizeRootObjectToView)
        view.setFlags(__import__("PySide6.QtGui", fromlist=["Qt"]).Qt.WindowType.FramelessWindowHint
                      | __import__("PySide6.QtGui", fromlist=["Qt"]).Qt.WindowType.Tool)
        view.resize(400, 720)
        view.setSource(QUrl.fromLocalFile(qml_path))
        screen = app.primaryScreen().availableGeometry()
        view.setPosition(screen.right() - 440, screen.bottom() - 760)
        view.show()

        def grab() -> None:
            img = app.primaryScreen().grabWindow(int(view.winId()))
            out = os.path.join(OUT_DIR, f"{name}.png")
            img.save(out)
            print("SHOT", out)
            view.close()
            QTimer.singleShot(150, app.quit)

        QTimer.singleShot(2200, grab)

    def stitch(paths: list[str]) -> None:
        from PIL import Image
        ims = [Image.open(p).convert("RGB") for p in paths]
        h = max(i.height for i in ims)
        w = sum(i.width for i in ims) + 12 * (len(ims) - 1)
        canvas = Image.new("RGB", (w, h), (24, 24, 28))
        x = 0
        for im in ims:
            canvas.paste(im, (x, (h - im.height) // 2))
            x += im.width + 12
        out = os.path.join(OUT_DIR, "gallery_compare.png")
        canvas.save(out)
        print("COMPARE", out)

    start = next((i for i, v in enumerate(VARIANTS) if v[0] == only), 0) if only else 0
    render_variant(start)
    app.exec()


if __name__ == "__main__":
    main()
