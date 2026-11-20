import QtQuick

Item {
    id: root

    property real energy: 0.14
    property bool active: true
    property bool motionEnabled: true
    property color accent: "#0D9488"
    property color secondary: "#3B5BDB"
    property string logoSource: "../../assets/gigaforge-mark.png"
    property real pulse: 0.55

    readonly property real boundedEnergy: Math.max(0, Math.min(1, energy))
    readonly property int orbitDuration: boundedEnergy > 0.72 ? 5600 : 14500

    Canvas {
        id: aura
        anchors.fill: parent
        onPaint: {
            var ctx = getContext("2d")
            ctx.reset()
            var cx = width / 2
            var cy = height / 2
            var radius = Math.max(width, height) * 0.48
            var color = root.accent.toString()
            var glow = ctx.createRadialGradient(cx, cy, 2, cx, cy, radius)
            glow.addColorStop(0, color)
            glow.addColorStop(0.22, "rgba(255,255,255,0.28)")
            glow.addColorStop(1, "rgba(255,255,255,0)")
            ctx.globalAlpha = 0.12 + root.boundedEnergy * 0.20 * root.pulse
            ctx.fillStyle = glow
            ctx.fillRect(0, 0, width, height)

            ctx.globalAlpha = 0.14 + root.boundedEnergy * 0.14
            ctx.strokeStyle = root.secondary.toString()
            ctx.lineWidth = 0.8
            ctx.save()
            ctx.translate(cx, cy)
            ctx.scale(1, 0.58)
            for (var orbit = 0; orbit < 3; orbit++) {
                ctx.beginPath()
                ctx.arc(0, 0, Math.min(width * 0.28, height * 0.39) + orbit * 10, 0, Math.PI * 2)
                ctx.stroke()
            }
            ctx.restore()
            ctx.globalAlpha = 1
        }
        onWidthChanged: requestPaint()
        onHeightChanged: requestPaint()
    }

    Connections {
        target: root
        function onPulseChanged() { aura.requestPaint() }
        function onEnergyChanged() { aura.requestPaint() }
        function onAccentChanged() { aura.requestPaint() }
        function onSecondaryChanged() { aura.requestPaint() }
    }

    Item {
        id: outerOrbit
        width: parent.width * 0.86
        height: parent.height * 0.52
        anchors.centerIn: parent
        NumberAnimation on rotation {
            running: root.motionEnabled && root.active
            loops: Animation.Infinite
            from: 0
            to: 360
            duration: root.orbitDuration
        }
        Rectangle {
            width: 7 + root.boundedEnergy * 2
            height: width
            radius: width / 2
            x: outerOrbit.width * 0.14
            y: outerOrbit.height / 2 - height / 2
            color: root.accent
            border.width: 1
            border.color: "#DFFFFFFF"
        }
    }

    Item {
        id: innerOrbit
        width: parent.width * 0.66
        height: parent.height * 0.38
        anchors.centerIn: parent
        NumberAnimation on rotation {
            running: root.motionEnabled && root.active
            loops: Animation.Infinite
            from: 360
            to: 0
            duration: Math.round(root.orbitDuration * 0.72)
        }
        Rectangle {
            width: 5
            height: width
            radius: width / 2
            x: innerOrbit.width * 0.88
            y: innerOrbit.height / 2 - height / 2
            color: root.secondary
            border.width: 1
            border.color: "#DFFFFFFF"
        }
    }

    Image {
        id: coreMark
        anchors.fill: parent
        anchors.margins: 8
        source: root.logoSource
        sourceSize.width: 180
        sourceSize.height: 180
        fillMode: Image.PreserveAspectFit
        smooth: true
        scale: 1
        SequentialAnimation {
            running: root.motionEnabled && root.active
            loops: Animation.Infinite
            onStopped: coreMark.scale = 1
            NumberAnimation {
                target: coreMark
                property: "scale"
                to: 1.025 + root.boundedEnergy * 0.035
                duration: root.boundedEnergy > 0.72 ? 720 : 2100
                easing.type: Easing.InOutSine
            }
            NumberAnimation {
                target: coreMark
                property: "scale"
                to: 1
                duration: root.boundedEnergy > 0.72 ? 720 : 2100
                easing.type: Easing.InOutSine
            }
        }
    }

    SequentialAnimation on pulse {
        running: root.motionEnabled && root.active
        loops: Animation.Infinite
        onStopped: root.pulse = 0.72
        NumberAnimation {
            to: root.boundedEnergy > 0.72 ? 1 : 0.82
            duration: root.boundedEnergy > 0.72 ? 680 : 1900
            easing.type: Easing.InOutSine
        }
        NumberAnimation {
            to: 0.42
            duration: root.boundedEnergy > 0.72 ? 680 : 1900
            easing.type: Easing.InOutSine
        }
    }

    onMotionEnabledChanged: {
        if (!motionEnabled)
            pulse = 0.72
    }
    onActiveChanged: {
        if (!active)
            pulse = 0.72
    }
}
