// render3d 运行时场景 v2——View3D + 透明背景 + Balsam 蒙皮组件 + toon + 分级光照。
// 解耦：模型 = Loader 加载 Balsam model.qml（Joint 按 objectName=骨名寻址）；
// 姿势 = Python bone_bridge 按 rig_profile 语义角色合成绝对四元数 → posePayload。
import QtQuick
import QtQuick3D

Rectangle {
    id: root
    color: "transparent"

    // ---- 注入属性（scene_host 逐帧/按需写） ----
    property url modelUrl: ""      // Balsam 组件 qml（含 Skin/Joint/Model）
    onModelUrlChanged: loadModel()
    property url textureUrl: ""    // 贴图绝对路径（toon 材质用）
    property var posePayload: ({}) // {骨名: [qx,qy,qz,qw]}——changed 时分发
    property bool spin: false
    property int lightLevel: 1
    property real lightDirX: 0
    property real lightDirY: 1
    property real lightDirZ: 0
    property real lightColorR: 1
    property real lightColorG: 1
    property real lightColorB: 1
    property real lightIntensity: 1
    property real ambientR: 0.35
    property real ambientG: 0.37
    property real ambientB: 0.45
    property bool shadowEnabled: false
    property real wetness: 0
    property var jointMap: ({})    // objectName → Node（装载后收集）

    // ---- 交互（D11 整窗语义：任何点击=宠物点击；按压=系统拖拽跟手） ----
    signal petClicked()
    signal petDragStarted()
    MouseArea {
        anchors.fill: parent
        onClicked: root.petClicked()
        onPressed: function(mouse) {
            root.petDragStarted()
            root.Window.window.startSystemMove()   // 无边框窗系统级拖动（跨平台）
            mouse.accepted = false                  // 不吞事件，留给上层（如双击菜单）
        }
    }

    onPosePayloadChanged: {
        if (!Object.keys(posePayload).length) return
        for (var k in posePayload) {
            var j = jointMap[k]
            if (j) j.rotation = Qt.quaternion(posePayload[k][3], posePayload[k][0],
                                              posePayload[k][1], posePayload[k][2])
        }
    }

    function collectJoints(node, map) {
        if (node.objectName) map[node.objectName] = node
        for (var i = 0; i < node.data.length; ++i) {
            var c = node.data[i]
            if (c && c.objectName !== undefined) collectJoints(c, map)
        }
    }

    function loadModel() {
        if (modelHolder.loaded || !root.modelUrl || !root.modelUrl.toString().length)
            return
        var comp = Qt.createComponent(root.modelUrl)
        if (comp.status === Component.Error) {
            console.log("R3D model load error:", comp.errorString())
            return
        }
        var obj = comp.createObject(modelHolder)
        if (!obj) { console.log("R3D createObject failed"); return }
        modelHolder.loaded = true
        var m = {}
        collectJoints(obj, m)
        root.jointMap = m
        try { applyToon(obj) } catch (e) { console.log("R3D toon swap skip:", e) }
    }

    function applyToon(item) {
        // 把蒙皮 Model 的材质换成 toon（PrincipledMaterial 为转换默认）
        for (var i = 0; i < item.data.length; ++i) {
            var c = item.data[i]
            if (c instanceof Model) c.materials = [toonMat]
            else if (c) applyToon(c)
        }
    }

    View3D {
        anchors.fill: parent

        environment: SceneEnvironment {
            backgroundMode: SceneEnvironment.Transparent
            tonemapMode: SceneEnvironment.TonemapModeLinear
        }

        PerspectiveCamera {
            id: cam
            position: Qt.vector3d(0, 90, 260)
            clipNear: 1
            clipFar: 2000
        }

        DirectionalLight {
            eulerRotation.y: root.lightDirX === 0 && root.lightDirZ === 0
                             ? 0 : Math.atan2(root.lightDirX, root.lightDirZ) * 180 / Math.PI
            eulerRotation.x: -Math.asin(Math.max(-1, Math.min(1, root.lightDirY))) * 180 / Math.PI
            brightness: 1.4 * root.lightIntensity
            ambientColor: Qt.rgba(root.ambientR, root.ambientG, root.ambientB, 1.0)
            visible: root.lightLevel > 0
        }

        Node {
            id: pivot
            scale: Qt.vector3d(150, 150, 150)

            // ⚠️ 3D 组件不能用 Loader（2D 机器，会把 Node 挂到 Item 下不进场景图，
            // 实测组件已实例化但不渲染）——用 createComponent+createObject 挂 3D 父。
            // 装载必须响应式：Python 在场景完成后才注入 modelUrl（completed 时为空）。
            Node {
                id: modelHolder
                property bool loaded: false
                Component.onCompleted: root.loadModel()
            }

            SequentialAnimation on eulerRotation.y {
                running: root.spin
                loops: Animation.Infinite
                NumberAnimation { from: 0; to: 360; duration: 6000 }
            }
        }
        // 成品网格 y∈[0,1.5]：相机高 90 略偏上俯视；蒙皮模型自身贴地（glTF 坐标原点=脚底）
    }

    CustomMaterial {
        id: toonMat
        shadingMode: CustomMaterial.Shaded
        property color uBase: "#7c86b8"
        property real uStep: 0.5
        property vector3d uAmbient: Qt.vector3d(root.ambientR, root.ambientG, root.ambientB)
        property vector3d uRim: Qt.vector3d(0.5, 0.55, 0.7)
        property real uWet: root.wetness
        property real uLightGain: root.lightIntensity
        property real uHasTex: 1.0
        property TextureInput uBaseTex: TextureInput {
            texture: Texture {
                source: root.textureUrl
                generateMipmaps: true
                mipFilter: Texture.Linear
            }
        }
        fragmentShader: "toon_materials/toon.frag"
    }
}
