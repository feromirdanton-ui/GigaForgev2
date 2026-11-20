import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Item {
    id: root

    property string language: "ru"
    property real textScale: 1
    property real densityScale: 1
    property string editorContents: ""
    property string activePage: "forge"
    property bool projectsExpanded: false
    property bool collapsed: false
    property string renameSessionId: ""
    property string renameTitle: ""
    signal chooseProjectRequested()
    signal newChatRequested()
    signal navigateRequested(string page)
    signal collapseRequested()
    signal expandRequested()
    signal hoverLabelChanged(var target, string label, bool active)

    readonly property var visibleChats: bridge.chatSessions.filter(function(row) {
        return !root.chatFilter || String(row.title || "").toLowerCase().indexOf(root.chatFilter.toLowerCase()) >= 0
    })
    readonly property var otherProjects: bridge.recentProjects.filter(function(row) {
        return !row.active
    })
    property string chatFilter: ""

    function dateGroupLabel(key) {
        if (key === "today")
            return root.tr("СЕГОДНЯ", "TODAY")
        if (key === "yesterday")
            return root.tr("ВЧЕРА", "YESTERDAY")
        if (key === "this_week")
            return root.tr("НА ЭТОЙ НЕДЕЛЕ", "THIS WEEK")
        return root.tr("РАНЬШЕ", "EARLIER")
    }

    function beginChatRename(sessionId, title) {
        root.renameSessionId = sessionId
        root.renameTitle = String(title || "")
    }

    function finishChatRename(commit) {
        const sessionId = root.renameSessionId
        if (!sessionId)
            return
        const title = root.renameTitle.trim()
        root.renameSessionId = ""
        root.renameTitle = ""
        if (commit && title)
            bridge.renameChatSession(sessionId, title)
    }

    ThemeTokens {
        id: themeTokens
        themeName: bridge.appearanceTheme
        light: bridge.isLightTheme
        compact: bridge.density === "compact"
    }

    GlassCard {
        objectName: "sidebarSurface"
        anchors.fill: parent
        dark: true
        radius: 0
        inset: 0

        ColumnLayout {
            objectName: "sidebarContent"
            anchors.fill: parent
            visible: !root.collapsed
            anchors.leftMargin: 10 * root.densityScale
            anchors.rightMargin: 11 * root.densityScale
            anchors.topMargin: 10 * root.densityScale
            anchors.bottomMargin: 10 * root.densityScale
            spacing: 7 * root.densityScale

            RowLayout {
                objectName: "sidebarHeader"
                Layout.fillWidth: true
                spacing: 8
                Rectangle {
                    Layout.preferredWidth: 32
                    Layout.preferredHeight: 32
                    radius: 9
                    color: themeTokens.railActive
                    Text {
                        anchors.centerIn: parent
                        text: "G"
                        color: themeTokens.mintGlow
                        font.family: themeTokens.display
                        font.pixelSize: Math.round(16 * root.textScale)
                        font.bold: true
                    }
                }
                Text {
                    text: "GigaForge"
                    color: themeTokens.railInk
                    font.family: themeTokens.display
                    font.pixelSize: Math.round(14 * root.textScale)
                    font.bold: true
                    Layout.fillWidth: true
                }
                Button {
                    id: collapseButton
                    objectName: "collapseChatHistoryButton"
                    Layout.preferredWidth: 32
                    Layout.preferredHeight: 32
                    Accessible.name: root.tr("Скрыть боковую панель", "Hide sidebar")
                    onHoveredChanged: root.hoverLabelChanged(
                        collapseButton, Accessible.name, hovered)
                    onClicked: root.collapseRequested()
                    background: Rectangle {
                        radius: 9
                        color: parent.hovered ? themeTokens.railHover : "transparent"
                    }
                    contentItem: Text {
                        text: "\uE76B"
                        color: themeTokens.railInk2
                        font.family: bridge.iconFontFamily
                        font.pixelSize: Math.round(17 * root.textScale)
                        horizontalAlignment: Text.AlignHCenter
                        verticalAlignment: Text.AlignVCenter
                    }
                }

            }

            Button {
                id: newChatButton
                objectName: "newChatButton"
                Layout.fillWidth: true
                Layout.preferredHeight: 36 * root.densityScale
                enabled: !bridge.running
                Accessible.name: root.tr("Новый чат", "New chat")
                onClicked: root.newChatRequested()
                background: Rectangle {
                    radius: 10
                    color: parent.hovered ? themeTokens.railActive : themeTokens.railHover
                    border.color: themeTokens.railLine
                }
                contentItem: RowLayout {
                    spacing: 10
                    Item {
                        Layout.preferredWidth: 27
                        Layout.preferredHeight: 27
                        SmartIcon {
                            objectName: "newChatIcon"
                            anchors.fill: parent
                            kind: "chat"
                            accent: themeTokens.mintGlow
                            ring: "transparent"
                        }
                        Rectangle {
                            anchors.right: parent.right
                            anchors.bottom: parent.bottom
                            width: 11
                            height: 11
                            radius: 6
                            color: themeTokens.rail
                            border.color: themeTokens.railLine
                            Text {
                                anchors.centerIn: parent
                                text: "+"
                                color: themeTokens.mintGlow
                                font.pixelSize: Math.round(10 * root.textScale)
                                font.bold: true
                            }
                        }
                    }
                    Text {
                        text: root.tr("Новый чат", "New chat")
                        color: themeTokens.railInk
                        font.family: themeTokens.text
                        font.pixelSize: Math.round(11.5 * root.textScale)
                        font.weight: Font.DemiBold
                        Layout.fillWidth: true
                    }
                }
            }

            ColumnLayout {
                objectName: "sidebarPrimaryNavigation"
                Layout.fillWidth: true
                spacing: 2 * root.densityScale
                Repeater {
                    model: [
                        { key: "forge", label: "Чат", labelEn: "Chat", glyph: "\uE8BD" },
                        { key: "explore", label: "Deep Research", labelEn: "Deep Research", glyph: "\uE773" },
                        { key: "orchestrate", label: "Orchestrate", labelEn: "Orchestrate", glyph: "\uE8F1" }
                    ]
                    delegate: Button {
                        required property var modelData
                        objectName: "sidebarDestination_" + modelData.key
                        Layout.fillWidth: true
                        Layout.preferredHeight: 35 * root.densityScale
                        padding: 0
                        Accessible.name: root.language === "en" ? modelData.labelEn : modelData.label
                        onClicked: root.navigateRequested(modelData.key)
                        background: Rectangle {
                            radius: 9
                            color: root.activePage === modelData.key
                                ? themeTokens.railActive
                                : parent.hovered ? themeTokens.railHover : "transparent"
                            border.color: root.activePage === modelData.key
                                ? themeTokens.railLine : "transparent"
                        }
                        contentItem: RowLayout {
                            anchors.fill: parent
                            anchors.leftMargin: 12
                            anchors.rightMargin: 10
                            spacing: 10
                            Text {
                                text: modelData.glyph
                                color: root.activePage === modelData.key
                                    ? themeTokens.mintGlow : themeTokens.railInk2
                                font.family: bridge.iconFontFamily
                                font.pixelSize: Math.round(18 * root.textScale)
                            }
                            Text {
                                text: root.language === "en" ? modelData.labelEn : modelData.label
                                color: root.activePage === modelData.key
                                    ? themeTokens.railInk : themeTokens.railInk2
                                font.family: themeTokens.text
                                font.pixelSize: Math.round(10.5 * root.textScale)
                                font.bold: root.activePage === modelData.key
                                elide: Text.ElideRight
                                Layout.fillWidth: true
                            }
                        }
                    }

                }
            }

            Rectangle {
                Layout.fillWidth: true
                height: 1
                color: themeTokens.navRule
            }

            ColumnLayout {
                objectName: "sidebarProjectNavigation"
                Layout.fillWidth: true
                spacing: 2 * root.densityScale
                Repeater {
                    model: [
                        { key: "files", label: "Файлы и редактор", labelEn: "Files & editor", glyph: "\uE8A5" },
                        { key: "agents", label: "Агенты", labelEn: "Agents", glyph: "\uE902" }
                    ]
                    delegate: Button {
                        required property var modelData
                        objectName: "sidebarDestination_" + modelData.key
                        Layout.fillWidth: true
                        Layout.preferredHeight: 35 * root.densityScale
                        padding: 0
                        Accessible.name: root.language === "en" ? modelData.labelEn : modelData.label
                        onClicked: root.navigateRequested(modelData.key)
                        background: Rectangle {
                            radius: 9
                            color: root.activePage === modelData.key
                                ? themeTokens.railActive
                                : parent.hovered ? themeTokens.railHover : "transparent"
                            border.color: root.activePage === modelData.key
                                ? themeTokens.railLine : "transparent"
                        }
                        contentItem: RowLayout {
                            anchors.fill: parent
                            anchors.leftMargin: 12
                            anchors.rightMargin: 10
                            spacing: 10
                            Item {
                                Layout.preferredWidth: 24
                                Layout.preferredHeight: 24
                                SmartIcon {
                                    objectName: modelData.key === "files" ? "notebookIcon" : ""
                                    anchors.fill: parent
                                    visible: modelData.key === "files"
                                    kind: "notebook"
                                    accent: root.activePage === modelData.key
                                        ? themeTokens.mintGlow : themeTokens.railInk2
                                    ring: "transparent"
                                }
                                Text {
                                    anchors.fill: parent
                                    visible: modelData.key !== "files"
                                    text: modelData.glyph
                                    color: root.activePage === modelData.key
                                        ? themeTokens.mintGlow : themeTokens.railInk2
                                    font.family: bridge.iconFontFamily
                                    font.pixelSize: Math.round(19 * root.textScale)
                                    verticalAlignment: Text.AlignVCenter
                                }
                            }
                            Text {
                                text: root.language === "en" ? modelData.labelEn : modelData.label
                                color: root.activePage === modelData.key
                                    ? themeTokens.railInk : themeTokens.railInk2
                                font.family: themeTokens.text
                                font.pixelSize: Math.round(10.5 * root.textScale)
                                font.bold: root.activePage === modelData.key
                                elide: Text.ElideRight
                                Layout.fillWidth: true
                            }
                        }
                    }
                }
            }

            Rectangle {
                Layout.fillWidth: true
                height: 1
                color: themeTokens.navRule
            }

            RowLayout {
                Layout.fillWidth: true
                spacing: 4
                Text {
                    text: root.tr("ПРОЕКТЫ", "PROJECTS")
                    color: themeTokens.railInk2
                    font.family: themeTokens.mono
                    font.pixelSize: Math.round(9.5 * root.textScale)
                    font.letterSpacing: 1
                    Layout.fillWidth: true
                }
                Text {
                    text: bridge.recentProjects.length
                    color: themeTokens.railInk2
                    font.family: themeTokens.mono
                    font.pixelSize: Math.round(9.5 * root.textScale)
                }
                Button {
                    id: addProjectButton
                    objectName: "addProjectButton"
                    text: "+"
                    Accessible.name: root.tr("Открыть или создать проект", "Open or create a project")
                    onHoveredChanged: root.hoverLabelChanged(
                        addProjectButton, Accessible.name, hovered)
                    enabled: !bridge.running && !bridge.terminalBusy
                    onClicked: root.chooseProjectRequested()
                    Layout.preferredWidth: 34
                    Layout.preferredHeight: 30 * root.densityScale
                    background: Rectangle {
                        radius: 9
                        color: parent.hovered ? themeTokens.railActive : themeTokens.railHover
                        border.color: themeTokens.railLine
                    }
                    contentItem: Text {
                        text: parent.text
                        color: themeTokens.mintGlow
                        font.pixelSize: Math.round(18 * root.textScale)
                        font.weight: Font.Medium
                        horizontalAlignment: Text.AlignHCenter
                        verticalAlignment: Text.AlignVCenter
                    }
                }
            }

            Button {
                id: activeProjectButton
                objectName: "activeProjectButton"
                Layout.fillWidth: true
                Layout.preferredHeight: 50 * root.densityScale
                enabled: root.otherProjects.length > 0
                onClicked: root.projectsExpanded = !root.projectsExpanded
                Accessible.name: root.tr("Переключить недавние проекты", "Toggle recent projects")
                onHoveredChanged: root.hoverLabelChanged(
                    activeProjectButton, bridge.projectPath, hovered)
                background: Rectangle {
                    radius: 11
                    color: root.projectsExpanded ? themeTokens.railActive : themeTokens.railHover
                    border.color: root.projectsExpanded ? themeTokens.railLine : "transparent"
                }
                contentItem: RowLayout {
                    anchors.fill: parent
                    anchors.leftMargin: 10
                    anchors.rightMargin: 9
                    spacing: 9
                    Rectangle {
                        Layout.preferredWidth: 28
                        Layout.preferredHeight: 28
                        radius: 9
                        color: themeTokens.railActive
                        Text {
                            anchors.centerIn: parent
                            text: "▣"
                            color: themeTokens.mintGlow
                            font.pixelSize: Math.round(16 * root.textScale)
                        }
                    }
                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: 1
                        Text {
                            text: bridge.projectName
                            color: themeTokens.railInk
                            font.family: themeTokens.text
                            font.pixelSize: Math.round(10.5 * root.textScale)
                            font.bold: true
                            elide: Text.ElideRight
                            Layout.fillWidth: true
                        }
                        Text {
                            text: root.tr("Текущий проект", "Current project")
                            color: themeTokens.railInk2
                            font.family: themeTokens.text
                            font.pixelSize: Math.round(8.5 * root.textScale)
                            Layout.fillWidth: true
                        }
                    }
                    Text {
                        text: root.otherProjects.length
                            ? root.projectsExpanded ? "⌃" : "⌄"
                            : "·"
                        color: themeTokens.railInk2
                        font.pixelSize: 15
                    }
                }
            }

            ListView {
                id: projectList
                objectName: "recentProjectList"
                Layout.fillWidth: true
                Layout.preferredHeight: root.projectsExpanded && root.otherProjects.length
                    ? Math.min(124, Math.max(36, root.otherProjects.length * 34))
                    : 0
                visible: root.projectsExpanded && root.otherProjects.length > 0
                clip: true
                spacing: 2
                model: root.otherProjects
                boundsBehavior: Flickable.StopAtBounds
                ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

                delegate: Button {
                    required property var modelData
                    required property int index
                    objectName: "projectFolder_" + index
                    width: projectList.width
                    height: 32 * root.densityScale
                    padding: 5
                    enabled: !modelData.active && !bridge.running && !bridge.terminalBusy
                    Accessible.name: modelData.name + (modelData.active ? root.tr(" · открыт", " · active") : "")
                    onClicked: {
                        bridge.openRecentProject(modelData.path, root.editorContents)
                        root.projectsExpanded = false
                    }
                    background: Rectangle {
                        radius: 9
                        color: modelData.active ? themeTokens.railActive : parent.hovered ? themeTokens.railHover : "transparent"
                        border.color: modelData.active ? themeTokens.railLine : "transparent"
                        border.width: 1
                    }
                    contentItem: RowLayout {
                        spacing: 8
                        Rectangle {
                            Layout.preferredWidth: 20
                            Layout.preferredHeight: 20
                            radius: 6
                            color: modelData.active ? themeTokens.railHover : themeTokens.rail2
                            Text {
                                anchors.centerIn: parent
                                text: "▣"
                                color: modelData.active ? themeTokens.mintGlow : themeTokens.railInk2
                                font.pixelSize: Math.round(11 * root.textScale)
                            }
                        }
                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 0
                            Text {
                                text: modelData.name
                                color: modelData.active ? themeTokens.railInk : themeTokens.railInk2
                                font.family: themeTokens.text
                                font.pixelSize: Math.round(9.8 * root.textScale)
                                font.bold: modelData.active
                                elide: Text.ElideRight
                                Layout.fillWidth: true
                            }
                            Text {
                                text: modelData.path
                                color: themeTokens.railInk2
                                font.family: themeTokens.mono
                                font.pixelSize: Math.round(8 * root.textScale)
                                elide: Text.ElideMiddle
                                Layout.fillWidth: true
                            }
                        }
                    }
                }
            }

            Rectangle {
                Layout.fillWidth: true
                height: 1
                color: themeTokens.navRule
            }

            RowLayout {
                Layout.fillWidth: true
                spacing: 4
                Text {
                    text: root.tr("ИСТОРИЯ", "HISTORY")
                    color: themeTokens.railInk2
                    font.family: themeTokens.mono
                    font.pixelSize: Math.round(9.5 * root.textScale)
                    font.letterSpacing: 1
                    Layout.fillWidth: true
                }
                Text {
                    text: bridge.chatSessions.length
                    color: themeTokens.railInk2
                    font.family: themeTokens.mono
                    font.pixelSize: Math.round(9.5 * root.textScale)
                }
            }

            TextField {
                id: historySearch
                Layout.fillWidth: true
                Layout.preferredHeight: 30 * root.densityScale
                placeholderText: root.tr("Найти чат…", "Find a chat…")
                text: root.chatFilter
                onTextChanged: root.chatFilter = text.trim()
                selectByMouse: true
                color: themeTokens.railInk
                placeholderTextColor: themeTokens.railInk2
                font.family: themeTokens.text
                font.pixelSize: Math.round(11 * root.textScale)
                leftPadding: 9
                background: Rectangle {
                    radius: 8
                    color: themeTokens.rail2
                    border.color: historySearch.activeFocus ? themeTokens.mintGlow : themeTokens.railLine
                }
            }

            ListView {
                id: chatList
                objectName: "chatHistoryList"
                Layout.fillWidth: true
                Layout.fillHeight: true
                clip: true
                spacing: 1 * root.densityScale
                model: root.visibleChats
                boundsBehavior: Flickable.StopAtBounds
                ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
                section.property: "dateGroup"
                section.criteria: ViewSection.FullString
                section.delegate: Item {
                    objectName: "chatHistoryDateSection"
                    required property string section
                    width: chatList.width
                    height: 21 * root.densityScale
                    Text {
                        anchors.left: parent.left
                        anchors.right: parent.right
                        anchors.bottom: parent.bottom
                        anchors.leftMargin: 7
                        text: root.dateGroupLabel(parent.section)
                        color: themeTokens.railInk2
                        font.family: themeTokens.mono
                        font.pixelSize: Math.round(8 * root.textScale)
                        font.letterSpacing: 0.8
                        verticalAlignment: Text.AlignVCenter
                    }
                }

                delegate: Button {
                    id: chatRowButton
                    required property var modelData
                    objectName: "chatHistory_" + modelData.id
                    width: chatList.width
                    height: 35 * root.densityScale
                    padding: 2 * root.densityScale
                    enabled: !bridge.running
                    Accessible.name: modelData.title
                    onClicked: bridge.openChatSession(modelData.id)
                    background: Rectangle {
                        radius: 10
                        color: modelData.active ? themeTokens.railActive : parent.hovered ? themeTokens.railHover : "transparent"
                        border.color: modelData.active ? themeTokens.railLine : "transparent"
                        border.width: 1
                        Rectangle {
                            width: 3
                            anchors.left: parent.left
                            anchors.top: parent.top
                            anchors.bottom: parent.bottom
                            radius: 2
                            color: modelData.active ? themeTokens.mintGlow : "transparent"
                        }
                    }
                    contentItem: RowLayout {
                        anchors.fill: parent
                        anchors.margins: 3 * root.densityScale
                        anchors.leftMargin: 7 * root.densityScale
                        spacing: 4 * root.densityScale
                        TextField {
                            objectName: root.renameSessionId === modelData.id
                                ? "renameChatTitleField" : ""
                            visible: root.renameSessionId === modelData.id
                            enabled: visible && !bridge.running
                            text: root.renameTitle
                            maximumLength: 80
                            selectByMouse: true
                            color: themeTokens.railInk
                            font.family: themeTokens.text
                            font.pixelSize: Math.round(10.5 * root.textScale)
                            leftPadding: 5
                            rightPadding: 5
                            topPadding: 2
                            bottomPadding: 2
                            Layout.fillWidth: true
                            Layout.minimumWidth: 0
                            background: Rectangle {
                                radius: 5
                                color: themeTokens.rail2
                                border.color: parent.activeFocus
                                    ? themeTokens.mintGlow : themeTokens.railLine
                            }
                            onVisibleChanged: {
                                if (visible) {
                                    text = root.renameTitle
                                    forceActiveFocus()
                                    selectAll()
                                }
                            }
                            onTextChanged: {
                                if (visible)
                                    root.renameTitle = text
                            }
                            onAccepted: root.finishChatRename(true)
                            onEditingFinished: {
                                if (visible)
                                    root.finishChatRename(true)
                            }
                            Keys.onPressed: function(event) {
                                if (event.key === Qt.Key_Escape) {
                                    root.finishChatRename(false)
                                    event.accepted = true
                                }
                            }
                        }
                        Text {
                            objectName: "chatTitleText_" + modelData.id
                            visible: root.renameSessionId !== modelData.id
                            text: modelData.title
                            color: modelData.active ? themeTokens.railInk : themeTokens.railInk2
                            font.family: themeTokens.text
                            font.pixelSize: Math.round(11 * root.textScale)
                            font.bold: modelData.active
                            elide: Text.ElideRight
                            Layout.fillWidth: true
                            Layout.minimumWidth: 0
                        }
                        Text {
                            text: modelData.count
                            color: themeTokens.railInk2
                            font.family: themeTokens.mono
                            font.pixelSize: Math.round(8.5 * root.textScale)
                            visible: !chatRowButton.hovered && !renameChatButton.hovered
                                && root.renameSessionId !== modelData.id
                        }
                        Button {
                            id: renameChatButton
                            objectName: "renameChatButton_" + modelData.id
                            Layout.preferredWidth: 22
                            Layout.preferredHeight: 24
                            visible: root.renameSessionId !== modelData.id
                            opacity: chatRowButton.hovered || hovered ? 1 : 0
                            enabled: !bridge.running && opacity > 0
                            Accessible.name: root.tr("Переименовать чат", "Rename chat")
                            onClicked: {
                                root.beginChatRename(modelData.id, modelData.title)
                            }
                            background: Rectangle {
                                radius: 6
                                color: parent.hovered ? themeTokens.railActive : "transparent"
                            }
                            contentItem: Text {
                                text: "\uE70F"
                                color: themeTokens.railInk
                                font.family: bridge.iconFontFamily
                                font.pixelSize: Math.round(13 * root.textScale)
                                horizontalAlignment: Text.AlignHCenter
                                verticalAlignment: Text.AlignVCenter
                            }
                        }
                    }
                }

                Text {
                    anchors.centerIn: parent
                    width: parent.width - 18
                    visible: chatList.count === 0
                    text: root.chatFilter
                        ? root.tr("Совпадений нет", "No matches")
                        : root.tr("Здесь появятся сохранённые диалоги проекта", "Saved project chats will appear here")
                    color: themeTokens.railInk2
                    font.family: themeTokens.text
                    font.pixelSize: Math.round(9.5 * root.textScale)
                    horizontalAlignment: Text.AlignHCenter
                    wrapMode: Text.WordWrap
                }
            }
        }

        Button {
            id: expandButton
            objectName: "expandChatHistoryButton"
            visible: root.collapsed
            anchors.horizontalCenter: parent.horizontalCenter
            anchors.top: parent.top
            anchors.topMargin: 8 * root.densityScale
            width: 32
            height: 34
            Accessible.name: root.tr("Развернуть боковую панель", "Expand sidebar")
            onHoveredChanged: root.hoverLabelChanged(expandButton, Accessible.name, hovered)
            onClicked: root.expandRequested()
            background: Rectangle {
                radius: 9
                color: parent.hovered ? themeTokens.railHover : "transparent"
            }
            contentItem: Text {
                text: "\uE76C"
                color: themeTokens.railInk2
                font.family: bridge.iconFontFamily
                font.pixelSize: Math.round(14 * root.textScale)
                horizontalAlignment: Text.AlignHCenter
                verticalAlignment: Text.AlignVCenter
            }
        }

        Rectangle {
            anchors.top: parent.top
            anchors.bottom: parent.bottom
            anchors.right: parent.right
            width: 1
            color: themeTokens.navRule
        }
    }

    function tr(ru, en) {
        return root.language === "en" ? en : ru
    }
}
