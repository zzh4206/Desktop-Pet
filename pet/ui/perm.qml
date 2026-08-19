import QtQuick
import QtQuick.Controls
import PetPerm 1.0

// v0.8 win 权限自检页：win 无特权需求，运行时自检各能力可用性（§十二）
ApplicationWindow {
    id: root
    title: "桌宠 · 权限自检"
    width: 380
    height: 420
    visible: false
    color: "#f7f5f2"

    onClosing: function(close) { root.hide(); close.accepted = false }

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 16
        spacing: 10

        Text {
            Layout.fillWidth: true
            text: "Windows 端无需系统授权；以下为运行时能力自检"
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
            text: "重新检测"
            onClicked: Perm.refresh()
        }
    }
}
