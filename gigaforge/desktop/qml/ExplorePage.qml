import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Item {
    id: root
    signal fileRequested(string path)
    property string language: bridge.language
    property real textScale: 1.0
    property string query: ""
    property string filter: "all"
    property string resultMode: "paths"

    ThemeTokens { id: themeTokens; themeName: bridge.appearanceTheme; light: bridge.isLightTheme; compact: bridge.density === "compact" }
    Timer { id: contentSearchTimer; interval: 280; repeat: false; onTriggered: bridge.searchProject(root.query) }

    function tr(ru, en) { return language === "en" ? en : ru }
    function focusSearch() { projectSearch.forceActiveFocus() }
    function groupFor(file) {
        const ext = String(file.suffix || "").toLowerCase()
        const name = String(file.name || "").toLowerCase()
        if ([".md", ".rst", ".txt", ".adoc"].indexOf(ext) >= 0) return "docs"
        if ([".py", ".js", ".jsx", ".ts", ".tsx", ".qml", ".html", ".css", ".scss", ".vue", ".go", ".rs", ".java", ".cs", ".cpp", ".c", ".h", ".sh", ".ps1"].indexOf(ext) >= 0) return "code"
        if ([".json", ".toml", ".yaml", ".yml", ".ini", ".cfg", ".xml"].indexOf(ext) >= 0 || name === "dockerfile" || name === "makefile") return "config"
        return "other"
    }
    readonly property var visibleFiles: {
        const needle = query.trim().toLowerCase()
        return bridge.files.filter(function(file) {
            const group = root.groupFor(file)
            const matchGroup = root.filter === "all" || root.filter === group
            const matchText = !needle || String(file.path || "").toLowerCase().indexOf(needle) >= 0
            return matchGroup && matchText
        }).slice(0, 100)
    }
    readonly property int codeCount: bridge.files.filter(function(file) { return root.groupFor(file) === "code" }).length
    readonly property int docsCount: bridge.files.filter(function(file) { return root.groupFor(file) === "docs" }).length
    readonly property int configCount: bridge.files.filter(function(file) { return root.groupFor(file) === "config" }).length

    GlassCard {
        anchors.fill: parent
        inset: 0
        glass: true
        ColumnLayout {
            anchors.fill: parent
            anchors.margins: 18
            spacing: 12 * (bridge.density === "compact" ? 0.85 : 1.0)

            RowLayout {
                Layout.fillWidth: true
                spacing: 12
                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: 3
                    Text {
                        text: root.tr("КАРТА ПРОЕКТА", "PROJECT MAP")
                        color: themeTokens.mist
                        font.family: themeTokens.mono
                        font.pixelSize: Math.round(9.5 * root.textScale)
                        font.letterSpacing: 1.4
                    }
                    Text {
                        text: root.tr("Исследуйте код и материалы", "Explore code and project material")
                        color: themeTokens.ink
                        font.family: themeTokens.display
                        font.pixelSize: Math.round(22 * root.textScale)
                        font.bold: true
                    }
                    Text {
                        text: root.tr("Поиск работает по доступным файлам выбранного проекта. Выберите результат, чтобы открыть его в редакторе.",
                                      "Search the accessible files in the selected project. Select a result to open it in the editor.")
                        color: themeTokens.graphite
                        font.family: themeTokens.text
                        font.pixelSize: Math.round(11.5 * root.textScale)
                        wrapMode: Text.WordWrap
                        Layout.fillWidth: true
                    }
                }
                Button {
                    text: root.tr("Открыть редактор", "Open editor")
                    onClicked: root.fileRequested("")
                    background: Rectangle { radius: themeTokens.radiusControl; color: themeTokens.primaryTint; border.color: themeTokens.lineStrong }
                    contentItem: Text {
                        text: parent.text
                        color: themeTokens.primaryDeep
                        font.family: themeTokens.text
                        font.pixelSize: Math.round(10.5 * root.textScale)
                        font.bold: true
                        horizontalAlignment: Text.AlignHCenter
                        verticalAlignment: Text.AlignVCenter
                        leftPadding: 12
                        rightPadding: 12
                    }
                }
            }

            GlassCard {
                Layout.fillWidth: true
                Layout.preferredHeight: 76
                inset: 10
                RowLayout {
                    anchors.fill: parent
                    spacing: 10
                    Rectangle {
                        Layout.preferredWidth: 34
                        Layout.preferredHeight: 34
                        radius: 17
                        color: themeTokens.primaryTint
                        Text { anchors.centerIn: parent; text: "⌕"; color: themeTokens.primaryInk; font.pixelSize: 24 }
                    }
                    TextField {
                        id: projectSearch
                        Layout.fillWidth: true
                        placeholderText: root.tr("Найти файл или путь…", "Search files and paths…")
                        text: root.query
                        onTextChanged: {
                            root.query = text
                            if (root.resultMode === "content") contentSearchTimer.restart()
                        }
                        selectByMouse: true
                        color: themeTokens.ink
                        placeholderTextColor: themeTokens.ink3
                        font.family: themeTokens.text
                        font.pixelSize: Math.round(13 * root.textScale)
                        background: Rectangle { radius: themeTokens.radiusControl; color: themeTokens.surface; border.color: projectSearch.activeFocus ? themeTokens.primary : themeTokens.controlLine }
                        Keys.onEscapePressed: { clear(); root.forceActiveFocus() }
                    }
                    Text {
                        text: root.resultMode === "paths"
                            ? String(root.visibleFiles.length) + " / " + String(bridge.files.length)
                            : String(bridge.exploreResults.length) + root.tr(" совпадений", " matches")
                        color: themeTokens.ink3
                        font.family: themeTokens.mono
                        font.pixelSize: Math.round(9.5 * root.textScale)
                    }
                    Button {
                        text: root.tr("Обновить", "Refresh")
                        onClicked: {
                            bridge.refreshFiles()
                            if (root.resultMode === "content") contentSearchTimer.restart()
                        }
                        background: Rectangle { radius: themeTokens.radiusControl; color: themeTokens.surface2; border.color: themeTokens.line }
                        contentItem: Text { text: parent.text; color: themeTokens.ink2; font.family: themeTokens.text; font.pixelSize: Math.round(10 * root.textScale); horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; leftPadding: 8; rightPadding: 8 }
                    }
                }
            }

            RowLayout {
                Layout.fillWidth: true
                spacing: 10
                Repeater {
                    model: [
                        { key: "all", label: root.tr("Все файлы", "All files"), count: bridge.files.length },
                        { key: "code", label: root.tr("Код", "Code"), count: root.codeCount },
                        { key: "docs", label: root.tr("Документы", "Docs"), count: root.docsCount },
                        { key: "config", label: root.tr("Конфигурация", "Config"), count: root.configCount }
                    ]
                    delegate: Button {
                        required property var modelData
                        Layout.preferredHeight: 32
                        onClicked: root.filter = modelData.key
                        background: Rectangle {
                            radius: themeTokens.radiusPill
                            color: root.filter === modelData.key ? themeTokens.primaryTint : themeTokens.surface
                            border.color: root.filter === modelData.key ? themeTokens.lineStrong : themeTokens.line
                        }
                        contentItem: Text {
                            text: modelData.label + "  " + modelData.count
                            color: root.filter === modelData.key ? themeTokens.primaryDeep : themeTokens.ink3
                            font.family: themeTokens.text
                            font.pixelSize: Math.round(10.5 * root.textScale)
                            font.bold: root.filter === modelData.key
                            horizontalAlignment: Text.AlignHCenter
                            verticalAlignment: Text.AlignVCenter
                            leftPadding: 11
                            rightPadding: 11
                        }
                    }
                }
                Item { Layout.fillWidth: true }
                Text {
                    text: root.tr("Конфиденциальные пути исключены из индекса интерфейса", "Sensitive paths are excluded from the UI index")
                    color: themeTokens.ink4
                    font.family: themeTokens.text
                    font.pixelSize: Math.round(9.5 * root.textScale)
                    elide: Text.ElideRight
                    Layout.maximumWidth: 360
                }
            }

            RowLayout {
                Layout.fillWidth: true
                spacing: 8
                Repeater {
                    model: [
                        { key: "paths", ru: "Пути и имена", en: "Paths and names" },
                        { key: "content", ru: "По содержимому", en: "File contents" }
                    ]
                    delegate: Button {
                        required property var modelData
                        Layout.preferredHeight: 32
                        onClicked: {
                            root.resultMode = modelData.key
                            if (modelData.key === "content") contentSearchTimer.restart()
                            else {
                                contentSearchTimer.stop()
                                bridge.searchProject("")
                            }
                        }
                        background: Rectangle {
                            radius: themeTokens.radiusPill
                            color: root.resultMode === modelData.key ? themeTokens.primaryTint : themeTokens.surface
                            border.color: root.resultMode === modelData.key ? themeTokens.lineStrong : themeTokens.line
                        }
                        contentItem: Text {
                            text: root.language === "en" ? modelData.en : modelData.ru
                            color: root.resultMode === modelData.key ? themeTokens.primaryDeep : themeTokens.ink3
                            font.family: themeTokens.text
                            font.pixelSize: Math.round(10 * root.textScale)
                            font.bold: root.resultMode === modelData.key
                            horizontalAlignment: Text.AlignHCenter
                            verticalAlignment: Text.AlignVCenter
                            leftPadding: 12
                            rightPadding: 12
                        }
                    }
                }
                Item { Layout.fillWidth: true }
                BusyIndicator { visible: bridge.exploreSearchBusy; running: visible; Layout.preferredWidth: 22; Layout.preferredHeight: 22 }
                Text {
                    visible: root.resultMode === "content"
                    text: root.query.trim().length < 2
                        ? root.tr("Введите минимум 2 символа", "Enter at least 2 characters")
                        : root.tr("Поиск по текстовым файлам проекта", "Searching project text files")
                    color: themeTokens.ink4
                    font.family: themeTokens.text
                    font.pixelSize: Math.round(9.5 * root.textScale)
                }
            }

            GlassCard {
                Layout.fillWidth: true
                Layout.fillHeight: true
                inset: 0
                ColumnLayout {
                    anchors.fill: parent
                    anchors.margins: 8
                    spacing: 3
                    RowLayout {
                        Layout.fillWidth: true
                        Layout.leftMargin: 8
                        Layout.rightMargin: 8
                        Layout.preferredHeight: 30
                        Text { text: root.resultMode === "paths" ? root.tr("ФАЙЛ", "FILE") : root.tr("СОВПАДЕНИЕ В ФАЙЛЕ", "MATCH IN FILE"); color: themeTokens.ink3; font.family: themeTokens.mono; font.pixelSize: Math.round(9 * root.textScale); font.letterSpacing: 1.2; Layout.fillWidth: true }
                        Text { visible: root.resultMode === "paths"; text: root.tr("ТИП", "TYPE"); color: themeTokens.ink3; font.family: themeTokens.mono; font.pixelSize: Math.round(9 * root.textScale); font.letterSpacing: 1.2; Layout.preferredWidth: 84; horizontalAlignment: Text.AlignRight }
                    }
                    Rectangle { Layout.fillWidth: true; height: 1; color: themeTokens.line }
                    ScrollView {
                        id: projectFileScroll
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        clip: true
                        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
                        ColumnLayout {
                            width: projectFileScroll.availableWidth
                            spacing: 2
                            Repeater {
                                id: fileResults
                                model: root.resultMode === "paths" ? root.visibleFiles : []
                                delegate: Button {
                                    required property var modelData
                                    Layout.fillWidth: true
                                    Layout.preferredHeight: 48
                                    onClicked: root.fileRequested(modelData.path)
                                    background: Rectangle { radius: themeTokens.radiusControl; color: parent.hovered ? themeTokens.surface2 : "transparent"; border.color: parent.hovered ? themeTokens.line : "transparent" }
                                    contentItem: RowLayout {
                                        spacing: 10
                                        Rectangle {
                                            Layout.preferredWidth: 30
                                            Layout.preferredHeight: 30
                                            radius: 15
                                            color: themeTokens.aquaTint
                                            Text { anchors.centerIn: parent; text: String(modelData.suffix || "·").replace(".", "").slice(0, 2).toUpperCase(); color: themeTokens.aquaInk; font.family: themeTokens.mono; font.pixelSize: Math.round(8.5 * root.textScale); font.bold: true }
                                        }
                                        ColumnLayout {
                                            Layout.fillWidth: true
                                            spacing: 1
                                            Text { text: modelData.name; color: themeTokens.ink; font.family: themeTokens.text; font.pixelSize: Math.round(11 * root.textScale); font.bold: true; elide: Text.ElideRight; Layout.fillWidth: true }
                                            Text { text: modelData.path; color: themeTokens.ink3; font.family: themeTokens.mono; font.pixelSize: Math.round(9 * root.textScale); elide: Text.ElideMiddle; Layout.fillWidth: true }
                                        }
                                        Text { text: modelData.suffix || root.tr("файл", "file"); color: themeTokens.ink4; font.family: themeTokens.mono; font.pixelSize: Math.round(9 * root.textScale); Layout.preferredWidth: 84; horizontalAlignment: Text.AlignRight }
                                    }
                                }
                            }
                            Repeater {
                                id: contentResults
                                model: root.resultMode === "content"
                                    ? bridge.exploreResults.filter(function(hit) {
                                        const group = root.groupFor(hit)
                                        return root.filter === "all" || root.filter === group
                                    }) : []
                                delegate: Button {
                                    required property var modelData
                                    Layout.fillWidth: true
                                    Layout.preferredHeight: 62
                                    onClicked: root.fileRequested(modelData.path)
                                    background: Rectangle { radius: themeTokens.radiusControl; color: parent.hovered ? themeTokens.surface2 : "transparent"; border.color: parent.hovered ? themeTokens.line : "transparent" }
                                    contentItem: ColumnLayout {
                                        spacing: 3
                                        RowLayout {
                                            Layout.fillWidth: true
                                            Text { text: modelData.path + ":" + modelData.line; color: themeTokens.aquaInk; font.family: themeTokens.mono; font.pixelSize: Math.round(9 * root.textScale); Layout.fillWidth: true; elide: Text.ElideMiddle }
                                            Text { text: modelData.suffix || ""; color: themeTokens.ink4; font.family: themeTokens.mono; font.pixelSize: Math.round(8.5 * root.textScale) }
                                        }
                                        Text { text: modelData.text; color: themeTokens.ink2; font.family: themeTokens.mono; font.pixelSize: Math.round(9.5 * root.textScale); elide: Text.ElideRight; Layout.fillWidth: true }
                                    }
                                }
                            }
                            Text {
                                visible: root.resultMode === "paths" ? fileResults.count === 0 : contentResults.count === 0 && !bridge.exploreSearchBusy && root.query.trim().length >= 2
                                Layout.fillWidth: true
                                Layout.topMargin: 30
                                horizontalAlignment: Text.AlignHCenter
                                text: root.resultMode === "paths"
                                    ? (bridge.files.length === 0
                                        ? root.tr("Список файлов ещё не загружен", "Project files have not been loaded yet")
                                        : root.tr("Подходящих файлов нет", "No matching files"))
                                    : (root.query.trim().length < 2
                                        ? root.tr("Введите минимум 2 символа для поиска по содержимому", "Enter at least 2 characters to search file contents")
                                        : root.tr("Совпадений не найдено", "No matches found"))
                                color: themeTokens.ink3
                                font.family: themeTokens.text
                                font.pixelSize: Math.round(12 * root.textScale)
                            }
                        }
                    }
                }
            }
        }
    }
}

