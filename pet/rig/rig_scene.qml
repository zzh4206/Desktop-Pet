// 分层绑骨场景（v0.15 哑渲染器）—— RigPresenter 持有的 QQuickWidget 加载本文件。
//
// 设计约定（与 pet/rig/presenter.py + pet/rig/motion.py 成对）：
//   · v0.15 起运动数学全部下沉到 motion.py（MotionEngine），本场景退化为
//     哑渲染器：只照 Python 每帧推入的姿态数据摆图，不再做任何运动决策
//     （无 Timer、无正弦、无相位累加）。
//   · 保留一组"镜像属性"（bodyTilt/walking/walkHz/gaitK/gaitPhase/squashAt/
//     blinkOn），由 presenter 回写，供回归测试与门禁观察——语义与旧版同名
//     属性一致，只是由"QML 计算"改为"Python 计算、QML 只存值"。
//   · 双槽交叉淡化：figA 恒显，figB 以 mix 不透明度叠上；Python 侧用
//     QPropertyAnimation 补间 mix 并乒乓复用槽位。
//   · 部件清单 partsModel 由 Python 注入（dict 附 _url 绝对 file:// 地址）；
//     每部件角度由 partAngles 映射（part_id → 度）提供，Python 每帧推入。
import QtQuick
import PetRig 1.0

Item {
    id: root

    // ---- 2D 骨骼蒙皮（v0.18）----
    property bool skinnedMeshEnabled: false
    readonly property bool skinnedMeshVisible: skinnedMeshEnabled
        && skinnedMesh.ready && activeFigure === "healthy_neutral"
    property string specFile: ""
    property string meshDataFile: ""
    property string layersDir: ""
    property real lookAtX: 0.0
    property real lookAtY: 0.0
    property real blinkProgress: 0.0

    // ---- Presenter 写入的显示状态 ----
    property url figASrc: ""
    property url figBSrc: ""
    property real mix: 0.0        // 0=A … 1=B 完全可见
    property int facing: 1        // 1 右 / -1 左（即时镜像，语义同旧 set_facing）
    property string activeFigure: ""   // 当前展示的 figure 名（绑定件可见性）
    property var partsModel: []   // [{id,file,_url,source_figure,px_rect,pivot,z,kind,sway{...}}]

    // ---- 每帧姿态（motion.py 计算，presenter 推入）----
    property real bodyAngle: 0    // 整体旋转（tilt + walkRot），度
    property real bodyScaleX: 1   // 镜像 + squash 压缩
    property real bodyScaleY: 1   // 呼吸 + squash 压缩
    property real bodyY: 0        // 上下颠簸（walkBob），显示像素
    property bool blinkOn: false  // 眨眼脉冲（blink 覆盖件显隐）
    property var partAngles: ({}) // part_id -> 角度（度）

    // ---- 镜像属性（presenter 回写，测试/门禁观察用）----
    property real bodyTilt: 0     // 速度倾斜目标角
    property bool walking: false
    property real walkHz: 0
    property real gaitK: 0
    property real gaitPhase: 0
    property real squashAt: -1e9

    // ---- 实时地面阴影（v0.17 光影通道，presenter 推入；见 pet/sun.py）----
    property real shadowAlpha: 0.0     // 0..1（夜晚→0）
    property real shadowOffsetX: 0.0   // 归一化水平偏移（-0.5..0.5 × 窗宽）
    property real shadowScaleX: 0.6    // 水平拉伸（阴影宽 = scaleX × 窗宽）
    property real shadowScaleY: 0.07   // 垂直厚度（阴影高 = scaleY × 窗高）

    // ---- 源图→画布几何（与 QLabel KeepAspectRatio+AlignCenter 同构）----
    property real srcW: 1024
    property real srcH: 1536
    readonly property double fitScale: Math.min(width / srcW, height / srcH)
    readonly property double dispW: srcW * fitScale
    readonly property double dispH: srcH * fitScale
    readonly property double offX: (width - dispW) / 2
    readonly property double offY: (height - dispH) / 2

    function setSourceSize(w, h) { srcW = w; srcH = h }

    // The young airborne sprite has more transparent padding than its idle rig.
    function frameDisplayScale(url) {
        return url.toString().endsWith("/young_fall_air.png") ? 1.15 : 1.0
    }

    // 地面阴影（水平面 = 屏幕平面）：贴窗口底部、渲染在 mirrorNode 之前（被
    // 宠物压住）。不在 mirrorNode/bobNode 内 → 不受 bodyAngle/bodyScale/bodyY
    // 影响：影子跟着太阳走、跟着时长伸缩，不随身体摇晃/颠簸。
    Rectangle {
        id: groundShadow
        visible: root.shadowAlpha > 0.005
        width: root.width * root.shadowScaleX
        height: root.height * root.shadowScaleY
        radius: height / 2
        anchors.horizontalCenter: parent.horizontalCenter
        anchors.horizontalCenterOffset: root.shadowOffsetX * root.width
        // 垂直居中贴地：影子中心压在脚底线（窗口底），可见上半边向上淡出
        anchors.verticalCenter: parent.bottom
        gradient: Gradient {
            GradientStop { position: 0.0; color: Qt.rgba(0, 0, 0, 0.0) }
            GradientStop { position: 0.5; color: Qt.rgba(0, 0, 0, root.shadowAlpha) }
            GradientStop { position: 1.0; color: Qt.rgba(0, 0, 0, 0.0) }
        }
    }

    Item {
        id: mirrorNode
        anchors.fill: parent
        transform: [
            Rotation {   // 速度倾斜 + 步态滚转（脚底原点）
                origin.x: root.width / 2; origin.y: root.height
                angle: root.bodyAngle
                Behavior on angle { NumberAnimation { duration: 90;
                                                       easing.type: Easing.OutQuad } }
            },
            Scale {      // 朝向镜像 + 落地压扁 + 呼吸（脚底原点）
                id: mirrorScale
                origin.x: root.width / 2; origin.y: root.height
                xScale: root.bodyScaleX
                yScale: root.bodyScaleY
            }
        ]

        Item {
            id: bobNode
            width: parent.width
            height: parent.height
            y: root.bodyY
            // bob 不加 Behavior：33ms 步进本身平滑，Behavior 反而滞后抖动

            // 2D 骨骼蒙皮渲染节点（当 skinnedMeshEnabled 时接管渲染）
            SkinnedMeshItem {
                id: skinnedMesh
                objectName: "skinnedMesh"
                anchors.fill: parent
                visible: root.skinnedMeshVisible
                specFile: root.specFile
                meshDataFile: root.meshDataFile
                layersDir: root.layersDir
                lookAtX: root.lookAtX
                lookAtY: root.lookAtY
                blinkProgress: root.blinkProgress
            }

            // ---- under_core 部件（压在主体下，接缝被核心图遮住）----
            Repeater {
                model: root.skinnedMeshVisible ? [] : root.partsModel.filter(function (p) { return p.z === "under_core" })
                delegate: RigPartDelegate {}
            }

            Image {
                id: figA
                anchors.fill: parent
                source: root.figASrc
                scale: root.frameDisplayScale(source)
                fillMode: Image.PreserveAspectFit
                mipmap: true
                visible: !root.skinnedMeshVisible
            }
            Image {
                id: figB
                anchors.fill: parent
                source: root.figBSrc
                scale: root.frameDisplayScale(source)
                fillMode: Image.PreserveAspectFit
                opacity: root.mix
                mipmap: true
                visible: !root.skinnedMeshVisible
            }

            // ---- over_core 部件 ----
            Repeater {
                model: root.skinnedMeshVisible ? [] : root.partsModel.filter(function (p) { return p.z !== "under_core" })
                delegate: RigPartDelegate {}
            }
        }
    }

    // 部件委托：一槽一件；可见性由 source_figure===activeFigure 决定；
    // blink 覆盖件仅在脉冲窗口可见；角度由 Python 推入的 partAngles 提供
    component RigPartDelegate : Item {
        id: dlg
        required property var modelData
        property var d: modelData
        anchors.fill: parent
        visible: !!d && d.source_figure === root.activeFigure
            && (d.kind !== "blink" || root.blinkOn)

        Image {
            source: dlg.d && dlg.d._url ? dlg.d._url : ""
            visible: dlg.visible
            x: root.offX + d.px_rect[0] * root.fitScale
            y: root.offY + d.px_rect[1] * root.fitScale
            width: (d.px_rect[2] - d.px_rect[0]) * root.fitScale
            height: (d.px_rect[3] - d.px_rect[1]) * root.fitScale
            fillMode: Image.Stretch          // 包围盒即内容框
            smooth: true
            mipmap: false                    // 部件持续旋转下逐帧跳 LOD=色移
            transform: Rotation {
                origin.x: (d.pivot[0] - d.px_rect[0]) * root.fitScale
                origin.y: (d.pivot[1] - d.px_rect[1]) * root.fitScale
                angle: {
                    const m = root.partAngles
                    return (m && typeof m[d.id] === "number") ? m[d.id] : 0
                }
            }
        }
    }
}
