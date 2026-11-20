import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Item {
    id: root
    signal openChatRequested()
    signal historyRequested()
    property string language: bridge.language
    property real textScale: 1.0
    property int selectedStepIndex: -1
    readonly property var selectedStep: selectedStepIndex >= 0 && selectedStepIndex < bridge.lastSteps.length
        ? bridge.lastSteps[selectedStepIndex] : null
    readonly property var runtimeEvents: {
        const events = bridge.events
        let latestCommand = -1
        for (let index = 0; index < events.length; index++) {
            const event = events[index]
            if (event.kind === "command" && ((event.data || {}).role === "user"))
                latestCommand = index
        }
        return events.slice(latestCommand + 1).filter(function(event) {
            return event.kind === "step" || event.kind === "tool" || event.kind === "error"
        })
    }
    readonly property var runtimeAgents: bridge.agents.filter(function(agent) {
        const status = String(agent.status || "")
        return status !== "В каталоге" && status !== "Available"
    })

    ThemeTokens { id: themeTokens; themeName: bridge.appearanceTheme; light: bridge.isLightTheme; compact: bridge.density === "compact" }
    function tr(ru, en) { return language === "en" ? en : ru }
    function eventTitle(event) {
        const data = event.data || {}
        return String(data.node || data.name || data.stage || event.kind || tr("Событие", "Event"))
    }
    function stepStatusColor(status) {
        const value = String(status || "").toLowerCase()
        if (value.indexOf("err") >= 0 || value.indexOf("fail") >= 0)
            return themeTokens.fail
        if (value.indexOf("run") >= 0 || value.indexOf("progress") >= 0)
            return themeTokens.aqua
        return themeTokens.mint
    }

    ScrollView {
        id: orchestrateScroll
        anchors.fill: parent
        clip: true
        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
        ColumnLayout {
            width: orchestrateScroll.availableWidth
            spacing: 14 * (bridge.density === "compact" ? 0.85 : 1.0)

            RowLayout {
                Layout.fillWidth: true
                spacing: 12
                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: 3
                    Text { text: root.tr("ОРКЕСТРАЦИЯ", "ORCHESTRATION"); color: themeTokens.mist; font.family: themeTokens.mono; font.pixelSize: Math.round(9.5 * root.textScale); font.letterSpacing: 1.4 }
                    Text { text: root.tr("Ход задачи", "Task execution"); color: themeTokens.ink; font.family: themeTokens.display; font.pixelSize: Math.round(22 * root.textScale); font.bold: true }
                    Text {
                        text: bridge.running
                            ? root.tr("Трасса, участники и события, сообщённые во время выполнения.", "Trace, participants and events reported during execution.")
                            : root.tr("Фактические данные последнего запуска без условных этапов.", "Actual data from the latest run, without synthetic stages.")
                        color: themeTokens.graphite; font.family: themeTokens.text; font.pixelSize: Math.round(11.5 * root.textScale); wrapMode: Text.WordWrap; Layout.fillWidth: true
                    }
                }
                Button {
                    visible: bridge.canResumeGoal
                    text: root.tr("Продолжить Goal", "Continue Goal")
                    enabled: !bridge.running
                    onClicked: bridge.continueGoal()
                    background: Rectangle { radius: themeTokens.radiusControl; color: themeTokens.primaryTint; border.color: themeTokens.lineStrong }
                    contentItem: Text { text: parent.text; color: themeTokens.primaryDeep; font.family: themeTokens.text; font.pixelSize: Math.round(10.5 * root.textScale); font.bold: true; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; leftPadding: 10; rightPadding: 10 }
                }
                Button {
                    text: root.tr("История Goal", "Goal history")
                    onClicked: root.historyRequested()
                    background: Rectangle { radius: themeTokens.radiusControl; color: themeTokens.surface2; border.color: themeTokens.line }
                    contentItem: Text { text: parent.text; color: themeTokens.ink2; font.family: themeTokens.text; font.pixelSize: Math.round(10 * root.textScale); horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; leftPadding: 10; rightPadding: 10 }
                }
            }

            GlassCard {
                Layout.fillWidth: true
                Layout.preferredHeight: 160
                Layout.minimumHeight: 140
                inset: 16
                glass: true
                accent: bridge.running ? themeTokens.aqua : "transparent"
                ColumnLayout {
                    anchors.fill: parent
                    spacing: 10
                    RowLayout {
                        Layout.fillWidth: true
                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 3
                            Text { text: root.tr("ЗАПУСК", "RUN"); color: themeTokens.mist; font.family: themeTokens.mono; font.pixelSize: Math.round(9 * root.textScale); font.letterSpacing: 1.3 }
                            Text { text: bridge.lastGoal; color: themeTokens.ink; font.family: themeTokens.display; font.pixelSize: Math.round(15 * root.textScale); font.bold: true; elide: Text.ElideRight; maximumLineCount: 2; wrapMode: Text.WordWrap; Layout.fillWidth: true }
                        }
                        Rectangle {
                            Layout.preferredHeight: 26
                            Layout.preferredWidth: runState.implicitWidth + 18
                            radius: themeTokens.radiusPill
                            color: bridge.running ? themeTokens.aquaTint : (bridge.progressPercent >= 100 ? themeTokens.primaryTint : themeTokens.surface3)
                            border.color: bridge.running ? themeTokens.aqua : themeTokens.line
                            Text { id: runState; anchors.centerIn: parent; text: bridge.running ? root.tr("ВЫПОЛНЯЕТСЯ", "RUNNING") : String(bridge.lastRunStatus || root.tr("Ожидание", "Idle")).toUpperCase(); color: bridge.running ? themeTokens.aquaInk : themeTokens.ink2; font.family: themeTokens.mono; font.pixelSize: Math.round(9 * root.textScale); font.bold: true }
                        }
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        spacing: 12
                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 5
                            RowLayout {
                                Layout.fillWidth: true
                                Text { text: root.tr("ПРОГРЕСС RUNTIME", "RUNTIME PROGRESS"); color: themeTokens.ink3; font.family: themeTokens.mono; font.pixelSize: Math.round(8.5 * root.textScale) }
                                Item { Layout.fillWidth: true }
                                Text {
                                    text: bridge.running || bridge.progressPercent > 0
                                        ? String(Math.max(0, Math.min(100, bridge.progressPercent))) + "%"
                                        : "—"
                                    color: themeTokens.ink2
                                    font.family: themeTokens.mono
                                    font.pixelSize: Math.round(9 * root.textScale)
                                }
                            }
                            Rectangle {
                                Layout.fillWidth: true
                                height: 4
                                radius: 2
                                color: themeTokens.surface3
                                Rectangle {
                                    width: parent.width * Math.max(0, Math.min(100, bridge.progressPercent)) / 100
                                    height: parent.height
                                    radius: 2
                                    color: bridge.running ? themeTokens.aqua : themeTokens.mint
                                }
                            }
                        }
                        Text {
                            text: String(bridge.lastSteps.length) + root.tr(" измеренных узлов", " measured nodes")
                            color: themeTokens.ink3
                            font.family: themeTokens.mono
                            font.pixelSize: Math.round(8.5 * root.textScale)
                        }
                    }
                }
            }

            GridLayout {
                Layout.fillWidth: true
                columns: width >= 900 ? 2 : 1
                columnSpacing: 14
                rowSpacing: 14

                GlassCard {
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    Layout.preferredHeight: 310
                    Layout.minimumHeight: 260
                    inset: 14
                    ColumnLayout {
                        anchors.fill: parent
                        spacing: 10
                        RowLayout {
                            Layout.fillWidth: true
                            Text { text: root.tr("УЧАСТНИКИ ЗАПУСКА", "RUN PARTICIPANTS"); color: themeTokens.mist; font.family: themeTokens.mono; font.pixelSize: Math.round(9.5 * root.textScale); font.letterSpacing: 1.2 }
                            Item { Layout.fillWidth: true }
                            Text { text: String(root.runtimeAgents.length); color: themeTokens.ink3; font.family: themeTokens.mono; font.pixelSize: Math.round(9 * root.textScale) }
                        }
                        ListView {
                            id: runTeamList
                            Layout.fillWidth: true
                            Layout.fillHeight: true
                            clip: true
                            model: root.runtimeAgents
                            spacing: 3
                            ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
                            delegate: RowLayout {
                                required property var modelData
                                width: runTeamList.width
                                height: 36
                                spacing: 8
                                Rectangle {
                                    width: 25; height: 25; radius: 13
                                    color: modelData.statusColor || themeTokens.primaryTint
                                    opacity: 0.72
                                    Text { anchors.centerIn: parent; text: String(modelData.name || "AG").slice(0, 2).toUpperCase(); color: themeTokens.ink; font.family: themeTokens.mono; font.pixelSize: Math.round(8 * root.textScale); font.bold: true }
                                }
                                ColumnLayout {
                                    Layout.fillWidth: true
                                    spacing: 1
                                    Text { text: modelData.name; color: themeTokens.ink; font.family: themeTokens.text; font.pixelSize: Math.round(10.5 * root.textScale); font.bold: true; elide: Text.ElideRight; Layout.fillWidth: true }
                                    Text { text: modelData.roleRu || modelData.role; color: themeTokens.ink3; font.family: themeTokens.text; font.pixelSize: Math.round(9 * root.textScale); elide: Text.ElideRight; Layout.fillWidth: true }
                                }
                                Text { text: modelData.status; color: modelData.statusColor || themeTokens.ink3; font.family: themeTokens.text; font.pixelSize: Math.round(9 * root.textScale); Layout.maximumWidth: 100; elide: Text.ElideRight }
                            }
                            Text {
                                anchors.centerIn: parent
                                visible: root.runtimeAgents.length === 0
                                text: root.tr("Runtime не сообщил участников этого запуска.", "Runtime has not reported participants for this run.")
                                color: themeTokens.ink3
                                font.family: themeTokens.text
                                font.pixelSize: Math.round(10 * root.textScale)
                                horizontalAlignment: Text.AlignHCenter
                                wrapMode: Text.WordWrap
                                width: parent.width - 24
                            }
                        }
                    }
                }

                GlassCard {
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    Layout.preferredHeight: 310
                    Layout.minimumHeight: 260
                    inset: 14
                    ColumnLayout {
                        anchors.fill: parent
                        spacing: 10
                        RowLayout {
                            Layout.fillWidth: true
                            Text { text: root.tr("СОБЫТИЯ ВЫПОЛНЕНИЯ", "EXECUTION EVENTS"); color: themeTokens.mist; font.family: themeTokens.mono; font.pixelSize: Math.round(9.5 * root.textScale); font.letterSpacing: 1.2 }
                            Item { Layout.fillWidth: true }
                            Text { text: String(root.runtimeEvents.length); color: themeTokens.ink3; font.family: themeTokens.mono; font.pixelSize: Math.round(9 * root.textScale) }
                        }
                        ListView {
                            id: latestEventsList
                            Layout.fillWidth: true
                            Layout.fillHeight: true
                            clip: true
                            spacing: 1
                            model: root.runtimeEvents.slice().reverse().slice(0, 100)
                            ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
                            delegate: RowLayout {
                                required property var modelData
                                width: latestEventsList.width
                                height: 42
                                spacing: 8
                                Rectangle { width: 6; height: 6; radius: 3; color: modelData.kind === "error" ? themeTokens.fail : modelData.kind === "tool" ? themeTokens.aqua : themeTokens.mint }
                                ColumnLayout {
                                    Layout.fillWidth: true
                                    spacing: 1
                                    Text { text: root.eventTitle(modelData); color: modelData.kind === "error" ? themeTokens.fail : themeTokens.ink; font.family: themeTokens.text; font.pixelSize: Math.round(10.5 * root.textScale); font.bold: true; elide: Text.ElideRight; Layout.fillWidth: true }
                                    Text { text: String(modelData.text || (modelData.data || {}).summary || (modelData.data || {}).status || ""); color: themeTokens.ink3; font.family: themeTokens.text; font.pixelSize: Math.round(9 * root.textScale); elide: Text.ElideRight; Layout.fillWidth: true }
                                }
                            }
                            Text {
                                anchors.centerIn: parent
                                visible: root.runtimeEvents.length === 0
                                text: root.tr("События появятся, когда runtime сообщит шаг, инструмент или ошибку.", "Events appear when runtime reports a step, tool call or error.")
                                color: themeTokens.ink3
                                font.family: themeTokens.text
                                font.pixelSize: Math.round(10 * root.textScale)
                                horizontalAlignment: Text.AlignHCenter
                            }
                        }
                    }
                }
            }

            GlassCard {
                Layout.fillWidth: true
                Layout.preferredHeight: Math.max(220, Math.min(340, 112 + bridge.lastSteps.length * 30))
                Layout.minimumHeight: 200
                inset: 14
                ColumnLayout {
                    anchors.fill: parent
                    spacing: 8
                    RowLayout {
                        Layout.fillWidth: true
                        Text { text: root.tr("ФАКТИЧЕСКАЯ ТРАССА", "MEASURED EXECUTION TRACE"); color: themeTokens.mist; font.family: themeTokens.mono; font.pixelSize: Math.round(9.5 * root.textScale); font.letterSpacing: 1.2 }
                        Item { Layout.fillWidth: true }
                        Button { text: root.tr("Открыть чат", "Open chat"); onClicked: root.openChatRequested(); background: Rectangle { radius: themeTokens.radiusControl; color: themeTokens.surface2; border.color: themeTokens.line } contentItem: Text { text: parent.text; color: themeTokens.ink2; font.family: themeTokens.text; font.pixelSize: Math.round(9.5 * root.textScale); horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; leftPadding: 8; rightPadding: 8 } }
                    }
                    Item {
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        ListView {
                            id: runStepList
                            anchors.fill: parent
                            clip: true
                            spacing: 1
                            model: bridge.lastSteps
                            ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
                            delegate: Item {
                                required property var modelData
                                required property int index
                                width: runStepList.width
                                height: 30
                                Rectangle {
                                    anchors.fill: parent
                                    radius: themeTokens.radiusControl
                                    color: root.selectedStepIndex === index ? themeTokens.surface2 : "transparent"
                                }
                                RowLayout {
                                    anchors.fill: parent
                                    anchors.leftMargin: 8
                                    anchors.rightMargin: 8
                                    spacing: 8
                                    Rectangle { width: 6; height: 6; radius: 3; color: root.stepStatusColor(modelData.status) }
                                    Text { text: modelData.node || root.tr("Неизвестный узел", "Unnamed node"); color: themeTokens.ink; font.family: themeTokens.mono; font.pixelSize: Math.round(10 * root.textScale); Layout.fillWidth: true; elide: Text.ElideRight }
                                    Text { text: String(modelData.attempts || 1) + root.tr(" попыт.", " attempt(s)"); color: themeTokens.ink3; font.family: themeTokens.text; font.pixelSize: Math.round(9 * root.textScale) }
                                    Text { text: Number(modelData.ms) > 0 ? String(modelData.ms) + " ms" : "—"; color: themeTokens.ink3; font.family: themeTokens.mono; font.pixelSize: Math.round(9 * root.textScale); Layout.preferredWidth: 70; horizontalAlignment: Text.AlignRight }
                                    Text { text: modelData.status || "—"; color: root.stepStatusColor(modelData.status); font.family: themeTokens.text; font.pixelSize: Math.round(9 * root.textScale); Layout.preferredWidth: 90; horizontalAlignment: Text.AlignRight; elide: Text.ElideRight }
                                }
                                TapHandler {
                                    onTapped: root.selectedStepIndex = index
                                }
                            }
                        }
                        Text {
                            anchors.centerIn: parent
                            width: Math.min(parent.width - 20, 420)
                            visible: bridge.lastSteps.length === 0
                            text: root.tr("Измеренные узлы и длительность появятся, когда runtime сохранит трассу.", "Measured nodes and timings appear when runtime records a trace.")
                            color: themeTokens.ink3
                            font.family: themeTokens.text
                            font.pixelSize: Math.round(10 * root.textScale)
                            horizontalAlignment: Text.AlignHCenter
                            wrapMode: Text.WordWrap
                        }
                    }
                    Text {
                        visible: root.selectedStep !== null
                        Layout.fillWidth: true
                        text: root.selectedStep
                            ? String(root.selectedStep.node || "") + " · " + String(root.selectedStep.status || root.tr("статус не указан", "status not reported"))
                                + " · " + String(root.selectedStep.attempts || 1) + root.tr(" попыток", " attempts")
                                + " · " + (Number(root.selectedStep.ms) > 0 ? String(root.selectedStep.ms) + " ms" : root.tr("время не измерено", "duration not measured"))
                            : ""
                        color: themeTokens.ink3
                        font.family: themeTokens.mono
                        font.pixelSize: Math.round(9 * root.textScale)
                        elide: Text.ElideRight
                    }
                }
            }
        }
    }
}

