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
    // v0.17.0 输入框封顶高度（约 5 行，超出后输入框内部滚动不再挤占消息区）
    readonly property int inputMaxHeight: 110

    // 关闭不退出 app：Esc/窗口 X 仅隐藏
    onClosing: function(close) { root.hide(); close.accepted = false }

    // v0.17.5 删除会话确认（不可逆——消息无回收站；Popup 不支持
    // anchors，坐标相对 contentItem 手工居中）
    Popup {
        id: deleteConfirm
        x: (root.width - width) / 2
        y: (root.height - height) / 2
        modal: true
        focus: true
        width: 280
        height: 150
        padding: 0
        property string pendingSid: ""
        property string pendingTitle: ""

        background: Rectangle {
            radius: 14
            color: "#ffffff"
            border.color: "#14222222"
            border.width: 1
            Rectangle {  // 柔影
                anchors.fill: parent; z: -1
                radius: parent.radius; color: "#1c222222"
            }
        }
        Column {
            anchors.fill: parent
            anchors.margins: 18
            spacing: 10

            Text {
                width: parent.width
                text: "删除会话「%1」？".arg(deleteConfirm.pendingTitle)
                font.pixelSize: 14
                font.bold: true
                color: root.cPetText
                wrapMode: Text.Wrap
            }
            Text {
                width: parent.width
                text: "该会话的全部消息将不可恢复"
                font.pixelSize: 12
                color: "#9a948c"
                wrapMode: Text.Wrap
            }
            Row {
                spacing: 10
                layoutDirection: Qt.RightToLeft   // 主操作（删除）在右

                Rectangle {
                    width: 84; height: 32; radius: 16
                    color: delMa.pressed ? "#d17a45" : root.cAccent
                    Text {
                        anchors.centerIn: parent
                        text: "删除"
                        font.pixelSize: 13
                        color: "white"
                    }
                    MouseArea {
                        id: delMa
                        anchors.fill: parent
                        cursorShape: Qt.PointingHandCursor
                        onClicked: {
                            Chat.deleteSession(deleteConfirm.pendingSid)
                            deleteConfirm.close()
                        }
                    }
                }
                Rectangle {
                    width: 84; height: 32; radius: 16
                    color: cancelMa.pressed ? "#e8e4dd" : "#f0eeea"
                    Text {
                        anchors.centerIn: parent
                        text: "取消"
                        font.pixelSize: 13
                        color: root.cPetText
                    }
                    MouseArea {
                        id: cancelMa
                        anchors.fill: parent
                        cursorShape: Qt.PointingHandCursor
                        onClicked: deleteConfirm.close()
                    }
                }
            }
        }
    }

    ColumnLayout {
        anchors.fill: parent
        spacing: 0

        // ---- 顶栏（v0.17.2 会话切换器 + 新建；柔和分隔，非硬线） ----
        Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: 44
            color: "#ffffff"

            // 左：新建会话
            Rectangle {
                anchors.left: parent.left
                anchors.leftMargin: 10
                anchors.verticalCenter: parent.verticalCenter
                width: 30; height: 30; radius: 15
                color: plusMa.pressed ? "#f0e2d6"
                     : plusMa.containsMouse ? "#f7ede3" : "#00000000"
                Text {
                    anchors.centerIn: parent
                    text: "＋"
                    font.pixelSize: 18
                    color: root.cAccent
                }
                MouseArea {
                    id: plusMa
                    anchors.fill: parent
                    cursorShape: Qt.PointingHandCursor
                    hoverEnabled: true
                    onClicked: Chat.newSession()
                }
            }

            // 中：会话切换器（标题 + ▾，点击弹列表）
            Item {
                id: sessionSwitcher
                anchors.centerIn: parent
                width: Math.min(parent.width - 150,
                                titleTxt.implicitWidth
                                + arrowTxt.implicitWidth + 30)
                height: 30

                Rectangle {  // 悬停/弹层打开时的高亮底
                    anchors.fill: parent
                    radius: 15
                    color: switchMa.containsMouse || sessionPopup.visible
                           ? "#f0eeea" : "#00000000"
                }
                Text {
                    id: titleTxt
                    anchors.left: parent.left
                    anchors.leftMargin: 12
                    anchors.verticalCenter: parent.verticalCenter
                    width: parent.width - arrowTxt.implicitWidth - 28
                    text: Chat.sessionTitle
                    font.pixelSize: 14
                    font.bold: true
                    color: root.cPetText
                    elide: Text.ElideRight
                }
                Text {
                    id: arrowTxt
                    anchors.right: parent.right
                    anchors.rightMargin: 10
                    anchors.verticalCenter: parent.verticalCenter
                    text: sessionPopup.visible ? "▴" : "▾"
                    font.pixelSize: 11
                    color: "#9a948c"
                }
                MouseArea {
                    id: switchMa
                    anchors.fill: parent
                    cursorShape: Qt.PointingHandCursor
                    hoverEnabled: true
                    onClicked: sessionPopup.visible
                                ? sessionPopup.close()
                                : sessionPopup.open()
                }
            }

            // 会话列表弹层（顶栏层子项，居中垂下）
            Popup {
                id: sessionPopup
                x: (parent.width - width) / 2
                y: 46
                width: 300
                height: Math.min(sessionList.count * 46 + 8, 322)
                padding: 4
                background: Rectangle {
                    radius: 12
                    color: "#ffffff"
                    border.color: "#14222222"
                    border.width: 1
                    Rectangle {  // 柔影
                        anchors.fill: parent; z: -1
                        radius: parent.radius; color: "#1c222222"
                    }
                }
                contentItem: ListView {
                    id: sessionList
                    clip: true
                    spacing: 2
                    model: Chat.sessionList
                    boundsBehavior: Flickable.StopAtBounds
                    ScrollIndicator.vertical: ScrollIndicator {}

                    delegate: Rectangle {
                        id: sessRow
                        width: sessionList.width
                        height: 44
                        radius: 10
                        property bool editing: false   // v0.17.3 行内重命名态
                        color: rowMa.pressed ? "#f0eeea"
                             : rowMa.containsMouse ? "#f7f5f2"
                             : "#00000000"
                        Row {
                            anchors.verticalCenter: parent.verticalCenter
                            anchors.left: parent.left
                            width: parent.width - 24
                            anchors.leftMargin: 12
                            spacing: 8
                            // 当前会话小圆点（强调色）
                            Rectangle {
                                id: dot
                                anchors.verticalCenter: parent.verticalCenter
                                width: 6; height: 6; radius: 3
                                visible: modelData.sid === Chat.activeSid
                                color: root.cAccent
                            }
                            Text {
                                anchors.verticalCenter: parent.verticalCenter
                                width: parent.width - relTxt.implicitWidth
                                       - dot.width - 16
                                text: modelData.title
                                visible: !sessRow.editing
                                font.pixelSize: 13
                                color: modelData.sid === Chat.activeSid
                                       ? root.cAccent : root.cPetText
                                elide: Text.ElideRight
                            }
                            Text {
                                id: relTxt
                                anchors.verticalCenter: parent.verticalCenter
                                text: modelData.rel
                                visible: !sessRow.editing
                                font.pixelSize: 11
                                color: "#9a948c"
                            }
                        }
                        // 行内重命名输入框（Enter 确认 / 失焦取消——点别处
                        // 或弹层关闭不误提交）。边框外包：mac 原生样式不
                        // 支持 TextField 自绘 background（告警+不生效）
                        Rectangle {
                            visible: sessRow.editing
                            anchors.fill: parent
                            anchors.margins: 4
                            radius: 8
                            color: "#ffffff"
                            border.color: root.cAccent
                            border.width: 1
                        }
                        TextField {
                            id: renameInput
                            visible: sessRow.editing
                            anchors.fill: parent
                            anchors.margins: 6
                            font.pixelSize: 13
                            color: root.cPetText
                            selectByMouse: true
                            background: null
                            onAccepted: {
                                if (Chat.renameSession(modelData.sid, text))
                                    sessRow.editing = false
                                // 被拒（空名）保持编辑态
                            }
                            onActiveFocusChanged:
                                if (!activeFocus && sessRow.editing)
                                    sessRow.editing = false
                        }
                        // 行尾 ✎（hover 显示；点击进入编辑并全选）
                        Rectangle {
                            visible: rowMa.containsMouse && !sessRow.editing
                            anchors.right: parent.right
                            anchors.rightMargin: 6
                            anchors.verticalCenter: parent.verticalCenter
                            width: 26; height: 26; radius: 13
                            color: renMa.pressed ? "#f0e2d6" : "#00000000"
                            Text {
                                anchors.centerIn: parent
                                text: "✎"
                                font.pixelSize: 13
                                color: "#9a948c"
                            }
                            MouseArea {
                                id: renMa
                                anchors.fill: parent
                                cursorShape: Qt.PointingHandCursor
                                hoverEnabled: true
                                onClicked: {
                                    renameInput.text = modelData.title
                                    sessRow.editing = true
                                    renameInput.forceActiveFocus()
                                    renameInput.selectAll()
                                }
                            }
                        }
                        MouseArea {
                            id: rowMa
                            anchors.fill: parent
                            cursorShape: Qt.PointingHandCursor
                            hoverEnabled: true
                            // v0.17.4：右键弹菜单（mac 习惯主入口）；左键切换
                            acceptedButtons: Qt.LeftButton | Qt.RightButton
                            onClicked: function(mouse) {
                                if (mouse.button === Qt.RightButton) {
                                    // 右键时捕获行数据到 Menu 属性——Popup
                                    // 处理器里的 modelData 不可靠（离屏实测
                                    // 空 map），Item 子树此处可靠
                                    sessMenu.pendingSid = modelData.sid
                                    sessMenu.pendingTitle = modelData.title
                                    sessMenu.popup()
                                    return
                                }
                                if (Chat.switchSession(modelData.sid))
                                    sessionPopup.close()
                            }
                            Menu {
                                id: sessMenu
                                property string pendingSid: ""
                                property string pendingTitle: ""
                                MenuItem {
                                    text: "重命名"
                                    onTriggered: {
                                        // 与 ✎ 同款：预填+全选+聚焦
                                        renameInput.text = sessMenu.pendingTitle
                                        sessRow.editing = true
                                        renameInput.forceActiveFocus()
                                        renameInput.selectAll()
                                    }
                                }
                                MenuItem {
                                    text: "删除会话…"
                                    onTriggered: {
                                        deleteConfirm.pendingSid =
                                            sessMenu.pendingSid
                                        deleteConfirm.pendingTitle =
                                            sessMenu.pendingTitle
                                        deleteConfirm.open()
                                    }
                                }
                            }
                        }
                    }
                }
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

                    height: model.role === "swe" ? sweBlock.height : row.implicitHeight

                    // v0.21 swe 步骤：整宽等宽终端块（无头像，纯文本）
                    Rectangle {
                        id: sweBlock
                        visible: model.role === "swe"
                        x: 12
                        width: parent.width - 24
                        radius: 8
                        color: "#2b2b33"
                        height: sweTxt.implicitHeight + 16
                        Text {
                            id: sweTxt
                            anchors.left: parent.left
                            anchors.right: parent.right
                            anchors.leftMargin: 12
                            anchors.rightMargin: 12
                            anchors.verticalCenter: parent.verticalCenter
                            text: model.content
                            textFormat: Text.PlainText
                            wrapMode: Text.Wrap
                            color: "#d8d8e0"
                            font.family: Qt.platform.os === "osx" ? "Menlo" : "Consolas"
                            font.pixelSize: 12
                            lineHeight: 1.3
                        }
                    }

                    Row {
                        id: row
                        visible: model.role !== "swe"
                        spacing: 8
                        // pet 行贴左（头像在左）/ user 行贴右（头像在右）：
                        // layoutDirection 只反转内部顺序，行本身须锚定
                        anchors.left: model.role === "user" ? undefined : parent.left
                        anchors.right: model.role === "user" ? parent.right : undefined
                        layoutDirection: model.role === "user" ? Qt.RightToLeft : Qt.LeftToRight
                        leftPadding: 12
                        rightPadding: 12

                        // 头像位：pet=宠物立绘（缺图回退 🐱）；user=首字母圆标
                        Item {
                            id: avatar
                            width: 30; height: 30
                            anchors.verticalCenter: parent.verticalCenter

                            // user：首字母圆标
                            Rectangle {
                                visible: model.role === "user"
                                anchors.fill: parent
                                radius: 15
                                color: "#c8d4ea"
                                Text {
                                    anchors.centerIn: parent
                                    text: "我"
                                    font.pixelSize: 12
                                    color: "#44597e"
                                }
                            }
                            // pet：当前立绘（透明底等比缩放）
                            Image {
                                visible: model.role !== "user"
                                         && Chat.petAvatar !== ""
                                anchors.fill: parent
                                source: Chat.petAvatar
                                fillMode: Image.PreserveAspectFit
                                smooth: true
                                mipmap: true
                            }
                            // pet 回退：🐱 圆标
                            Rectangle {
                                visible: model.role !== "user"
                                         && Chat.petAvatar === ""
                                anchors.fill: parent
                                radius: 15
                                color: root.cAccent
                                Text {
                                    anchors.centerIn: parent
                                    text: "🐱"
                                    font.pixelSize: 18
                                    color: "white"
                                }
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
                                radius: 12  // 四角一致（左右圆润程度相同）
                                readonly property bool mine: model.role === "user"
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

                // 流式占位气泡：Chat.streamingText 非空时显示在列表底部，
                // 逐字增长（v0.4 Must "流式打字机"）。_on_done 落定后清空→隐藏。
                footer: Item {
                    width: list.width
                    height: Chat.streamingText.length > 0 ? streamRow.implicitHeight : 0
                    visible: Chat.streamingText.length > 0

                    Row {
                        id: streamRow
                        spacing: 8
                        anchors.left: parent.left
                        leftPadding: 12
                        rightPadding: 12

                        Item {  // pet 头像
                            width: 30; height: 30
                            anchors.verticalCenter: parent.verticalCenter
                            Image {
                                visible: Chat.petAvatar !== ""
                                anchors.fill: parent
                                source: Chat.petAvatar
                                fillMode: Image.PreserveAspectFit
                                smooth: true
                                mipmap: true
                            }
                            Rectangle {
                                visible: Chat.petAvatar === ""
                                anchors.fill: parent
                                radius: 15
                                color: root.cAccent
                                Text {
                                    anchors.centerIn: parent
                                    text: "🐱"
                                    font.pixelSize: 18
                                    color: "white"
                                }
                            }
                        }

                        Rectangle {
                            anchors.verticalCenter: parent.verticalCenter
                            width: Math.min(list.width - 110,
                                            streamTxt.implicitWidth + 22)
                            height: streamTxt.implicitHeight + 18
                            radius: 12
                            color: root.cPet
                            Rectangle {
                                anchors.fill: parent; z: -1
                                radius: parent.radius; color: "#14222222"
                            }
                            Text {
                                id: streamTxt
                                anchors.centerIn: parent
                                width: parent.width - 22
                                text: Chat.streamingText
                                textFormat: Text.RichText
                                wrapMode: Text.Wrap
                                color: root.cPetText
                                font.pixelSize: 13
                                lineHeight: 1.25
                            }
                        }
                    }
                }
                onContentHeightChanged: Qt.callLater(function() { list.positionViewAtEnd() })
            }
        }

        // ---- 输入区（悬浮条样式；v0.17.0 多行自适应） ----
        Rectangle {
            Layout.fillWidth: true
            // 高度随输入行数增长（微信式纵向换行），消息区被自然挤压
            Layout.preferredHeight: inputBg.height + 20
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
                    id: inputBg
                    Layout.fillWidth: true
                    // 单行时约 44（旧版观感），多行随内容增长，封顶后内滚
                    Layout.preferredHeight: Math.min(input.implicitHeight + 16,
                                                     root.inputMaxHeight)
                    radius: 18
                    color: input.activeFocus ? "#ffffff" : "#f0eeea"
                    border.width: 1
                    border.color: input.activeFocus ? root.cAccent : "#00000000"

                    ScrollView {
                        anchors.fill: parent
                        anchors.leftMargin: 14
                        anchors.rightMargin: 8

                        TextArea {
                            id: input
                            wrapMode: TextArea.Wrap
                            placeholderText: "说点什么…（Enter 发送 / Shift+Enter 换行）"
                            font.pixelSize: 13
                            color: root.cPetText
                            placeholderTextColor: "#9a948c"
                            verticalAlignment: TextEdit.AlignVCenter
                            background: null
                            focus: true
                            selectByMouse: true
                            topPadding: 0
                            bottomPadding: 0
                            leftPadding: 0
                            rightPadding: 0

                            // M4 沿用：send 被拒（在飞/离线/空文本）时保留输入
                            function send() {
                                if (text.trim().length > 0 && Chat.send(text))
                                    text = ""
                            }
                            // v0.17.0：Enter=发送 / Shift+Enter=换行（微信习惯）；
                            // IME 组字中的 Enter 是确认候选，放行不发送
                            function _handleEnter(event) {
                                if (input.inputMethodComposing) {
                                    event.accepted = false
                                    return
                                }
                                if ((event.modifiers & Qt.ShiftModifier) === 0) {
                                    input.send()
                                    event.accepted = true
                                } else {
                                    // Keys 处理器默认吞事件，须显式放行
                                    // → TextArea 默认行为才会插入换行
                                    event.accepted = false
                                }
                            }
                            Keys.onReturnPressed: function(event) {
                                input._handleEnter(event)
                            }
                            Keys.onEnterPressed: function(event) {
                                input._handleEnter(event)
                            }
                            Keys.onEscapePressed: root.hide()
                        }
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
                        onClicked: input.onAccepted()
                    }
                }
            }
        }
    }
}
