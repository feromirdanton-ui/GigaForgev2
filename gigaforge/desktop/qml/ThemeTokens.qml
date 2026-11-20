import QtQuick

QtObject {
    id: root

    property string themeName: "pearl"
    property bool light: themeName === "pearl"
    property bool compact: false
    property bool motion: true

    readonly property bool graphiteTheme: themeName === "graphite"
    readonly property bool verdantTheme: themeName === "verdant"

    // Three distinct materials from the styleesh2 reference: pearl glass,
    // precise graphite, and bioluminescent verdant.
    readonly property color field: light ? "#E7EFF3" : graphiteTheme ? "#11171C" : "#08201D"
    readonly property color field2: light ? "#DCE7EC" : graphiteTheme ? "#0B1116" : "#041815"
    readonly property color surface: light ? "#D7F3F8F7" : graphiteTheme ? "#18A7C6D8" : "#1896D4B5"
    readonly property color surface2: light ? "#EAF9FCFB" : graphiteTheme ? "#D21B242B" : "#CA103027"
    readonly property color surface3: light ? "#F7FCFEFF" : graphiteTheme ? "#E51E2B33" : "#E50C3429"
    readonly property color line: light ? "#FFD0DEE2" : graphiteTheme ? "#319CBBD1" : "#3189D4BD"
    readonly property color lineSoft: light ? "#FFE3EDF0" : graphiteTheme ? "#1F9CBBD1" : "#1D89D4BD"
    readonly property color lineStrong: light ? "#FFAAC1C9" : graphiteTheme ? "#609CBBD1" : "#5D89D4BD"
    readonly property color controlLine: light ? "#FF9EB8C1" : graphiteTheme ? "#718EB0C4" : "#6386EFC3"

    readonly property color ink: light ? "#0B1B24" : graphiteTheme ? "#E7ECEF" : "#E3F5EC"
    readonly property color ink2: light ? "#3C5563" : graphiteTheme ? "#DFE5E9" : "#D7EFE3"
    readonly property color ink3: light ? "#536E78" : graphiteTheme ? "#8D98A3" : "#83B3A0"
    readonly property color ink4: light ? "#8498A0" : graphiteTheme ? "#65727D" : "#648B79"
    readonly property color graphite: light ? "#435C67" : graphiteTheme ? "#C7D2D9" : "#C7E4D6"
    readonly property color mist: light ? "#596F77" : graphiteTheme ? "#91A0AA" : "#91B5A2"

    readonly property color rail: light ? "#0D282B" : graphiteTheme ? "#E7171B20" : "#E6081F18"
    readonly property color rail2: light ? "#12393A" : graphiteTheme ? "#F01A1F24" : "#F00B2B22"
    readonly property color railInk: light ? "#EAF3F6" : ink
    readonly property color railInk2: light ? "#9DB3BE" : ink3
    readonly property color railLine: light ? "#2DFFFFFF" : graphiteTheme ? "#2496B0C4" : "#2186EFC3"
    readonly property color railHover: light ? "#18FFFFFF" : graphiteTheme ? "#1C96B0C4" : "#1C86EFC3"
    readonly property color railActive: light ? "#213CE0C2" : graphiteTheme ? "#262DD4BF" : "#2634D399"
    readonly property color navActive: railActive
    readonly property color navGlow: light ? "#60E0BE" : graphiteTheme ? "#2DD4BF" : "#6EE7B7"
    readonly property color navRule: railLine

    readonly property color primary: light ? "#177F88" : graphiteTheme ? "#46CFC8" : "#44C6A0"
    readonly property color primaryDeep: light ? "#105F69" : graphiteTheme ? "#28B8B4" : "#1F9C7F"
    readonly property color primaryTint: light ? "#DDEEF0" : graphiteTheme ? "#302DD4BF" : "#2934D399"
    readonly property color aqua: light ? "#368F9C" : graphiteTheme ? "#3FC7C0" : "#54C8B2"
    readonly property color aquaInk: light ? "#256F7B" : graphiteTheme ? "#91F2E6" : "#A1EBDD"
    readonly property color cobalt: light ? "#5577C7" : graphiteTheme ? "#78A1FF" : "#9ABAF5"
    readonly property color lilac: light ? "#9A87DD" : graphiteTheme ? "#B7A6FF" : "#C3B4FF"
    readonly property color aquaTint: light ? "#DDECF3" : graphiteTheme ? "#336D8DFF" : "#315F9ED4"
    readonly property color mint: light ? "#238F76" : graphiteTheme ? "#48D4A0" : "#6ED5AD"
    readonly property color mintDeep: light ? "#0F6E5B" : graphiteTheme ? "#28BD83" : "#23A879"
    readonly property color mintGlow: light ? "#4ABF9C" : graphiteTheme ? "#63D9AE" : "#64D5A8"
    readonly property color mintGlass: light ? "#6699E5D2" : graphiteTheme ? "#3D2DD4BF" : "#6034D399"
    readonly property color mintInk: light ? "#0B7F62" : graphiteTheme ? "#A7F3D0" : "#A7F3D0"
    readonly property color mintTint: light ? "#FFE2F5EF" : graphiteTheme ? "#224ADE80" : "#2634D399"
    readonly property color amber: light ? "#D69318" : graphiteTheme ? "#FBBF24" : "#E8C268"
    readonly property color amberInk: light ? "#8A5A05" : graphiteTheme ? "#FCD77A" : "#F3D58B"
    readonly property color amberTint: light ? "#FFFDF3DC" : graphiteTheme ? "#22FBBF24" : "#22E8C268"
    readonly property color red: light ? "#C8443F" : graphiteTheme ? "#FB7185" : "#F38FB1"
    readonly property color redInk: light ? "#A5322D" : graphiteTheme ? "#FDA4AF" : "#FDA4AF"
    readonly property color redTint: light ? "#FFFBE9E8" : graphiteTheme ? "#22FB7185" : "#22F38FB1"
    readonly property color lime: light ? "#91B84C" : graphiteTheme ? "#B7D76A" : "#B2D875"
    readonly property color limeTint: light ? "#1A86B83E" : graphiteTheme ? "#22A3E635" : "#22A7E163"
    readonly property color fail: red
    readonly property color failTint: redTint
    readonly property color warn: amberInk
    readonly property color warnTint: amberTint
    readonly property color verified: mint
    readonly property color verifiedTint: mintTint

    readonly property color consoleBg: light ? "#08141D" : "#071A14"
    readonly property color console2: light ? "#10232D" : "#0B211A"
    readonly property color console3: light ? "#1B3540" : "#142D24"
    readonly property color consoleInk: light ? "#D7E6EC" : "#E4F2EC"
    readonly property color consoleDim: light ? "#94ADB7" : "#91B5A2"

    readonly property color topbarTop: light ? "#F0FFFFFF" : graphiteTheme ? "#CC1A1F24" : "#CC0B2B22"
    readonly property color topbarBottom: light ? "#DDEAF2F4" : graphiteTheme ? "#C214171B" : "#C207211A"
    readonly property color glassHighlight: light ? "#EDFFFFFF" : graphiteTheme ? "#14D2EBF0" : "#18DCFFF0"
    // Stronger surfaces stand in for backdrop blur on Qt Quick platforms where
    // native background blur is unavailable, keeping floating tools legible.
    readonly property color glassTop: light ? "#F3FFFFFF" : graphiteTheme ? "#F51A1F24" : "#F00B2B22"
    readonly property color glassBottom: light ? "#E8F4F9FA" : graphiteTheme ? "#F012171C" : "#EC0A241C"

    readonly property color actionInk: light ? "#FFFFFF" : graphiteTheme ? "#11211F" : "#06251B"

    readonly property string display: "Segoe UI"
    readonly property string text: "Segoe UI"
    readonly property string serif: "Georgia"
    readonly property string mono: "Consolas"
    readonly property string code: "Consolas"

    readonly property int pageMargin: compact ? 18 : 24
    readonly property int gap: compact ? 12 : 16
    readonly property int radius: compact ? 14 : (light ? 17 : graphiteTheme ? 11 : 20)
    readonly property int radiusInner: compact ? 10 : (graphiteTheme ? 8 : 13)
    readonly property int radiusControl: compact ? 8 : (graphiteTheme ? 7 : 10)
    readonly property int radiusPill: 999

    readonly property color paper: field
    readonly property color plate: surface
    readonly property color recess: surface2
    readonly property color rule: line
    readonly property color ruleStrong: lineStrong
    readonly property color glassEdge: line
    readonly property color ember: primary
    readonly property color emberDeep: primaryDeep
    readonly property color emberTint: primaryTint
    readonly property color navCore: rail2
    readonly property color navBottom: rail
    readonly property color navInk: railInk
    readonly property color navMuted: railInk2
    readonly property color sun: amber
    readonly property color sky: aqua
}
