import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PySide6.QtWidgets import QApplication
from PySide6.QtQuickWidgets import QQuickWidget
from PySide6.QtQuick import QQuickItem
from PySide6.QtCore import QUrl
import pet.rig.skinned_mesh_item as smi

app = QApplication.instance() or QApplication([])
w = QQuickWidget()
w.setResizeMode(QQuickWidget.ResizeMode.SizeRootObjectToView)
qml_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "spike_render.qml")
with open(qml_path, "w", encoding="utf-8") as f:
    f.write("""import QtQuick
import PetRig 1.0
Item {
    width: 600
    height: 600
    SkinnedMeshItem {
        id: skinnedMesh
        objectName: "skinnedMesh"
        anchors.fill: parent
        specFile: "assets/reference/young_rig_spec.json"
        meshDataFile: "assets/rig_young/mesh/mesh_data.json"
        layersDir: "assets/rig_young/layers"
    }
}
""")

w.setSource(QUrl.fromLocalFile(qml_path))
w.resize(600, 600)
w.show()
app.processEvents()

root = w.rootObject()
item = root.findChild(QQuickItem, "skinnedMesh")
print("Found item:", item)

# Let's animate a bit
item.setBonePose("tail_01", 15.0)
item.setBonePose("tail_02", 20.0)
item.setBonePose("tail_03", 25.0)
item.setBonePose("tail_fluke", 30.0)
item.setBonePose("head", -5.0)
item.setBlink(0.7)
item.setLookAt(0.5, 0.2)

# Force a render
w.repaint()
app.processEvents()

img = w.grabFramebuffer()
print("Grabbed framebuffer size:", img.width(), img.height(), "isNull:", img.isNull())
out_path = "assets/rig_young/render_skinned_test.png"
img.save(out_path)
print("Saved rendered frame to:", out_path)
print("Item _rt initialized:", item._rt is not None)
if item._rt is not None:
    print("Layers in rt:", len(item._rt.layers))
    print("Bones in rt:", len(item._rt.bones))
w.close()
