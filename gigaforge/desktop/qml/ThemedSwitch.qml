import QtQuick
import QtQuick.Controls

Switch {
    id: control

    property color accent: "#17AFA0"
    property color trackColor: "#DCE8EB"
    property color trackBorder: "#B9CFD4"
    property color labelColor: "#49636B"
    property color activeLabelColor: "#16383E"

    spacing: control.text.length > 0 ? 9 : 0

    indicator: Rectangle {
        implicitWidth: 42
        implicitHeight: 24
        x: control.mirrored ? control.width - width : 0
        y: control.topPadding + (control.availableHeight - height) / 2
        radius: height / 2
        color: control.checked ? control.accent : control.trackColor
        border.color: control.activeFocus ? control.accent : control.trackBorder
        border.width: control.activeFocus ? 2 : 1

        Rectangle {
            width: 18
            height: 18
            y: 2
            x: control.checked ? parent.width - width - 3 : 3
            radius: width / 2
            color: "#FFFFFF"
            border.color: control.checked ? "#FFFFFF" : control.trackBorder
            border.width: 1
            Behavior on x {
                NumberAnimation {
                    duration: 130
                    easing.type: Easing.OutCubic
                }
            }
        }
    }

    contentItem: Text {
        text: control.text
        font: control.font
        color: control.checked ? control.activeLabelColor : control.labelColor
        opacity: control.enabled ? 1 : 0.55
        verticalAlignment: Text.AlignVCenter
        leftPadding: control.indicator.width + control.spacing
        elide: Text.ElideRight
    }
}
