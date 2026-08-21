import QtQuick
import QtQuick.Controls
import PetMem 1.0

// v0.9 记忆管理页：查看/删除/清空（§2.2 接口 + 版本规划 v0.9 Must）
ApplicationWindow {
    id: root
    title: "桌宠 · 记忆"
    width: 420
    height: 500
    visible: false
    color: "#f7f5f2"

    onClosing: function(close) { root.hide(); close.accepted = false }

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 16
        spacing: 10

        Text {
            Layout.fillWidth: true
            text: Mem.count === 0 ? "还没有记忆。聊天里告诉宠物关于你的事，"
                                  + "或让它调用 memory_save～"
                                : "宠物记得 " + Mem.count + " 条（按重要性排序）"
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
            model: Mem.items

            delegate: Rectangle {
                width: list.width
                height: memRow.implicitHeight + 16
                radius: 10
                color: "#ffffff"

                RowLayout {
                    id: memRow
                    anchors.fill: parent
                    anchors.margins: 8
                    spacing: 8

                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: 2

                        Text {
                            Layout.fillWidth: true
                            text: modelData.fact
                            font.pixelSize: 13
                            color: "#33302c"
                            wrapMode: Text.Wrap
                        }
                        Text {
                            text: "重要度 " + Math.round(modelData.importance * 100)
                                  + "% · 提过 " + modelData.recall_count + " 次"
                            font.pixelSize: 10
                            color: "#b0aaa2"
                        }
                    }

                    // 单条删除
                    Rectangle {
                        Layout.preferredWidth: 26
                        Layout.preferredHeight: 26
                        radius: 13
                        color: delMa.pressed ? "#d9d4ce" : "#f0eeea"

                        Text {
                            anchors.centerIn: parent
                            text: "✕"
                            font.pixelSize: 12
                            color: "#d9534f"
                        }
                        MouseArea {
                            id: delMa
                            anchors.fill: parent
                            cursorShape: Qt.PointingHandCursor
                            onClicked: Mem.forget(modelData.id)
                        }
                    }
                }
            }
        }

        RowLayout {
            Layout.fillWidth: true
            spacing: 8

            Button {
                Layout.fillWidth: true
                text: "刷新"
                onClicked: Mem.refresh()
            }

            Button {
                Layout.fillWidth: true
                text: "清空全部"
                enabled: Mem.count > 0
                onClicked: Mem.clear()
            }
        }
    }
}
