import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Flickable {
    id: root

    property var attachments: []
    property real densityScale: 1
    property real textScale: 1
    property string language: "ru"
    signal removeRequested(string path)

    implicitHeight: 26 * densityScale
    clip: true
    contentWidth: Math.max(width, chips.implicitWidth)
    contentHeight: height
    flickableDirection: Flickable.HorizontalFlick
    boundsBehavior: Flickable.StopAtBounds

    ThemeTokens {
        id: themeTokens
        themeName: bridge.appearanceTheme
        light: bridge.isLightTheme
        compact: bridge.density === "compact"
    }

    RowLayout {
        id: chips
        height: root.height
        spacing: 5 * root.densityScale

        Repeater {
            model: root.attachments

            delegate: Button {
                required property var modelData
                Layout.preferredWidth: Math.min(
                    168 * root.densityScale,
                    Math.max(86 * root.densityScale, String(modelData.name || "").length * 6.5 + 40)
                )
                Layout.preferredHeight: 22 * root.densityScale
                padding: 0
                Accessible.name: root.tr("Убрать файл ", "Remove file ")
                    + String(modelData.name || "")
                ToolTip.visible: hovered
                ToolTip.text: String(modelData.name || "")
                onClicked: root.removeRequested(String(modelData.path || ""))

                background: Rectangle {
                    radius: themeTokens.radiusPill
                    color: parent.hovered ? themeTokens.surface3 : themeTokens.surface2
                    border.color: themeTokens.line
                }

                contentItem: RowLayout {
                    anchors.fill: parent
                    anchors.leftMargin: 7 * root.densityScale
                    anchors.rightMargin: 6 * root.densityScale
                    spacing: 4 * root.densityScale
                    SmartIcon {
                        Layout.preferredWidth: 13 * root.densityScale
                        Layout.preferredHeight: 13 * root.densityScale
                        kind: "attach"
                        accent: themeTokens.ink3
                        ring: "transparent"
                    }
                    Text {
                        text: String(modelData.name || "")
                        color: themeTokens.ink2
                        font.family: themeTokens.text
                        font.pixelSize: Math.round(8.5 * root.textScale)
                        elide: Text.ElideMiddle
                        Layout.fillWidth: true
                    }
                    Text {
                        text: "×"
                        color: themeTokens.ink3
                        font.family: themeTokens.text
                        font.pixelSize: Math.round(11 * root.textScale)
                    }
                }
            }
        }
    }

    function tr(ru, en) {
        return root.language === "en" ? en : ru
    }
}
