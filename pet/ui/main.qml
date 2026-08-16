import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import PetChat 1.0

// 聊天面板 v0.4.6 美化（win 主笔）：暖色调、头像位、气泡小尾巴、
// 消息滑入淡入过渡、流式光标、输入区悬浮条。纯 QML，mac 零适配。
ApplicationWindow {
    id: root
    title: "桌宠 · 聊天"
    width: 420
    height: 560
    visible: false
    color: "#f7f5f2"

    // ---- 主题色（集中定义，v0.10 接 config 主题系统时改这里） ----
    readonly property color cBg: "#f7f5f2"        // 暖白底
    readonly property color cUser: "#5b8def"      // user 气泡蓝
    readonly property color cPet: "#ffffff"       // pet 气泡白
    readonly property color cPetText: "#33302c"   // pet 文字暖黑
    readonly property color cAccent: "#e8915d"    // 强调橙（发送按钮/宠物头像底）

    // 关闭不退出 app：Esc/窗口 X 仅隐藏
    onClosing: function(close) { root.hide(); close.accepted = false }

    ColumnLayout {
        anchors.fill: parent
        spacing: 0

        // ---- 顶栏（柔和分隔，非硬线） ----
        Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: 44
            color: "#ffffff"

            Text {
                anchors.centerIn: parent
                text: "桌宠 · 聊天"
                font.pixelSize: 14
                font.bold: true
                color: root.cPetText
            }
            Rectangle {  // 底部渐隐分隔
                anchors.bottom: parent.bottom
                width: parent.width; height: 1
                gradient: Gradient {
                    orientation: Qt.Horizontal
                    GradientStop { position: 0.0; color: "#00000000" }
                    GradientStop { position: 0.5; color: "#14222222" }
                    GradientStop { position: 1.0; color: "#00000000" }
                }
            }
        }

        // ---- 消息区 ----
        ScrollView {
            Layout.fillWidth: true
            Layout.fillHeight: true
            clip: true
            topPadding: 12
            bottomPadding: 12

            ListView {
                id: list
                model: Chat
                spacing: 10
                boundsBehavior: Flickable.StopAtBounds

                // inline delegate（model.role/model.rich 直接可见）
                delegate: Item {
                    width: list.width
                    // 进入动画：滑入 + 淡入（流式追加的尾条也会走这里）
                    opacity: 0
                    Behavior on opacity { NumberAnimation { duration: 160 } }
                    Component.onCompleted: opacity = 1

                    height: row.implicitHeight

                    Row {
                        id: row
                        spacing: 8
                        // pet 行贴左（头像在左）/ user 行贴右（头像在右）：
                        // layoutDirection 只反转内部顺序，行本身须锚定
                        anchors.left: model.role === "user" ? undefined : parent.left
                        anchors.right: model.role === "user" ? parent.right : undefined
                        layoutDirection: model.role === "user" ? Qt.RightToLeft : Qt.LeftToRight
                        leftPadding: 12
                        rightPadding: 12

                        // 头像位：pet=宠物 emoji 底；user=首字母圆标
                        Rectangle {
                            id: avatar
                            width: 30; height: 30; radius: 15
                            anchors.verticalCenter: parent.verticalCenter
                            color: model.role === "user" ? "#c8d4ea" : root.cAccent
                            readonly property string petFace: "🐱"
                            Text {
                                anchors.centerIn: parent
                                text: model.role === "user" ? "我" : parent.petFace
                                font.pixelSize: model.role === "user" ? 12 : 18
                                color: model.role === "user" ? "#44597e" : "white"
                            }
                        }

                        // 气泡 + 小尾巴
                        Item {
                            anchors.verticalCenter: parent.verticalCenter
                            implicitWidth: bubbleRect.width + 4
                            implicitHeight: bubbleRect.height

                            Rectangle {
                                id: bubbleRect
                                x: model.role === "user" ? 4 : 0
                                width: Math.min(list.width - 110,
                                                bubbleTxt.implicitWidth + 22)
                                radius: 12
                                // 靠头像侧的角收直（对话感）
                                readonly property bool mine: model.role === "user"
                                topRightRadius: mine ? 4 : radius
                                bottomRightRadius: mine ? 4 : radius
                                topLeftRadius: mine ? radius : 4
                                bottomLeftRadius: mine ? radius : 4
                                color: mine ? root.cUser : root.cPet
                                // 柔和投影（气泡浮起感）
                                Rectangle {
                                    anchors.fill: parent; anchors.margins: 0
                                    radius: parent.radius; z: -1
                                    color: "#14222222"
                                    border.width: 0
                                }

                                Text {
                                    id: bubbleTxt
                                    anchors.centerIn: parent
                                    width: parent.width - 22
                                    text: model.rich
                                    textFormat: Text.RichText
                                    wrapMode: Text.Wrap
                                    color: model.role === "user" ? "white" : root.cPetText
                                    font.pixelSize: 13
                                    lineHeight: 1.25
                                }
                                implicitHeight: bubbleTxt.implicitHeight + 18
                            }
                        }
                    }
                }

                onCountChanged: Qt.callLater(function() { list.positionViewAtEnd() })
            }
        }

        // ---- 输入区（悬浮条样式） ----
        Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: 64
            color: "#ffffff"

            Rectangle {
                anchors.top: parent.top
                width: parent.width; height: 1
                gradient: Gradient {
                    orientation: Qt.Horizontal
                    GradientStop { position: 0.0; color: "#00000000" }
                    GradientStop { position: 0.5; color: "#14222222" }
                    GradientStop { position: 1.0; color: "#00000000" }
                }
            }

            RowLayout {
                anchors.fill: parent
                anchors.margins: 10
                spacing: 8

                Rectangle {
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    radius: 18
                    color: input.activeFocus ? "#ffffff" : "#f0eeea"
                    border.width: 1
                    border.color: input.activeFocus ? root.cAccent : "#00000000"

                    TextField {
                        id: input
                        anchors.fill: parent
                        anchors.leftMargin: 14
                        placeholderText: "说点什么…（Enter 发送 / Esc 隐藏）"
                        font.pixelSize: 13
                        color: root.cPetText
                        placeholderTextColor: "#9a948c"
                        verticalAlignment: TextInput.AlignVCenter
                        background: null
                        focus: true
                        selectByMouse: true
                        onAccepted: sendBtn.clicked()
                        Keys.onEscapePressed: root.hide()
                    }
                }

                // 圆形发送按钮（强调色）
                Rectangle {
                    Layout.preferredWidth: 38
                    Layout.preferredHeight: 38
                    radius: 19
                    color: sendMa.pressed ? "#d17a45" : root.cAccent

                    Text {
                        anchors.centerIn: parent
                        text: "➤"
                        color: "white"
                        font.pixelSize: 15
                    }
                    MouseArea {
                        id: sendMa
                        anchors.fill: parent
                        cursorShape: Qt.PointingHandCursor
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
    }
}
