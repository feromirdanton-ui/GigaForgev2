import QtQuick
import QtQuick.Layouts
import "Tokens.js" as T

// Верстак Forge: полоса задачи, дерево изменённых файлов, редактор, проверки, консоль.
// Данные приходят снаружи — компонент ничего не выдумывает.
Item {
    id: root

    property string goalText: ""
    property string stateText: ""
    property string branchText: ""
    property int progress: 0
    property var changes: []      // [{ path, plus, minus }]
    property var checks: []       // [{ name, verdict, note }]
    property var codeLines: []    // [{ n, text, kind }] kind: "" | "add" | "del"
    property string terminalText: ""
    property var events: []       // [{ time, kind, text, who }]

    readonly property int railGap: 12

    ColumnLayout {
        anchors.fill: parent
        spacing: root.railGap

        // ── полоса задачи ─────────────────────────────────────
        GlassCard {
            Layout.fillWidth: true
            Layout.preferredHeight: 64
            inset: 0

            RowLayout {
                anchors.fill: parent
                anchors.leftMargin: 18
                anchors.rightMargin: 18
                spacing: 16

                ColumnLayout {
                    spacing: 3
                    Layout.fillWidth: true
                    Text {
                        text: root.goalText.length > 0 ? root.goalText : qsTr("Задача не поставлена")
                        color: T.ink
                        font.family: T.display
                        font.pixelSize: 17
                        font.bold: true
                        elide: Text.ElideRight
                        Layout.fillWidth: true
                    }
                    RowLayout {
                        spacing: 8
                        Text {
                            text: root.branchText
                            color: T.ink3
                            font.family: T.mono
                            font.pixelSize: 11
                            visible: root.branchText.length > 0
                        }
                        Rectangle { width: 1; height: 9; color: T.line; visible: root.branchText.length > 0 }
                        Text {
                            text: root.stateText
                            color: T.aquaInk
                            font.family: T.mono
                            font.pixelSize: 11
                        }
                    }
                }

                // линия этапов
                Row {
                    spacing: 0
                    Layout.alignment: Qt.AlignVCenter
                    Repeater {
                        model: [qsTr("Анализ"), qsTr("План"), qsTr("Разработка"), qsTr("Проверка"), qsTr("Результат")]
                        delegate: Item {
                            required property int index
                            width: 74
                            height: 30
                            Rectangle {
                                width: 9
                                height: 9
                                radius: 4.5
                                anchors.verticalCenter: parent.verticalCenter
                                color: index * 20 < root.progress ? T.mint : T.surface
                                border.width: index * 20 < root.progress ? 0 : 1
                                border.color: T.line
                            }
                            Rectangle {
                                visible: index < 4
                                height: 1
                                width: parent.width - 12
                                x: 12
                                anchors.verticalCenter: parent.verticalCenter
                                color: T.lineSoft
                            }
                            Text {
                                x: 12
                                anchors.verticalCenter: parent.verticalCenter
                                text: modelData
                                color: index * 20 < root.progress ? T.ink : T.ink3
                                font.pixelSize: 11
                            }
                        }
                    }
                }

                Text {
                    text: root.progress + " %"
                    color: T.ink2
                    font.family: T.mono
                    font.pixelSize: 12
                    Layout.alignment: Qt.AlignVCenter
                }
            }
        }

        // ── лента событий ─────────────────────────────────────
        GlassCard {
            Layout.fillWidth: true
            Layout.preferredHeight: 104
            inset: 0

            ColumnLayout {
                anchors.fill: parent
                anchors.margins: 14
                spacing: 6

                Text {
                    text: qsTr("ЖУРНАЛ")
                    color: T.ink3
                    font.family: T.mono
                    font.pixelSize: 10
                    font.letterSpacing: 1.4
                }
                Repeater {
                    model: root.events.slice(-3)
                    delegate: RowLayout {
                        required property var modelData
                        Layout.fillWidth: true
                        spacing: 10
                        Text {
                            text: modelData.time
                            color: T.ink3
                            font.family: T.mono
                            font.pixelSize: 10
                            Layout.preferredWidth: 44
                        }
                        Rectangle {
                            width: 6
                            height: 6
                            radius: 3
                            color: modelData.kind === "error" ? T.red
                                 : modelData.kind === "tool" ? T.aqua
                                 : modelData.kind === "done" ? T.mint : T.line
                        }
                        Text {
                            text: modelData.text
                            color: T.ink2
                            font.pixelSize: 12
                            elide: Text.ElideRight
                            Layout.fillWidth: true
                        }
                        Text {
                            text: modelData.who
                            color: T.ink3
                            font.family: T.mono
                            font.pixelSize: 10
                        }
                    }
                }
                Text {
                    visible: root.events.length === 0
                    text: qsTr("События появятся после запуска задачи")
                    color: T.ink3
                    font.pixelSize: 12
                }
            }
        }

        // ── верстак ───────────────────────────────────────────
        RowLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            spacing: root.railGap

            // изменения
            GlassCard {
                Layout.preferredWidth: 268
                Layout.fillHeight: true
                inset: 0

                ColumnLayout {
                    anchors.fill: parent
                    anchors.margins: 14
                    spacing: 8

                    Text {
                        text: qsTr("Изменённые файлы")
                        color: T.ink3
                        font.family: T.mono
                        font.pixelSize: 10
                        font.letterSpacing: 1.4
                    }
                    Repeater {
                        model: root.changes
                        delegate: RowLayout {
                            required property var modelData
                            Layout.fillWidth: true
                            spacing: 6
                            Text {
                                text: modelData.path
                                color: T.ink2
                                font.family: T.mono
                                font.pixelSize: 11
                                elide: Text.ElideMiddle
                                Layout.fillWidth: true
                            }
                            Text {
                                text: "+" + modelData.plus
                                color: T.mintInk
                                font.family: T.mono
                                font.pixelSize: 11
                            }
                            Text {
                                text: "−" + modelData.minus
                                color: T.redInk
                                font.family: T.mono
                                font.pixelSize: 11
                            }
                        }
                    }
                    Item { Layout.fillHeight: true }
                    Text {
                        visible: root.changes.length === 0
                        text: qsTr("Пока нет изменений")
                        color: T.ink3
                        font.pixelSize: 12
                        wrapMode: Text.WordWrap
                        Layout.fillWidth: true
                    }
                }
            }

            // редактор
            GlassCard {
                Layout.fillWidth: true
                Layout.fillHeight: true
                inset: 0

                ColumnLayout {
                    anchors.fill: parent
                    anchors.margins: 0
                    spacing: 0

                    RowLayout {
                        Layout.fillWidth: true
                        Layout.preferredHeight: 34
                        Layout.leftMargin: 14
                        Layout.rightMargin: 14
                        spacing: 8
                        Text {
                            text: root.changes.length > 0 ? root.changes[0].path : qsTr("файл не выбран")
                            color: T.ink2
                            font.family: T.mono
                            font.pixelSize: 11
                            elide: Text.ElideMiddle
                            Layout.fillWidth: true
                        }
                        Rectangle {
                            width: 1
                            height: 10
                            color: T.line
                        }
                        Text {
                            text: qsTr("Diff")
                            color: T.primary
                            font.pixelSize: 11
                        }
                    }
                    Rectangle { Layout.fillWidth: true; height: 1; color: T.lineSoft }

                    ListView {
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        clip: true
                        model: root.codeLines
                        delegate: Rectangle {
                            required property var modelData
                            width: ListView.view.width
                            height: 19
                            color: modelData.kind === "add" ? "#E7F6F1"
                                 : modelData.kind === "del" ? "#FBEDEC" : "transparent"
                            Row {
                                spacing: 12
                                x: 12
                                anchors.verticalCenter: parent.verticalCenter
                                Text {
                                    text: modelData.n
                                    width: 24
                                    horizontalAlignment: Text.AlignRight
                                    color: "#5B7583"
                                    font.family: T.mono
                                    font.pixelSize: 11
                                }
                                Text {
                                    text: modelData.text
                                    color: T.ink2
                                    font.family: T.mono
                                    font.pixelSize: 11
                                }
                            }
                        }
                        Text {
                            anchors.centerIn: parent
                            visible: root.codeLines.length === 0
                            text: qsTr("Выберите файл — здесь появится код и дифф")
                            color: T.ink3
                            font.pixelSize: 12
                        }
                    }
                }
            }

            // проверки + консоль
            ColumnLayout {
                Layout.preferredWidth: 288
                Layout.fillHeight: true
                spacing: root.railGap

                GlassCard {
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    inset: 0
                    ColumnLayout {
                        anchors.fill: parent
                        anchors.margins: 14
                        spacing: 7
                        Text {
                            text: qsTr("Проверки")
                            color: T.ink3
                            font.family: T.mono
                            font.pixelSize: 10
                            font.letterSpacing: 1.4
                        }
                        Repeater {
                            model: root.checks
                            delegate: RowLayout {
                                required property var modelData
                                Layout.fillWidth: true
                                spacing: 7
                                Rectangle {
                                    width: 7
                                    height: 7
                                    radius: 3.5
                                    color: modelData.verdict === "ok" ? T.mint
                                         : modelData.verdict === "fail" ? T.red : T.amber
                                }
                                Text {
                                    text: modelData.name
                                    color: T.ink2
                                    font.pixelSize: 12
                                    elide: Text.ElideRight
                                    Layout.fillWidth: true
                                }
                                Text {
                                    text: modelData.note
                                    color: T.ink3
                                    font.family: T.mono
                                    font.pixelSize: 10
                                }
                            }
                        }
                        Item { Layout.fillHeight: true }
                        Text {
                            visible: root.checks.length === 0
                            text: qsTr("Проверок ещё не было")
                            color: T.ink3
                            font.pixelSize: 12
                        }
                    }
                }

                Rectangle {
                    Layout.fillWidth: true
                    Layout.preferredHeight: 132
                    radius: T.radius
                    color: T.consoleBg
                    border.width: 1
                    border.color: "#14303C"

                    ColumnLayout {
                        anchors.fill: parent
                        anchors.margins: 12
                        spacing: 4
                        Text {
                            text: qsTr("Терминал")
                            color: "#9DB3BE"
                            font.family: T.mono
                            font.pixelSize: 10
                            font.letterSpacing: 1.2
                        }
                        Text {
                            Layout.fillWidth: true
                            Layout.fillHeight: true
                            text: root.terminalText.length > 0 ? root.terminalText : qsTr("вывод появится здесь")
                            color: T.consoleInk
                            font.family: T.mono
                            font.pixelSize: 11
                            wrapMode: Text.WrapAnywhere
                            elide: Text.ElideRight
                        }
                    }
                }
            }
        }
    }
}
