import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Item {
    id: root

    property string mode: "chat"
    property string language: "ru"
    property real textScale: 1
    property real densityScale: 1
    property string objectNamePrefix: "modeSelector_"
    signal modeSelected(string value)

    implicitWidth: 132 * densityScale
    implicitHeight: 34 * densityScale

    readonly property var modes: [
        { key: "chat", ru: "Чат", en: "Chat" },
        { key: "agent", ru: "Агент", en: "Agent" }
    ]

    ThemeTokens {
        id: themeTokens
        themeName: bridge.appearanceTheme
        light: bridge.isLightTheme
        compact: bridge.density === "compact"
    }

    RowLayout {
        anchors.fill: parent
        spacing: 2 * root.densityScale

        Repeater {
            model: root.modes

            delegate: Button {
                required property var modelData
                objectName: root.objectNamePrefix + modelData.key
                Layout.preferredWidth: (modelData.key === "agent" ? 64 : 54) * root.densityScale
                Layout.fillHeight: true
                leftPadding: 6 * root.densityScale
                rightPadding: 6 * root.densityScale
                topPadding: 2
                bottomPadding: 2
                enabled: root.enabled && !bridge.running
                Accessible.name: root.language === "en" ? modelData.en : modelData.ru
                ToolTip.visible: hovered
                ToolTip.text: root.tr("Режим: ", "Mode: ")
                    + (root.language === "en" ? modelData.en : modelData.ru)
                onClicked: root.modeSelected(modelData.key)

                background: Rectangle {
                    radius: themeTokens.radiusPill
                    color: root.mode === modelData.key ? themeTokens.primaryTint : "transparent"
                    border.color: root.mode === modelData.key
                        ? themeTokens.lineStrong
                        : parent.activeFocus ? themeTokens.primary : "transparent"
                    border.width: root.mode === modelData.key || parent.activeFocus ? 1 : 0
                }

                contentItem: Text {
                    text: root.language === "en" ? modelData.en : modelData.ru
                    color: root.mode === modelData.key ? themeTokens.primaryDeep : themeTokens.ink2
                    font.family: themeTokens.text
                    font.pixelSize: Math.round(11 * root.textScale)
                    font.bold: root.mode === modelData.key
                    horizontalAlignment: Text.AlignHCenter
                    verticalAlignment: Text.AlignVCenter
                }
            }
        }
    }

    function tr(ru, en) {
        return root.language === "en" ? en : ru
    }
}
