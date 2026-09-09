import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import PetStatus 1.0

// 状态板：可视化桌宠内部不可见状态。默认仅展示「养成」；行为/传感器/
// 聊天情绪/呈现/主动关怀为开发诊断项，开「开发模式」后展示。
// 每 1s 自动刷新（仅面板可见时），数据就地更新不重置滚动。
ApplicationWindow {
    id: root
    title: "桌宠 · 状态板"
    width: 400
    height: 620
    visible: false
    color: "#f7f5f2"

    onClosing: function(close) { root.hide(); close.accepted = false }

    // 面板可见时每秒刷新；不可见即停（不空转）
    Timer {
        interval: 1000
        running: root.visible
        repeat: true
        onTriggered: Status.refresh()
    }
    onVisibleChanged: if (visible) Status.refresh()

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 12
        spacing: 8

        RowLayout {
            Layout.fillWidth: true
            spacing: 8

            Text {
                Layout.fillWidth: true
                text: "桌宠内部状态（每秒自动刷新）"
                font.pixelSize: 12
                color: "#9a948c"
            }

            Text {
                text: "开发模式"
                font.pixelSize: 12
                color: "#7a746c"
            }
            Switch {
                id: devSwitch
                checked: Status.devMode
                onToggled: Status.setDevMode(devSwitch.checked)
            }
        }

        ListView {
            id: list
            Layout.fillWidth: true
            Layout.fillHeight: true
            clip: true
            spacing: 4
            model: Status

            delegate: Item {
                width: list.width
                height: model.type === "section"
                        ? 28
                        : Math.max(30, fieldRow.implicitHeight + 12)

                // 分组标题
                Text {
                    visible: model.type === "section"
                    anchors.left: parent.left
                    anchors.leftMargin: 2
                    anchors.verticalCenter: parent.verticalCenter
                    text: model.type === "section" ? model.name : ""
                    font.pixelSize: 12
                    font.bold: true
                    color: "#e8915d"
                }

                // 字段行卡片
                Rectangle {
                    id: fieldCard
                    visible: model.type === "field"
                    anchors.fill: parent
                    radius: 8
                    color: "#ffffff"
                    border.width: 1
                    border.color: model.level === "bad" ? "#d9534f"
                                : model.level === "warn" ? "#e8915d"
                                : "#f0eeea"

                    RowLayout {
                        id: fieldRow
                        anchors.fill: parent
                        anchors.margins: 8
                        spacing: 8

                        Text {
                            Layout.preferredWidth: 92
                            text: model.type === "field" ? model.name : ""
                            font.pixelSize: 12
                            color: "#7a746c"
                            elide: Text.ElideRight
                        }
                        Text {
                            Layout.fillWidth: true
                            text: model.type === "field" ? model.value : ""
                            font.pixelSize: 12
                            font.bold: true
                            color: model.level === "bad" ? "#d9534f"
                                 : model.level === "warn" ? "#b5702f"
                                 : "#33302c"
                            wrapMode: Text.Wrap
                            horizontalAlignment: Text.AlignRight
                        }
                    }
                }
            }
        }

        Button {
            Layout.fillWidth: true
            text: "刷新"
            onClicked: Status.refresh()
        }
    }
}
