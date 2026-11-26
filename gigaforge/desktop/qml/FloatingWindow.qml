import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Rectangle {
    id: root

    property string title: ""
    property string iconText: "◈"
    property string iconFont: themeTokens.text
    // The window icon is a glyph, so it is text: the fill token would be 4.07
    // on the field.  ``accent`` stays the name the callers set; it now carries
    // the readable value.
    property color accent: themeTokens.primaryInk
    property string closeLabel: "Закрыть"
    property string minimizeLabel: "Свернуть"
    property string maximizeLabel: "Развернуть"
    property real minWidth: 370
    property real minHeight: 270
    property real maxWidth: 1500
    property real maxHeight: 1000
    property bool maximized: false
    property real stackLevel: 120
    default property alias contentData: content.data

    signal closeRequested()
    signal minimizeRequested()
    signal raiseRequested()
    signal geometryCommitted(real x, real y, real width, real height)

    property real restoredX: 24
    property real restoredY: 72
    property real restoredWidth: 580
    property real restoredHeight: 390

    ThemeTokens {
        id: themeTokens
        themeName: bridge.appearanceTheme
        light: bridge.isLightTheme
        compact: bridge.density === "compact"
    }

    radius: themeTokens.radius
    color: themeTokens.surface2
    border.color: themeTokens.lineStrong
    border.width: 1
    clip: true
    z: root.stackLevel
    gradient: Gradient {
        GradientStop { position: 0; color: themeTokens.glassTop }
        GradientStop { position: 1; color: themeTokens.glassBottom }
    }

    Rectangle {
        anchors.fill: parent
        anchors.margins: 1
        radius: Math.max(0, root.radius - 1)
        color: "transparent"
        border.color: themeTokens.glassHighlight
        border.width: 1
    }

    Rectangle {
        id: titleBar
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        height: 44
        color: themeTokens.surface

        Rectangle {
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.bottom: parent.bottom
            height: 1
            color: themeTokens.line
        }

        MouseArea {
            id: dragArea
            anchors.left: parent.left
            anchors.top: parent.top
            anchors.bottom: parent.bottom
            anchors.right: windowControls.left
            anchors.rightMargin: 4
            acceptedButtons: Qt.LeftButton
            cursorShape: root.maximized ? Qt.ArrowCursor : Qt.OpenHandCursor
            drag.target: root.maximized ? null : root
            drag.axis: Drag.XAndYAxis
            drag.minimumX: 0
            drag.maximumX: Math.max(0, root.parent ? root.parent.width - root.width : 0)
            drag.minimumY: 54
            drag.maximumY: Math.max(54, root.parent ? root.parent.height - root.height - 48 : 54)
            onPressed: {
                cursorShape = Qt.ClosedHandCursor
                root.raiseRequested()
            }
            onReleased: {
                cursorShape = root.maximized ? Qt.ArrowCursor : Qt.OpenHandCursor
                if (!root.maximized)
                    root.geometryCommitted(root.x, root.y, root.width, root.height)
            }
            onDoubleClicked: root.toggleMaximize()

            RowLayout {
                anchors.fill: parent
                anchors.leftMargin: 12
                anchors.rightMargin: 6
                spacing: 9
                Text {
                    text: root.iconText
                    color: root.accent
                    font.family: root.iconFont
                    font.pixelSize: 14
                    font.bold: true
                }
                Text {
                    text: root.title
                    color: themeTokens.ink
                    font.family: themeTokens.text
                    font.pixelSize: 12
                    font.bold: true
                    elide: Text.ElideRight
                    Layout.fillWidth: true
                }
            }
        }

        RowLayout {
            id: windowControls
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            anchors.rightMargin: 7
            spacing: 2
            z: 2

            Repeater {
                model: [
                    { label: root.minimizeLabel, text: "–", action: "minimize" },
                    { label: root.maximizeLabel, text: root.maximized ? "❐" : "□", action: "maximize" },
                    { label: root.closeLabel, text: "×", action: "close" }
                ]
                delegate: Button {
                    required property var modelData
                    Layout.preferredWidth: 27
                    Layout.preferredHeight: 26
                    Accessible.name: modelData.label
                    ToolTip.visible: hovered
                    ToolTip.text: modelData.label
                    onClicked: {
                        if (modelData.action === "minimize")
                            root.minimizeRequested()
                        else if (modelData.action === "maximize")
                            root.toggleMaximize()
                        else
                            root.closeRequested()
                    }
                    background: Rectangle {
                        radius: 8
                        color: parent.hovered
                            ? modelData.action === "close" ? themeTokens.redTint : themeTokens.surface3
                            : "transparent"
                    }
                    contentItem: Text {
                        text: modelData.text
                        color: modelData.action === "close" && parent.hovered
                            ? themeTokens.redInk : themeTokens.ink3
                        font.pixelSize: modelData.action === "maximize" ? 11 : 16
                        horizontalAlignment: Text.AlignHCenter
                        verticalAlignment: Text.AlignVCenter
                    }
                }
            }
        }
    }

    Item {
        id: content
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: titleBar.bottom
        anchors.bottom: parent.bottom
        anchors.margins: 10
        clip: true
    }

    TapHandler {
        acceptedButtons: Qt.LeftButton
        onPressedChanged: if (pressed) root.raiseRequested()
    }

    MouseArea {
        id: resizeHandle
        width: 18
        height: 18
        anchors.right: parent.right
        anchors.bottom: parent.bottom
        visible: !root.maximized
        enabled: visible
        cursorShape: Qt.SizeFDiagCursor
        property real startWidth: 0
        property real startHeight: 0
        property real startMouseX: 0
        property real startMouseY: 0
        onPressed: {
            root.raiseRequested()
            startWidth = root.width
            startHeight = root.height
            startMouseX = mouse.x
            startMouseY = mouse.y
        }
        onPositionChanged: {
            if (!pressed)
                return
            root.width = Math.min(root.maxWidth,
                                  Math.max(root.minWidth, startWidth + mouse.x - startMouseX))
            root.height = Math.min(root.maxHeight,
                                   Math.max(root.minHeight, startHeight + mouse.y - startMouseY))
        }
        onReleased: root.geometryCommitted(root.x, root.y, root.width, root.height)
        Rectangle {
            width: 10
            height: 10
            anchors.right: parent.right
            anchors.bottom: parent.bottom
            radius: 2
            color: "transparent"
            border.color: themeTokens.lineStrong
            border.width: 1
            rotation: 45
        }
    }

    function toggleMaximize() {
        if (!root.parent)
            return
        if (!root.maximized) {
            restoredX = x
            restoredY = y
            restoredWidth = width
            restoredHeight = height
            maximized = true
            x = 12
            y = 58
            width = Math.max(minWidth, root.parent.width - 24)
            height = Math.max(minHeight, root.parent.height - 82)
        } else {
            maximized = false
            x = Math.max(0, Math.min(restoredX, root.parent.width - 140))
            y = Math.max(44, Math.min(restoredY, root.parent.height - 100))
            width = restoredWidth
            height = restoredHeight
            geometryCommitted(x, y, width, height)
        }
    }
}
