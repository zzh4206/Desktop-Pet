import QtQuick
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
