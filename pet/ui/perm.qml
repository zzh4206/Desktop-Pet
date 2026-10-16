import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Window
import PetPerm 1.0

// v0.8 权限自检页（mac/win 共用）：头部文案经 Perm.note 注入，
// 项列表 model: Perm.items 通用；mac 有"打开系统设置"深链按钮。
// v0.23.8：定位开关独立成列表下方整宽按钮（delegate 内嵌按钮实测点不到）；
// 窗口打开时居中定位（旧版系统默认摆放，底部按钮可能被屏幕边缘裁掉）；
// 按钮显式描边高对比样式（macOS 默认样式淡到看不见、不像能点）。
ApplicationWindow {
    id: root
    title: "桌宠 · 权限自检"
    width: 380
    height: 560
    visible: false
    color: "#f7f5f2"

    // 定位项状态（从 items 提取；items 变化自动重算）
    readonly property bool locOn: {
        for (var i = 0; i < Perm.items.length; i++)
            if (Perm.items[i].action === "location")
                return Perm.items[i].ok
        return false
    }

    onClosing: function(close) { root.hide(); close.accepted = false }

    Component.onCompleted: {
        // 居中到可用桌面（旧版交给系统摆位——实测可能把底部按钮推出屏幕）
        x = Math.round((Screen.desktopAvailableWidth - width) / 2)
        y = Math.round((Screen.desktopAvailableHeight - height) / 2) - 20
        console.log("[perm] 窗口就绪 x=%1 y=%2 w=%3 h=%4 可用=%5x%6"
                    .arg(x).arg(y).arg(width).arg(height)
                    .arg(Screen.desktopAvailableWidth)
                    .arg(Screen.desktopAvailableHeight))
        Perm.refresh()
    }

    // v0.23.8 定位开启确认：页内 QML 对话框（不混 AppKit 模态——
    // NSAlert.runModal 在 QML 事件回调里嵌套模态循环会冻死主线程）。
    Dialog {
        id: locateConfirm
        modal: true
        title: "开启 IP 定位"
        width: 320
        parent: Overlay.overlay
        anchors.centerIn: parent

        contentItem: Text {
            text: "会向 ip-api.com 发送你的公网 IP 以获取大致位置" +
                  "（城市级，明文 HTTP），仅用于天气提醒；" +
                  "坐标随本地缓存保存，之后不再重复发送。\n\n" +
                  "关闭定位后需手动填写城市。"
            wrapMode: Text.Wrap
            font.pixelSize: 13
            color: "#33302c"
        }

        footer: DialogButtonBox {
            Button {
                text: "确认开启"
                DialogButtonBox.buttonRole: DialogButtonBox.AcceptRole
            }
            Button {
                text: "取消"
                DialogButtonBox.buttonRole: DialogButtonBox.RejectRole
            }
        }

        onAccepted: {
            console.log("[perm] 确认开启 → Perm.enable_location()")
            Perm.enable_location()
        }
    }

    // 高对比自绘按钮（不用 QQC Button：macOS 原生样式禁自定义 background/
    // contentItem（样式告警），且渲染极淡像不可点；Rectangle+MouseArea
    // 无样式耦合，点击行为最朴素可靠）
    component PermButton: Rectangle {
        id: pb
        Layout.fillWidth: true
        implicitHeight: 34
        radius: 8
        color: ma.pressed ? "#e8e4de" : "#ffffff"
        border.color: "#cfc9c0"
        border.width: 1
        property string text: ""
        signal clicked()
        Text {
            anchors.centerIn: parent
            text: pb.text
            color: "#33302c"
            font.pixelSize: 13
        }
        MouseArea {
            id: ma
            anchors.fill: parent
            cursorShape: Qt.PointingHandCursor
            onClicked: pb.clicked()
        }
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 16
        spacing: 10

        Text {
            Layout.fillWidth: true
            text: Perm.note
            font.pixelSize: 12
            color: "#9a948c"
            wrapMode: Text.Wrap
        }

        ListView {
            id: list
            Layout.fillWidth: true
            Layout.fillHeight: true
            clip: true
            spacing: 6
            model: Perm.items

            delegate: Rectangle {
                width: list.width
                // v0.23.0 原始双列文本布局（纯只读，无按钮）
                implicitHeight: Math.max(44, row.implicitHeight + 16)
                height: implicitHeight
                radius: 10
                color: "#ffffff"

                RowLayout {
                    id: row
                    anchors.fill: parent
                    anchors.leftMargin: 12
                    anchors.rightMargin: 12
                    anchors.topMargin: 8
                    anchors.bottomMargin: 8
                    spacing: 12

                    Text {
                        Layout.fillWidth: true
                        Layout.alignment: Qt.AlignVCenter
                        text: modelData.name
                        font.pixelSize: 13
                        color: "#33302c"
                        elide: Text.ElideRight
                    }

                    Text {
                        Layout.maximumWidth: 150
                        Layout.alignment: Qt.AlignRight | Qt.AlignVCenter
                        text: modelData.ok ? "✓ 可用" : "✗ " + modelData.detail
                        font.pixelSize: 12
                        color: modelData.ok ? "#4a9e5f" : "#d9534f"
                        horizontalAlignment: Text.AlignRight
                        wrapMode: Text.Wrap
                    }
                }
            }
        }

        // v0.23.8 定位开关：独立整宽按钮（不进 delegate）。
        // console.log 埋点：点击后日志可定位事件链断点。
        PermButton {
            text: root.locOn ? "关闭 IP 定位" : "开启 IP 定位（需确认）"
            onClicked: {
                console.log("[perm] 定位按钮点击 locOn=", root.locOn)
                if (root.locOn) Perm.disable_location()
                else {
                    console.log("[perm] 打开确认对话框")
                    locateConfirm.open()
                }
            }
        }

        PermButton {
            text: "打开系统设置"
            onClicked: Perm.open_settings()
        }

        PermButton {
            text: "重新检测"
            onClicked: {
                console.log("[perm] 重新检测点击")
                Perm.refresh()
            }
        }
    }
}
