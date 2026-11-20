import QtQuick

Item {
    id: root

    property string kind: "chat"
    property color accent: "#0D9488"
    property bool selected: false
    property color ring: "#3015302A"

    implicitWidth: 18
    implicitHeight: 18

    Rectangle {
        anchors.fill: parent
        radius: width / 2
        color: root.selected ? Qt.rgba(root.accent.r, root.accent.g, root.accent.b, 0.13)
                             : "transparent"
        border.color: root.selected ? Qt.rgba(root.accent.r, root.accent.g, root.accent.b, 0.42)
                                    : root.ring
        border.width: 1
    }

    Canvas {
        id: drawing
        anchors.fill: parent
        anchors.margins: parent.width * 0.12
        onPaint: {
            var ctx = getContext("2d")
            ctx.reset()
            var w = width
            var h = height
            var cx = w / 2
            var cy = h / 2
            var r = Math.min(w, h) * 0.095
            ctx.strokeStyle = root.accent.toString()
            ctx.fillStyle = root.accent.toString()
            ctx.lineWidth = Math.max(1, w * 0.075)
            ctx.lineCap = "round"
            ctx.lineJoin = "round"

            if (root.kind === "chat") {
                ctx.beginPath()
                ctx.moveTo(w * 0.21, h * 0.26)
                ctx.quadraticCurveTo(w * 0.21, h * 0.16, w * 0.33, h * 0.16)
                ctx.lineTo(w * 0.68, h * 0.16)
                ctx.quadraticCurveTo(w * 0.80, h * 0.16, w * 0.80, h * 0.29)
                ctx.lineTo(w * 0.80, h * 0.58)
                ctx.quadraticCurveTo(w * 0.80, h * 0.70, w * 0.68, h * 0.70)
                ctx.lineTo(w * 0.43, h * 0.70)
                ctx.lineTo(w * 0.28, h * 0.85)
                ctx.lineTo(w * 0.30, h * 0.68)
                ctx.quadraticCurveTo(w * 0.21, h * 0.65, w * 0.21, h * 0.56)
                ctx.closePath()
                ctx.stroke()
                for (var dot = 0; dot < 3; dot++) {
                    ctx.beginPath()
                    ctx.arc(w * (0.39 + dot * 0.14), h * 0.43, r * 0.55, 0, Math.PI * 2)
                    ctx.fill()
                }
            } else if (root.kind === "agent") {
                ctx.beginPath()
                ctx.moveTo(cx, h * 0.28)
                ctx.lineTo(w * 0.29, h * 0.72)
                ctx.lineTo(w * 0.71, h * 0.72)
                ctx.closePath()
                ctx.stroke()
                var nodes = [[cx, h * 0.22], [w * 0.25, h * 0.76], [w * 0.75, h * 0.76]]
                for (var node = 0; node < nodes.length; node++) {
                    ctx.beginPath()
                    ctx.arc(nodes[node][0], nodes[node][1], r, 0, Math.PI * 2)
                    ctx.fill()
                }
            } else if (root.kind === "notebook") {
                ctx.beginPath()
                ctx.moveTo(w * 0.27, h * 0.14)
                ctx.lineTo(w * 0.74, h * 0.14)
                ctx.lineTo(w * 0.74, h * 0.86)
                ctx.lineTo(w * 0.27, h * 0.86)
                ctx.closePath()
                ctx.stroke()
                ctx.beginPath()
                ctx.moveTo(w * 0.40, h * 0.15)
                ctx.lineTo(w * 0.40, h * 0.85)
                ctx.stroke()
                for (var line = 0; line < 3; line++) {
                    ctx.beginPath()
                    ctx.moveTo(w * 0.50, h * (0.34 + line * 0.16))
                    ctx.lineTo(w * 0.66, h * (0.34 + line * 0.16))
                    ctx.stroke()
                }
            } else if (root.kind === "subagents") {
                ctx.beginPath()
                ctx.arc(w * 0.36, h * 0.35, r * 1.2, 0, Math.PI * 2)
                ctx.stroke()
                ctx.beginPath()
                ctx.arc(w * 0.68, h * 0.40, r, 0, Math.PI * 2)
                ctx.stroke()
                ctx.beginPath()
                ctx.moveTo(w * 0.12, h * 0.78)
                ctx.quadraticCurveTo(w * 0.16, h * 0.55, w * 0.39, h * 0.59)
                ctx.quadraticCurveTo(w * 0.62, h * 0.61, w * 0.64, h * 0.79)
                ctx.stroke()
                ctx.beginPath()
                ctx.moveTo(w * 0.62, h * 0.66)
                ctx.quadraticCurveTo(w * 0.84, h * 0.58, w * 0.90, h * 0.79)
                ctx.stroke()
            } else if (root.kind === "attach") {
                ctx.beginPath()
                ctx.moveTo(w * 0.68, h * 0.20)
                ctx.bezierCurveTo(w * 0.89, h * 0.01, w * 1.02, h * 0.27, w * 0.83, h * 0.45)
                ctx.lineTo(w * 0.45, h * 0.81)
                ctx.bezierCurveTo(w * 0.27, h * 0.98, w * 0.08, h * 0.72, w * 0.27, h * 0.54)
                ctx.lineTo(w * 0.62, h * 0.22)
                ctx.bezierCurveTo(w * 0.73, h * 0.12, w * 0.86, h * 0.28, w * 0.74, h * 0.40)
                ctx.lineTo(w * 0.40, h * 0.72)
                ctx.stroke()
            } else {
                ctx.beginPath()
                ctx.arc(cx, cy, Math.min(w, h) * 0.34, 0, Math.PI * 2)
                ctx.stroke()
                ctx.beginPath()
                ctx.arc(cx, cy, Math.min(w, h) * 0.16, 0, Math.PI * 2)
                ctx.stroke()
                ctx.beginPath()
                ctx.arc(cx, cy, r * 0.8, 0, Math.PI * 2)
                ctx.fill()
                ctx.beginPath()
                ctx.moveTo(w * 0.60, h * 0.68)
                ctx.lineTo(w * 0.73, h * 0.81)
                ctx.lineTo(w * 0.94, h * 0.55)
                ctx.stroke()
            }
        }
    }

    onKindChanged: drawing.requestPaint()
    onAccentChanged: drawing.requestPaint()
    onWidthChanged: drawing.requestPaint()
    onHeightChanged: drawing.requestPaint()
}
