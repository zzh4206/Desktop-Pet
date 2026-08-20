import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import PetPerm 1.0

// v0.8 权限自检页（mac/win 共用）：头部文案经 Perm.note 注入，
// 项列表 model: Perm.items 通用；mac 有"打开系统设置"深链按钮。
ApplicationWindow {
    id: root
    title: "桌宠 · 权限自检"
    width: 380
    height: 460
    visible: false
    color: "#f7f5f2"

    onClosing: function(close) { root.hide(); close.accepted = false }

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
                height: 44
                radius: 10
                color: "#ffffff"

                Text {
                    anchors.left: parent.left
                    anchors.leftMargin: 12
                    anchors.verticalCenter: parent.verticalCenter
                    text: modelData.name
                    font.pixelSize: 13
                    color: "#33302c"
                }
                Text {
                    anchors.right: parent.right
                    anchors.rightMargin: 12
                    anchors.verticalCenter: parent.verticalCenter
                    text: modelData.ok ? "✓ 可用" : "✗ " + modelData.detail
                    font.pixelSize: 12
                    color: modelData.ok ? "#4a9e5f" : "#d9534f"
                }
            }
        }

        Button {
            Layout.fillWidth: true
            text: "打开系统设置"
            onClicked: Perm.open_settings()
        }

        Button {
            Layout.fillWidth: true
            text: "重新检测"
            onClicked: Perm.refresh()
        }
    }
}
