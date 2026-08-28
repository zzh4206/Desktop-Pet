// 分层绑骨场景（v0.13）—— RigPresenter 持有的 QQuickWidget 加载本文件。
//
// 设计约定（与 pet/rig/presenter.py 成对）：
//   · 单一 33ms 时钟 + 纯绑定合成：呼吸/行走律动/部件摆动互不覆盖，软件
//     渲染后端同样工作（offscreen 自动化测试可跑）。
//   · 双槽交叉淡化：figA 恒显，figB 以 mix 不透明度叠上；Python 侧用
//     QPropertyAnimation 补间 mix 并乒乓复用槽位 —— 帧序列不再硬切。
//   · 全部输入是普通属性/函数：facing 镜像即时翻转（对齐旧行为），
//     bodyTilt 行走/坠落倾斜，walking 律动开关，squash() 落地压扁回弹。
//   · 部件清单 partsModel 由 Python 注入（dict 附 _url 绝对 file:// 地址），
//     under_core 在主体之下渲染 —— 接缝天然被核心图遮挡（鲸尾策略）。
import QtQuick

Item {
    id: root

    // ---- Presenter 写入的状态 ----
    property url figASrc: ""
    property url figBSrc: ""
    property real mix: 0.0        // 0=A … 1=B 完全可见
    property int facing: 1        // 1 右 / -1 左（即时镜像，语义同旧 set_facing）
    property real bodyTilt: 0     // 速度倾斜目标角（度）
    property bool walking: false
    property real walkHz: 0       // 步态频率 Hz（FSM 速度映射；0=用部件周期）
    property string activeFigure: ""   // 当前展示的 figure 名（绑定件可见性）
    property var partsModel: []   // [{id,file,_url,source_figure,px_rect,pivot,z,kind,sway{...}}]

    // ---- 源图→画布几何（与 QLabel KeepAspectRatio+AlignCenter 同构）----
    property real srcW: 1024
    property real srcH: 1536
    readonly property double fitScale: Math.min(width / srcW, height / srcH)
    readonly property double dispW: srcW * fitScale
    readonly property double dispH: srcH * fitScale
    readonly property double offX: (width - dispW) / 2
    readonly property double offY: (height - dispH) / 2

    function setSourceSize(w, h) { srcW = w; srcH = h }

    // 落地压扁回弹（land 事件调用一次）
    function squash() { _sq.restart() }

    // ---- 全局时钟：唯一动画源（页面不可见时自动停，省电）----
    Timer {
        id: clock
        interval: 33; repeat: true; running: root.visible
        property real t: 0
        onTriggered: {
            t += interval
            // 步态包络：walking↔静止 的平滑过渡（时间常数 ~150ms，起步
            // 渐入、停步缓收——limb 角度乘 gaitK，相位不跳变）
            const target = root.walking ? 1.0 : 0.0
            root.gaitK += (target - root.gaitK) * (interval / 150.0)
            if (Math.abs(root.gaitK - target) < 0.01) root.gaitK = target
        }
    }
    // 呼吸（常驻）：周期 3.4s 峰 1.2%
    readonly property real breathY: 1.0 + 0.012 * Math.sin(2 * Math.PI * clock.t / 3400)
    // 落地冲量：squashAt 之后按指数衰减的压缩量
    property real squashAt: -1e9
    readonly property real squashK:
        clock.t - squashAt < 400 ? Math.exp(-(clock.t - squashAt) / 90) : 0
    // 步态分量（v0.14：频率与 limb 步频同源，包络乘 gaitK——停步不再
    // 依赖 walking 硬开关，幅度随 gaitK 收敛，无相位突跳）
    readonly property real gaitHz: walkHz > 0 ? walkHz : 1.3
    property real gaitK: 0
    readonly property real walkRot: gaitK * Math.sin(2 * Math.PI * clock.t * gaitHz / 1000.0) * 1.4
    readonly property real walkBob:
        gaitK * -Math.abs(Math.sin(2 * Math.PI * clock.t * gaitHz / 1000.0)) * 2.2

    SequentialAnimation {
        id: _sq
        ScriptAction { script: root.squashAt = clock.t }
    }

    Item {
        id: mirrorNode
        anchors.fill: parent
        transform: [
            Rotation {   // 速度倾斜（脚底原点）
                origin.x: root.width / 2; origin.y: root.height
                angle: root.bodyTilt + root.walkRot
                Behavior on angle { NumberAnimation { duration: 90;
                                                       easing.type: Easing.OutQuad } }
            },
            Scale {      // 朝向镜像 + 落地压扁（脚底原点）
                id: mirrorScale
                origin.x: root.width / 2; origin.y: root.height
                xScale: root.facing * (1 + 0.03 * root.squashK)
                yScale: root.breathY * (1 - 0.05 * root.squashK)
            }
        ]

        Item {
            id: bobNode
            anchors.fill: parent
            y: root.walkBob
            // bob 不加 Behavior：33ms 步进本身平滑，Behavior 反而滞后抖动

            // ---- under_core 部件（压在主体下，接缝被核心图遮住）----
            Repeater {
                model: root.partsModel.filter(function (p) { return p.z === "under_core" })
                delegate: RigPartDelegate {}
            }

            Image {
                id: figA
                anchors.fill: parent
                source: root.figASrc
                fillMode: Image.PreserveAspectFit
                mipmap: true
            }
            Image {
                id: figB
                anchors.fill: parent
                source: root.figBSrc
                fillMode: Image.PreserveAspectFit
                opacity: root.mix
                mipmap: true
            }

            // ---- over_core 部件 ----
            Repeater {
                model: root.partsModel.filter(function (p) { return p.z !== "under_core" })
                delegate: RigPartDelegate {}
            }
        }
    }

    // 部件委托：一槽一件；可见性由 source_figure===activeFigure 决定
    component RigPartDelegate : Item {
        id: dlg
        required property var modelData
        property var d: modelData
        anchors.fill: parent
        visible: !!d && d.source_figure === root.activeFigure
        width: parent ? parent.width : 0
        height: parent ? parent.height : 0

        Image {
            source: dlg.d && dlg.d._url ? dlg.d._url : ""
            visible: dlg.visible
            x: root.offX + d.px_rect[0] * root.fitScale
            y: root.offY + d.px_rect[1] * root.fitScale
            width: (d.px_rect[2] - d.px_rect[0]) * root.fitScale
            height: (d.px_rect[3] - d.px_rect[1]) * root.fitScale
            fillMode: Image.Stretch          // 包围盒即内容框
            mipmap: true; smooth: true
            transform: Rotation {
                origin.x: (d.pivot[0] - d.px_rect[0]) * root.fitScale
                origin.y: (d.pivot[1] - d.px_rect[1]) * root.fitScale
                angle: {
                    const s = d.sway || null
                    if (!s || !s.amp_deg) return 0
                    if (d.kind === "limb") {
                        // v0.14 行走驱动肢体：gaitK 包络 × 步频正弦；
                        // 相位取 phase_ms/period_ms 比例（双腿反相=0/0.5）
                        const hz = root.walkHz > 0
                            ? root.walkHz : 1000.0 / s.period_ms
                        return root.gaitK * s.amp_deg * Math.sin(
                            2 * Math.PI * (clock.t * hz / 1000.0
                                + (s.phase_ms || 0) / s.period_ms))
                    }
                    const ph = (clock.t + (s.phase_ms || 0)) % s.period_ms
                    return s.amp_deg * Math.sin(2 * Math.PI * ph / s.period_ms)
                }
            }
        }
    }
}
