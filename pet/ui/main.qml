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
    flags: Qt.WindowStaysOnTopHint  // 聊天窗口常置顶（点桌面不降层，高于宠物浮窗）
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
                model: Chat.messages
                spacing: 6
                delegate: Loader {
                    width: list.width
                    sourceComponent: model.modelData.role === "user" ? userBubble : petBubble
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

    Component {
        id: userBubble
        Item {
            width: parent ? parent.width : 420
            height: userRect.height

            Rectangle {
                id: userRect
                anchors.right: parent.right
                // 宽 = 自然宽与上限取小；txt.implicitWidth 是不换行自然宽（稳定）
                width: Math.min(list.width * 0.75, userTxt.implicitWidth + 16)
                color: "#4a90d9"
                radius: 10

                Text {
                    id: userTxt
                    anchors.centerIn: parent
                    width: parent.width - 16
                    text: model.modelData.rich
                    textFormat: Text.RichText
                    wrapMode: Text.Wrap
                    color: "white"
                }
                implicitHeight: userTxt.implicitHeight + 16
            }
        }
    }

    Component {
        id: petBubble
        Item {
            width: parent ? parent.width : 420
            height: petRect.height

            Rectangle {
                id: petRect
                anchors.left: parent.left
                width: Math.min(list.width * 0.75, petTxt.implicitWidth + 16)
                color: "#ececec"
                radius: 10

                Text {
                    id: petTxt
                    anchors.centerIn: parent
                    width: parent.width - 16
                    text: model.modelData.rich
                    textFormat: Text.RichText
                    wrapMode: Text.Wrap
                    color: "#222"
                }
                implicitHeight: petTxt.implicitHeight + 16
            }
        }
    }
}
