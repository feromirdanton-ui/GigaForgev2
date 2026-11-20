import QtQuick
import QtQuick.Controls

Popup {
    id: root
    objectName: "navigationHoverLabel"

    property Item anchorItem: null
    property string label: ""
    property bool hoverActive: false
    property real textScale: 1
    property real densityScale: 1

    ThemeTokens {
        id: themeTokens
        themeName: bridge.appearanceTheme
        light: bridge.isLightTheme
        compact: bridge.density === "compact"
    }

    parent: Overlay.overlay
    modal: false
    focus: false
    closePolicy: Popup.NoAutoClose
    z: 10000
    width: labelText.implicitWidth + 22 * root.densityScale
    height: labelText.implicitHeight + 14 * root.densityScale
    x: {
        if (!root.anchorItem || !Overlay.overlay)
            return 0
        return root.anchorItem.mapToItem(
            Overlay.overlay,
            root.anchorItem.width + 9 * root.densityScale,
            root.anchorItem.height / 2
        ).x
    }
    y: {
        if (!root.anchorItem || !Overlay.overlay)
            return 0
        const point = root.anchorItem.mapToItem(
            Overlay.overlay,
            root.anchorItem.width + 9 * root.densityScale,
            root.anchorItem.height / 2
        )
        return Math.max(
            8,
            Math.min(point.y - height / 2, Overlay.overlay.height - height - 8)
        )
    }

    contentItem: Text {
        id: labelText
        text: root.label
        color: themeTokens.railInk
        font.family: themeTokens.text
        font.pixelSize: Math.round(10.5 * root.textScale)
        font.weight: Font.DemiBold
        leftPadding: 11 * root.densityScale
        rightPadding: 11 * root.densityScale
        topPadding: 7 * root.densityScale
        bottomPadding: 7 * root.densityScale
        verticalAlignment: Text.AlignVCenter
    }

    background: Rectangle {
        objectName: "navigationHoverBackground"
        radius: 12 * root.densityScale
        color: themeTokens.verdantTheme ? "#F0113028"
            : themeTokens.graphiteTheme ? "#F0222A31" : "#F00D282D"
        border.color: themeTokens.lineStrong
        border.width: 1
    }

    enter: Transition {
        NumberAnimation { property: "opacity"; from: 0; to: 1; duration: 110 }
    }
    exit: Transition {
        NumberAnimation { property: "opacity"; from: 1; to: 0; duration: 90 }
    }

    Timer {
        id: showDelay
        interval: 320
        onTriggered: {
            if (root.hoverActive && root.anchorItem)
                root.open()
        }
    }

    onHoverActiveChanged: {
        showDelay.stop()
        if (hoverActive)
            showDelay.start()
        else
            close()
    }
}
