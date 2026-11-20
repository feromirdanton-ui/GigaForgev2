import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Item {
    id: root

    signal sourceRequested(string path)
    property string language: "ru"
    property real textScale: 1
    property real densityScale: 1
    property string selectedTopicId: ""
    property string viewMode: "research"

    readonly property var topics: bridge.researchTopics
    readonly property var library: bridge.researchLibrary
    readonly property var selectedTopic: {
        const rows = root.topics
        if (!rows.length)
            return null
        const selected = rows.find(function(row) { return row.id === root.selectedTopicId })
        return selected || rows[0]
    }
    readonly property var graphData: buildGraphData()
    readonly property bool anyResearchRunning: root.topics.some(function(row) {
        return row.state === "searching"
    })

    ThemeTokens {
        id: themeTokens
        themeName: bridge.appearanceTheme
        light: bridge.isLightTheme
        compact: bridge.density === "compact"
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 12 * root.densityScale
        spacing: 7 * root.densityScale

        RowLayout {
            Layout.fillWidth: true
            spacing: 8
            Rectangle {
                Layout.preferredWidth: 32
                Layout.preferredHeight: 32
                radius: 11
                color: themeTokens.aquaTint
                Text {
                    anchors.centerIn: parent
                    text: "⌕"
                    color: themeTokens.aquaInk
                    font.pixelSize: Math.round(21 * root.textScale)
                }
            }
            ColumnLayout {
                Layout.fillWidth: true
                spacing: 1
                Text {
                    text: root.tr("ПАРАЛЛЕЛЬНЫЙ ПОИСК ПО ПРОЕКТУ", "PARALLEL PROJECT RESEARCH")
                    color: themeTokens.ink3
                    font.family: themeTokens.mono
                    font.pixelSize: Math.round(8.5 * root.textScale)
                    font.letterSpacing: 1
                }
                Text {
                    text: root.tr(
                        "Ищите по нескольким темам и собирайте связанные источники в библиотеку.",
                        "Search multiple topics and collect connected sources in a knowledge library."
                    )
                    color: themeTokens.ink2
                    font.family: themeTokens.text
                    font.pixelSize: Math.round(10 * root.textScale)
                    elide: Text.ElideRight
                    Layout.fillWidth: true
                }
            }
            Text {
                text: root.tr("до 4 одновременно", "up to 4 at once")
                color: themeTokens.ink4
                font.family: themeTokens.mono
                font.pixelSize: Math.round(8 * root.textScale)
                visible: root.width >= 720
            }
        }

        GlassCard {
            Layout.fillWidth: true
            Layout.preferredHeight: 52 * root.densityScale
            inset: 7
            RowLayout {
                anchors.fill: parent
                spacing: 7 * root.densityScale
                Rectangle {
                    Layout.preferredWidth: 28
                    Layout.preferredHeight: 28
                    radius: 9
                    color: themeTokens.primaryTint
                    Text {
                        anchors.centerIn: parent
                        text: "⌕"
                        color: themeTokens.primaryDeep
                        font.pixelSize: Math.round(20 * root.textScale)
                    }
                }
                TextField {
                    id: researchQuery
                    objectName: "researchQuery"
                    Layout.fillWidth: true
                    placeholderText: root.tr(
                        "Добавьте тему или вопрос для отдельного поиска…",
                        "Add a topic or question as a separate search…"
                    )
                    selectByMouse: true
                    color: themeTokens.ink
                    placeholderTextColor: themeTokens.ink3
                    font.family: themeTokens.text
                    font.pixelSize: Math.round(11.5 * root.textScale)
                    background: Rectangle {
                        radius: themeTokens.radiusControl
                        color: themeTokens.surface
                        border.color: researchQuery.activeFocus ? themeTokens.primary : themeTokens.controlLine
                    }
                    Keys.onReturnPressed: root.startQuery()
                }
                Button {
                    objectName: "startResearchButton"
                    text: root.tr("Исследовать", "Research")
                    enabled: researchQuery.text.trim().length >= 2 && !bridge.running
                    onClicked: root.startQuery()
                    Layout.preferredHeight: 32
                    background: Rectangle {
                        radius: themeTokens.radiusControl
                        color: parent.enabled ? themeTokens.primary : themeTokens.surface3
                        border.color: parent.enabled ? themeTokens.lineStrong : themeTokens.line
                    }
                    contentItem: Text {
                        text: parent.text
                        color: parent.enabled ? themeTokens.actionInk : themeTokens.ink4
                        font.family: themeTokens.text
                        font.pixelSize: Math.round(9.5 * root.textScale)
                        font.bold: true
                        horizontalAlignment: Text.AlignHCenter
                        verticalAlignment: Text.AlignVCenter
                        leftPadding: 11
                        rightPadding: 11
                    }
                }
            }
        }

        RowLayout {
            Layout.fillWidth: true
            spacing: 6
            Repeater {
                model: [
                    { key: "research", ru: "Исследования", en: "Research", count: root.topics.length },
                    { key: "library", ru: "Библиотека", en: "Library", count: root.library.length }
                ]
                delegate: Button {
                    required property var modelData
                    onClicked: root.viewMode = modelData.key
                    Layout.preferredHeight: 28
                    background: Rectangle {
                        radius: themeTokens.radiusPill
                        color: root.viewMode === modelData.key ? themeTokens.primaryTint : themeTokens.surface
                        border.color: root.viewMode === modelData.key ? themeTokens.lineStrong : themeTokens.line
                    }
                    contentItem: Text {
                        text: (root.language === "en" ? modelData.en : modelData.ru) + "  " + modelData.count
                        color: root.viewMode === modelData.key ? themeTokens.primaryDeep : themeTokens.ink3
                        font.family: themeTokens.text
                        font.pixelSize: Math.round(9 * root.textScale)
                        font.bold: root.viewMode === modelData.key
                        horizontalAlignment: Text.AlignHCenter
                        verticalAlignment: Text.AlignVCenter
                        leftPadding: 10
                        rightPadding: 10
                    }
                }
            }
            Item { Layout.fillWidth: true }
            Text {
                text: root.tr(
                    "Поиск по текстам доступных файлов · секретные пути исключены",
                    "Searches accessible project text · sensitive paths are excluded"
                )
                color: themeTokens.ink4
                font.family: themeTokens.text
                font.pixelSize: Math.round(8 * root.textScale)
                elide: Text.ElideRight
                Layout.maximumWidth: root.width * 0.58
                visible: root.width >= 650
            }
        }

        ListView {
            id: researchLanes
            visible: root.viewMode === "research" && root.topics.length > 0
            Layout.fillWidth: true
            Layout.preferredHeight: visible ? 38 * root.densityScale : 0
            clip: true
            orientation: ListView.Horizontal
            spacing: 6
            model: root.topics
            boundsBehavior: Flickable.StopAtBounds

            delegate: Button {
                required property var modelData
                required property int index
                objectName: "researchLane_" + modelData.id
                width: Math.min(220, Math.max(140, laneLabel.implicitWidth + 54))
                height: 34
                padding: 6
                onClicked: root.selectedTopicId = modelData.id
                background: Rectangle {
                    radius: 10
                    color: root.selectedTopic && root.selectedTopic.id === modelData.id
                        ? themeTokens.surface2 : themeTokens.surface
                    border.color: root.selectedTopic && root.selectedTopic.id === modelData.id
                        ? themeTokens.lineStrong : themeTokens.line
                }
                contentItem: RowLayout {
                    spacing: 6
                    Rectangle {
                        Layout.preferredWidth: 6
                        Layout.preferredHeight: 6
                        radius: 3
                        color: modelData.state === "searching" ? themeTokens.aqua
                            : modelData.state === "error" ? themeTokens.fail
                            : modelData.state === "ready" ? themeTokens.mint
                            : themeTokens.ink4
                    }
                    Text {
                        id: laneLabel
                        text: modelData.query
                        color: themeTokens.ink2
                        font.family: themeTokens.text
                        font.pixelSize: Math.round(9 * root.textScale)
                        font.bold: root.selectedTopic && root.selectedTopic.id === modelData.id
                        elide: Text.ElideRight
                        Layout.fillWidth: true
                    }
                    Text {
                        text: modelData.state === "searching"
                            ? "···" : String((modelData.results || []).length)
                        color: themeTokens.ink4
                        font.family: themeTokens.mono
                        font.pixelSize: Math.round(8 * root.textScale)
                    }
                }
            }
        }

        GridLayout {
            id: researchGrid
            Layout.fillWidth: true
            Layout.fillHeight: true
            columns: root.width >= 860 ? 2 : 1
            columnSpacing: 9 * root.densityScale
            rowSpacing: 8 * root.densityScale

            GlassCard {
                id: sourcePanel
                objectName: "researchSourcePanel"
                Layout.fillWidth: true
                Layout.fillHeight: true
                Layout.preferredWidth: root.width >= 860 ? root.width * 0.56 : root.width
                Layout.preferredHeight: root.width >= 860
                    ? -1 : Math.max(104, (root.height - 155) * 0.58)
                inset: 0

                ColumnLayout {
                    anchors.fill: parent
                    anchors.margins: 10 * root.densityScale
                    spacing: 6 * root.densityScale

                    RowLayout {
                        Layout.fillWidth: true
                        Text {
                            text: root.viewMode === "library"
                                ? root.tr("СОХРАНЁННЫЕ ИСТОЧНИКИ", "SAVED SOURCES")
                                : root.tr("ИСТОЧНИКИ ИССЛЕДОВАНИЯ", "RESEARCH SOURCES")
                            color: themeTokens.ink3
                            font.family: themeTokens.mono
                            font.pixelSize: Math.round(8.5 * root.textScale)
                            font.letterSpacing: 1
                        }
                        Item { Layout.fillWidth: true }
                        Text {
                            visible: root.viewMode === "research" && root.selectedTopic !== null
                            text: root.selectedTopic
                                ? String((root.selectedTopic.results || []).length) + root.tr(" источников", " sources")
                                : ""
                            color: themeTokens.ink4
                            font.family: themeTokens.mono
                            font.pixelSize: Math.round(8 * root.textScale)
                        }
                        Button {
                            visible: root.viewMode === "research" && root.selectedTopic !== null
                            text: root.tr("Убрать", "Remove")
                            onClicked: {
                                bridge.removeResearchTopic(root.selectedTopic.id)
                                root.selectedTopicId = ""
                            }
                            background: Rectangle { radius: 7; color: parent.hovered ? themeTokens.failTint : "transparent" }
                            contentItem: Text { text: parent.text; color: themeTokens.ink4; font.family: themeTokens.text; font.pixelSize: Math.round(8 * root.textScale); horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; leftPadding: 5; rightPadding: 5 }
                        }
                    }

                    ListView {
                        id: resultsList
                        visible: root.viewMode === "research"
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        clip: true
                        spacing: 3
                        boundsBehavior: Flickable.StopAtBounds
                        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
                        model: root.selectedTopic ? root.selectedTopic.results : []

                        delegate: Item {
                            required property var modelData
                            width: resultsList.width
                            height: 64
                            Rectangle {
                                anchors.fill: parent
                                radius: 9
                                color: sourceRowHover.hovered ? themeTokens.surface2 : "transparent"
                            }
                            HoverHandler { id: sourceRowHover }
                            RowLayout {
                                anchors.fill: parent
                                anchors.margins: 7
                                spacing: 7
                                Rectangle {
                                    Layout.preferredWidth: 26
                                    Layout.preferredHeight: 26
                                    radius: 8
                                    color: themeTokens.aquaTint
                                    Text {
                                        anchors.centerIn: parent
                                        text: String(modelData.suffix || "·").replace(".", "").slice(0, 2).toUpperCase()
                                        color: themeTokens.aquaInk
                                        font.family: themeTokens.mono
                                        font.pixelSize: Math.round(8 * root.textScale)
                                        font.bold: true
                                    }
                                }
                                ColumnLayout {
                                    Layout.fillWidth: true
                                    spacing: 2
                                    Text {
                                        text: modelData.path + ":" + modelData.line
                                        color: themeTokens.primaryDeep
                                        font.family: themeTokens.mono
                                        font.pixelSize: Math.round(8 * root.textScale)
                                        elide: Text.ElideMiddle
                                        Layout.fillWidth: true
                                    }
                                    Text {
                                        text: modelData.text
                                        color: themeTokens.ink2
                                        font.family: themeTokens.text
                                        font.pixelSize: Math.round(9 * root.textScale)
                                        wrapMode: Text.Wrap
                                        maximumLineCount: 2
                                        elide: Text.ElideRight
                                        Layout.fillWidth: true
                                    }
                                }
                                Button {
                                    text: modelData.saved ? "★" : "☆"
                                    Accessible.name: modelData.saved
                                        ? root.tr("Убрать из библиотеки", "Remove from library")
                                        : root.tr("Сохранить в библиотеку", "Save to library")
                                    ToolTip.visible: hovered
                                    ToolTip.text: Accessible.name
                                    onClicked: bridge.toggleResearchSource(root.selectedTopic.id, modelData.path)
                                    Layout.preferredWidth: 28
                                    Layout.preferredHeight: 28
                                    background: Rectangle {
                                        radius: 8
                                        color: modelData.saved ? themeTokens.mintTint : "transparent"
                                    }
                                    contentItem: Text {
                                        text: parent.text
                                        color: modelData.saved ? themeTokens.mintDeep : themeTokens.ink4
                                        font.pixelSize: Math.round(15 * root.textScale)
                                        horizontalAlignment: Text.AlignHCenter
                                        verticalAlignment: Text.AlignVCenter
                                    }
                                }
                                Button {
                                    text: "↗"
                                    Accessible.name: root.tr("Открыть источник в редакторе проекта", "Open source in the project editor")
                                    onClicked: root.sourceRequested(modelData.path)
                                    Layout.preferredWidth: 26
                                    Layout.preferredHeight: 28
                                    background: Rectangle { radius: 8; color: parent.hovered ? themeTokens.primaryTint : "transparent" }
                                    contentItem: Text { text: parent.text; color: themeTokens.primaryDeep; font.pixelSize: Math.round(14 * root.textScale); horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter }
                                }
                            }
                        }
                    }

                    ListView {
                        id: libraryList
                        visible: root.viewMode === "library"
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        clip: true
                        spacing: 4
                        boundsBehavior: Flickable.StopAtBounds
                        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
                        model: root.library

                        delegate: GlassCard {
                            required property var modelData
                            Layout.fillWidth: true
                            Layout.preferredHeight: 78
                            inset: 6
                            ColumnLayout {
                                anchors.fill: parent
                                spacing: 3
                                RowLayout {
                                    Layout.fillWidth: true
                                    Text {
                                        text: modelData.path
                                        color: themeTokens.primaryDeep
                                        font.family: themeTokens.mono
                                        font.pixelSize: Math.round(8.5 * root.textScale)
                                        elide: Text.ElideMiddle
                                        Layout.fillWidth: true
                                    }
                                    Button {
                                        text: "↗"
                                        Accessible.name: root.tr("Открыть источник", "Open source")
                                        onClicked: root.sourceRequested(modelData.path)
                                        Layout.preferredWidth: 24
                                        Layout.preferredHeight: 23
                                        background: Rectangle { radius: 7; color: parent.hovered ? themeTokens.primaryTint : "transparent" }
                                        contentItem: Text { text: parent.text; color: themeTokens.primaryDeep; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter }
                                    }
                                    Button {
                                        text: "×"
                                        Accessible.name: root.tr("Убрать из библиотеки", "Remove from library")
                                        onClicked: bridge.removeResearchSource(modelData.path)
                                        Layout.preferredWidth: 24
                                        Layout.preferredHeight: 23
                                        background: Rectangle { radius: 7; color: parent.hovered ? themeTokens.failTint : "transparent" }
                                        contentItem: Text { text: parent.text; color: themeTokens.fail; font.pixelSize: 15; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter }
                                    }
                                }
                                TextField {
                                    Layout.fillWidth: true
                                    Layout.preferredHeight: 30
                                    placeholderText: root.tr("Короткая заметка к источнику…", "Add a short source note…")
                                    text: modelData.note
                                    selectByMouse: true
                                    color: themeTokens.ink2
                                    placeholderTextColor: themeTokens.ink4
                                    font.family: themeTokens.text
                                    font.pixelSize: Math.round(9 * root.textScale)
                                    onEditingFinished: bridge.setResearchNote(modelData.path, text)
                                    background: Rectangle { radius: 7; color: themeTokens.surface; border.color: themeTokens.line }
                                }
                            }
                        }

                        Text {
                            anchors.centerIn: parent
                            width: parent.width - 24
                            visible: libraryList.count === 0
                            text: root.tr("Сохраняйте важные источники звёздочкой в результатах.", "Save useful sources with the star button in the results.")
                            color: themeTokens.ink4
                            font.family: themeTokens.text
                            font.pixelSize: Math.round(10 * root.textScale)
                            horizontalAlignment: Text.AlignHCenter
                            wrapMode: Text.WordWrap
                        }
                    }

                    Item {
                        visible: root.viewMode === "research"
                            && root.selectedTopic !== null
                            && ["searching", "empty", "error", "interrupted"].indexOf(root.selectedTopic.state) >= 0
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        ColumnLayout {
                            anchors.centerIn: parent
                            width: Math.min(parent.width - 28, 440)
                            spacing: 8
                            BusyIndicator {
                                visible: root.selectedTopic && root.selectedTopic.state === "searching"
                                running: visible
                                Layout.alignment: Qt.AlignHCenter
                                Layout.preferredWidth: 24
                                Layout.preferredHeight: 24
                            }
                            Text {
                                Layout.fillWidth: true
                                text: root.selectedTopic && root.selectedTopic.state === "searching"
                                    ? root.tr("Ищу в доступных текстах проекта…", "Searching accessible project text…")
                                    : root.selectedTopic && root.selectedTopic.state === "error"
                                        ? String(root.selectedTopic.error || root.tr("Поиск не завершился.", "Search did not finish."))
                                        : root.selectedTopic && root.selectedTopic.state === "interrupted"
                                            ? root.tr("Поиск прервался при закрытии приложения.", "The search was interrupted when the app closed.")
                                            : root.selectedTopic
                                                ? root.tr("Совпадений нет. Измените формулировку или повторите поиск.", "No matches. Refine the query or run it again.")
                                                : root.tr("Добавьте вопрос: каждая тема станет отдельным поиском.", "Add a question; each topic gets its own search.")
                                color: themeTokens.ink3
                                font.family: themeTokens.text
                                font.pixelSize: Math.round(10 * root.textScale)
                                horizontalAlignment: Text.AlignHCenter
                                wrapMode: Text.WordWrap
                            }
                            Button {
                                visible: root.selectedTopic
                                    && ["empty", "error", "interrupted"].indexOf(root.selectedTopic.state) >= 0
                                text: root.tr("Повторить поиск", "Search again")
                                onClicked: root.startQuery(root.selectedTopic.query)
                                Layout.alignment: Qt.AlignHCenter
                                background: Rectangle { radius: 8; color: themeTokens.primaryTint; border.color: themeTokens.lineStrong }
                                contentItem: Text { text: parent.text; color: themeTokens.primaryDeep; font.family: themeTokens.text; font.pixelSize: Math.round(9 * root.textScale); horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; leftPadding: 10; rightPadding: 10 }
                            }
                        }
                    }
                }
            }

            GlassCard {
                id: graphPanel
                objectName: "researchKnowledgeGraph"
                Layout.fillWidth: true
                Layout.fillHeight: true
                Layout.preferredWidth: root.width >= 860 ? root.width * 0.40 : root.width
                Layout.preferredHeight: root.width >= 860
                    ? -1 : Math.max(82, (root.height - 155) * 0.42)
                inset: 0

                ColumnLayout {
                    anchors.fill: parent
                    anchors.margins: 9 * root.densityScale
                    spacing: 4
                    RowLayout {
                        Layout.fillWidth: true
                        Text {
                            text: root.tr("ГРАФ ЗНАНИЙ", "KNOWLEDGE GRAPH")
                            color: themeTokens.ink3
                            font.family: themeTokens.mono
                            font.pixelSize: Math.round(8.5 * root.textScale)
                            font.letterSpacing: 1
                        }
                        Item { Layout.fillWidth: true }
                        Text {
                            text: root.viewMode === "library"
                                ? root.tr("ссылки в документах", "document links")
                                : root.tr("совпадения · ссылки", "matches · links")
                            color: themeTokens.ink4
                            font.family: themeTokens.mono
                            font.pixelSize: Math.round(7.5 * root.textScale)
                        }
                    }

                    Item {
                        id: graphSurface
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        Canvas {
                            id: graphCanvas
                            anchors.fill: parent
                            visible: root.graphData.nodes.length > 0
                            onPaint: {
                                const context = getContext("2d")
                                context.clearRect(0, 0, width, height)
                                const nodes = root.graphData.nodes
                                const edges = root.graphData.edges
                                context.lineWidth = 1.2
                                for (let edgeIndex = 0; edgeIndex < edges.length; edgeIndex++) {
                                    const edge = edges[edgeIndex]
                                    let sourceIndex = -1
                                    let targetIndex = -1
                                    for (let nodeIndex = 0; nodeIndex < nodes.length; nodeIndex++) {
                                        if (nodes[nodeIndex].id === edge.source)
                                            sourceIndex = nodeIndex
                                        if (nodes[nodeIndex].id === edge.target)
                                            targetIndex = nodeIndex
                                    }
                                    if (sourceIndex < 0 || targetIndex < 0)
                                        continue
                                    const from = root.graphPosition(sourceIndex, nodes.length)
                                    const to = root.graphPosition(targetIndex, nodes.length)
                                    context.beginPath()
                                    context.strokeStyle = edge.kind === "reference"
                                        ? themeTokens.mintDeep : themeTokens.primary
                                    context.globalAlpha = edge.kind === "reference" ? 0.72 : 0.42
                                    context.moveTo(from.x, from.y)
                                    context.lineTo(to.x, to.y)
                                    context.stroke()
                                }
                                context.globalAlpha = 1
                            }
                            onWidthChanged: requestPaint()
                            onHeightChanged: requestPaint()
                        }

                        Repeater {
                            id: graphNodes
                            model: root.graphData.nodes
                            delegate: Button {
                                required property var modelData
                                required property int index
                                width: modelData.type === "topic" ? 126 : 116
                                height: modelData.type === "topic" ? 40 : 36
                                x: root.graphPosition(index, graphNodes.count).x - width / 2
                                y: root.graphPosition(index, graphNodes.count).y - height / 2
                                enabled: !!modelData.path
                                Accessible.name: modelData.path || modelData.label
                                ToolTip.visible: hovered
                                ToolTip.text: modelData.path || modelData.label
                                onClicked: if (modelData.path) root.sourceRequested(modelData.path)
                                background: Rectangle {
                                    radius: modelData.type === "topic" ? 12 : 9
                                    color: modelData.type === "topic" ? themeTokens.primaryTint : themeTokens.surface
                                    border.color: modelData.type === "topic" ? themeTokens.lineStrong : themeTokens.line
                                    border.width: 1
                                }
                                contentItem: Text {
                                    text: modelData.label
                                    color: modelData.type === "topic" ? themeTokens.primaryDeep : themeTokens.ink2
                                    font.family: themeTokens.text
                                    font.pixelSize: Math.round(modelData.type === "topic" ? 8.5 : 8 * root.textScale)
                                    font.bold: modelData.type === "topic"
                                    horizontalAlignment: Text.AlignHCenter
                                    verticalAlignment: Text.AlignVCenter
                                    wrapMode: Text.Wrap
                                    maximumLineCount: 2
                                    elide: Text.ElideRight
                                    leftPadding: 4
                                    rightPadding: 4
                                }
                            }
                        }

                        Text {
                            anchors.centerIn: parent
                            visible: root.graphData.nodes.length === 0
                            text: root.viewMode === "library"
                                ? root.tr("Сохранённые ссылки появятся здесь.", "Saved links will appear here.")
                                : root.tr("Граф появится после поиска по источникам.", "The graph appears after source search.")
                            color: themeTokens.ink4
                            font.family: themeTokens.text
                            font.pixelSize: Math.round(9 * root.textScale)
                            horizontalAlignment: Text.AlignHCenter
                            wrapMode: Text.WordWrap
                            width: parent.width - 24
                        }
                    }

                    RowLayout {
                        Layout.fillWidth: true
                        spacing: 10
                        Rectangle { width: 14; height: 2; color: themeTokens.primary }
                        Text { text: root.tr("результат поиска", "search match"); color: themeTokens.ink4; font.family: themeTokens.text; font.pixelSize: Math.round(7.5 * root.textScale) }
                        Rectangle { width: 14; height: 2; color: themeTokens.mintDeep }
                        Text { text: root.tr("явная ссылка", "explicit link"); color: themeTokens.ink4; font.family: themeTokens.text; font.pixelSize: Math.round(7.5 * root.textScale) }
                        Item { Layout.fillWidth: true }
                    }
                }
            }
        }
    }

    onGraphDataChanged: Qt.callLater(function() { graphCanvas.requestPaint() })

    function graphPosition(index, count) {
        const centerX = graphSurface.width / 2
        const centerY = graphSurface.height / 2
        if (index === 0 && root.viewMode === "research")
            return { x: centerX, y: centerY }
        const ringIndex = root.viewMode === "research" ? index - 1 : index
        const ringCount = Math.max(1, count - (root.viewMode === "research" ? 1 : 0))
        const angle = -Math.PI / 2 + ringIndex * Math.PI * 2 / ringCount
        const radiusX = Math.max(38, Math.min(graphSurface.width * 0.34, 128))
        const radiusY = Math.max(24, Math.min(graphSurface.height * 0.34, 116))
        return { x: centerX + Math.cos(angle) * radiusX, y: centerY + Math.sin(angle) * radiusY }
    }

    function buildGraphData() {
        const nodes = []
        const edges = []
        const nodeIds = {}
        function addSource(path) {
            const value = String(path || "")
            if (!value || nodeIds[value] !== undefined || nodes.length >= 9)
                return
            const name = value.split("/").pop() || value
            nodeIds[value] = nodes.length
            nodes.push({ id: value, path: value, label: name, type: "source" })
        }
        function addEdge(source, target, kind) {
            if (!source || !target || source === target)
                return
            if (!edges.some(function(edge) { return edge.source === source && edge.target === target }))
                edges.push({ source: source, target: target, kind: kind })
        }

        if (root.viewMode === "research") {
            const topic = root.selectedTopic
            if (!topic)
                return { nodes: nodes, edges: edges }
            nodes.push({ id: "topic:" + topic.id, label: topic.query, type: "topic", path: "" })
            for (const result of (topic.results || []).slice(0, 4)) {
                addSource(result.path)
                addEdge("topic:" + topic.id, result.path, "match")
            }
            for (const result of (topic.results || []).slice(0, 4)) {
                if (nodeIds[result.path] === undefined)
                    continue
                for (const related of (result.related || []).slice(0, 2)) {
                    addSource(related)
                    addEdge(result.path, related, "reference")
                }
            }
        } else {
            for (const source of root.library.slice(0, 5))
                addSource(source.path)
            for (const source of root.library.slice(0, 5)) {
                if (nodeIds[source.path] === undefined)
                    continue
                for (const related of (source.related || []).slice(0, 3)) {
                    addSource(related)
                    addEdge(source.path, related, "reference")
                }
            }
        }
        return { nodes: nodes, edges: edges }
    }

    function startQuery(value) {
        const query = String(value === undefined ? researchQuery.text : value).trim()
        if (query.length < 2)
            return
        const id = bridge.startResearch(query)
        if (id) {
            root.selectedTopicId = id
            root.viewMode = "research"
            if (value === undefined)
                researchQuery.clear()
        }
    }

    function tr(ru, en) {
        return root.language === "en" ? en : ru
    }
}
