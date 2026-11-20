import QtQuick

Rectangle {
    id: root
    ThemeTokens { id: themeTokens; themeName: bridge.appearanceTheme; light: bridge.isLightTheme; compact: bridge.density === "compact"; motion: bridge.motionEnabled }
    default property alias contentData: body.data
    property int inset: 16
    property bool flat: false
    property bool dark: false
    property bool glass: false
    property color glassTint: themeTokens.light ? "#B8DFF0E5" : "#CC0B251B"
    property color surface: "transparent"
    property color accent: "transparent"

    readonly property color face: root.surface.a > 0
                                  ? root.surface
                                  : (root.dark ? themeTokens.rail : themeTokens.surface)

    radius: root.dark ? 0 : themeTokens.radius
    color: root.face
    gradient: Gradient {
        GradientStop {
            position: 0.0
            color: root.surface.a > 0 ? root.surface
                  : root.dark ? themeTokens.rail2 : root.glass ? themeTokens.glassTop : themeTokens.surface2
        }
        GradientStop {
            position: 1.0
            color: root.surface.a > 0 ? root.surface
                  : root.dark ? themeTokens.rail : root.glass ? root.glassTint : themeTokens.glassBottom
        }
    }
    border.color: root.dark ? "#00000000" : root.glass ? themeTokens.lineStrong : themeTokens.line
    border.width: root.flat || root.dark ? 0 : 1

    Rectangle {
        visible: root.glass && !root.dark
        anchors.fill: parent
        anchors.margins: 1
        radius: Math.max(0, root.radius - 1)
        color: "transparent"
        border.color: themeTokens.glassHighlight
        border.width: 1
    }

    Rectangle {
        visible: root.glass && !root.dark
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        anchors.margins: 1
        height: 1
        color: themeTokens.glassHighlight
        radius: 1
    }

    Rectangle {
        visible: root.accent.a > 0
        anchors.left: parent.left
        anchors.top: parent.top
        anchors.bottom: parent.bottom
        width: 3
        color: root.accent
        radius: 2
    }

    Item {
        id: body
        anchors.fill: parent
        anchors.margins: root.inset
    }
}
