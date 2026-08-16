import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import PetChat 1.0

ApplicationWindow {
    id: root
    title: "桌宠 · 聊天"
    width: 420
    height: 560
    visible: false
    color: "#fafafa"

    // 关闭不退出 app：Esc/窗口 X 仅隐藏
    onClosing: function(close) { root.hide(); close.accepted = false }

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 8
        spacing: 6

        ScrollView {
            Layout.fillWidth: true
            Layout.fillHeight: true
            clip: true

            ListView {
                id: list
                model: Chat
                spacing: 6

                // inline delegate（model.role/model.rich 直接可见，不用 Loader/Component）
                delegate: Item {
                    width: list.width
                    height: bubbleRect.height

                    Rectangle {
                        id: bubbleRect
                        // user 靠右蓝 / pet 靠左灰
                        anchors.right: model.role === "user" ? parent.right : undefined
                        anchors.left: model.role === "user" ? undefined : parent.left
                        width: Math.min(list.width * 0.75, bubbleTxt.implicitWidth + 16)
                        color: model.role === "user" ? "#4a90d9" : "#ececec"
                        radius: 10

                        Text {
                            id: bubbleTxt
                            anchors.centerIn: parent
                            width: parent.width - 16
                            text: model.rich
                            textFormat: Text.RichText
                            wrapMode: Text.Wrap
                            color: model.role === "user" ? "white" : "#222"
                        }
                        implicitHeight: bubbleTxt.implicitHeight + 16
                    }
                }

                onCountChanged: Qt.callLater(function() { list.positionViewAtEnd() })
            }
        }

        // 流式占位：正在生成的助手消息
        Text {
            Layout.fillWidth: true
            visible: Chat.streamingText.length > 0
            text: Chat.streamingText
            textFormat: Text.RichText
            wrapMode: Text.Wrap
            color: "#333"
            leftPadding: 10; rightPadding: 10; topPadding: 4; bottomPadding: 4
            Rectangle {
                z: -1
                anchors.fill: parent
                color: "#fff3d6"
                radius: 10
            }
        }

        RowLayout {
            Layout.fillWidth: true
            spacing: 6

            TextField {
                id: input
                Layout.fillWidth: true
                placeholderText: "说点什么…"
                focus: true
                onAccepted: sendBtn.clicked()
                Keys.onEscapePressed: root.hide()
            }

            Button {
                id: sendBtn
                text: "发送"
                onClicked: {
                    if (input.text.trim().length > 0) {
                        Chat.send(input.text)
                        input.text = ""
                    }
                }
            }
        }
    }
}
