"""Render the QML surface offscreen and measure the pixels it actually paints.

``tests/test_qml_tokens.py`` measures the *tokens*: it reads ThemeTokens.qml,
evaluates each colour for each theme and asserts the floor.  That catches a bad
palette, but it cannot catch a token that is correct and used in the wrong
place -- a label painted with ``primary`` instead of ``primaryInk``, or a badge
whose tint is composited over something other than the field.

This script closes that gap the way ``ui_design_audit.py`` does for the web
panel: it loads Main.qml in an offscreen QGuiApplication, grabs the framebuffer,
and reports the colours actually present.

**It uses the product's own bridge, in offline mode.**  The first version of
this script loaded Main.qml bare and got a frame of 922 colours with a
``min_contrast`` of 1.0 -- because ``bridge`` was undefined, every binding that
reads it failed, and the window painted its fallback background.  Measuring
that frame would have reported a catastrophic contrast failure that no user can
see.  ``run_qml(offline=True)`` builds the real ``DashboardBridge`` with
scenario events and no model calls, which is exactly the state a design audit
needs: every panel populated, nothing reaching the network.

Needs PySide6, so it is a script and not a test -- CI installs the browser but
not Qt.

    python scripts/qml_render_audit.py --theme pearl --out /tmp/qml.png
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

QML_DIR = Path(__file__).resolve().parents[1] / "gigaforge" / "desktop" / "qml"


def _luminance(rgb: tuple[int, int, int]) -> float:
    channels = [c / 255.0 for c in rgb]
    linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    la, lb = _luminance(a), _luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def _set_theme(root, theme: str) -> bool:
    """Find the ThemeTokens object in the tree and set its theme.

    The tokens are a ``QtObject`` instantiated inside Main.qml, so they are not
    a QML singleton and cannot be reached by name from Python.  Walking the
    children for the object that has both ``themeName`` and ``field`` finds it
    without depending on the id, which is private to the file.

    **``themeName`` alone is not enough.**  Main.qml binds ``light`` to
    ``bridge.isLightTheme`` (line 537), and an explicit binding overrides the
    token object's own ``light: themeName === "pearl"``.  Setting only
    ``themeName`` therefore left ``light`` true, every colour resolved to the
    pearl branch, and all three themes rendered the same frame -- which is
    exactly what the first run of this script reported.  Both properties are
    set here, and the caller verifies the field colour actually changed.
    """
    if root is None:
        return False
    if root.property("themeName") is not None and root.property("field") is not None:
        root.setProperty("themeName", theme)
        root.setProperty("light", theme == "pearl")
        return True
    for child in root.children():
        if _set_theme(child, theme):
            return True
    return False


def _field_colour(root) -> tuple[int, int, int] | None:
    """The ``field`` colour the token object currently holds, as RGB."""
    if root is None:
        return None
    if root.property("themeName") is not None and root.property("field") is not None:
        colour = root.property("field")
        return (colour.red(), colour.green(), colour.blue())
    for child in root.children():
        found = _field_colour(child)
        if found is not None:
            return found
    return None


def render(theme: str, width: int, height: int, out: Path) -> dict:
    """Load Main.qml with the real offline bridge, grab a frame, summarise it."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("QT_QUICK_BACKEND", "software")
    os.environ.setdefault("QT_QUICK_CONTROLS_STYLE", "Basic")

    from PySide6.QtCore import QUrl
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtQml import QQmlApplicationEngine

    from gigaforge.desktop.qt_app import _make_bridge_types, _qt_bindings
    from gigaforge.desktop.app import _fallback_context

    app = QGuiApplication.instance() or QGuiApplication(sys.argv[:1])
    app.setApplicationName("GigaForge")
    app.setOrganizationName("GigaForge")

    # The offline bridge is the product's own: scenario events, no model calls.
    ctx = _fallback_context()
    Bridge = _make_bridge_types(_qt_bindings())
    bridge = Bridge(ctx, startup_message="Offline preview", view=None, offline=True)

    engine = QQmlApplicationEngine()
    engine.rootContext().setContextProperty("bridge", bridge)
    engine.load(QUrl.fromLocalFile(str(QML_DIR / "Main.qml")))
    if not engine.rootObjects():
        raise SystemExit("Main.qml did not load -- see the QML errors above")

    window = engine.rootObjects()[0]
    for obj in engine.rootObjects():
        _set_theme(obj, theme)
    window.setWidth(width)
    window.setHeight(height)
    window.show()
    for _ in range(6):
        app.processEvents()

    # Verify the theme actually took effect.  Without this the script happily
    # reports three identical frames as three themes -- which it did.
    expected = {"pearl": (231, 239, 243), "graphite": (17, 23, 28), "verdant": (8, 32, 29)}[theme]
    painted = _field_colour(window)
    if painted is not None and max(abs(a - b) for a, b in zip(painted, expected)) > 12:
        raise SystemExit(
            f"theme {theme!r} did not apply: field is {painted}, expected ~{expected}"
        )

    image = window.grabWindow() if hasattr(window, "grabWindow") else None
    if image is None:
        screen = window.screen()
        image = screen.grabWindow(0) if screen else None
    if image is None or image.isNull():
        raise SystemExit("could not grab a frame from the QML window")

    # ``grabWindow`` returns a QPixmap; ``pixelColor`` lives on QImage.
    if hasattr(image, "toImage"):
        image = image.toImage()
    image.save(str(out))
    return summarise(image)


def summarise(image) -> dict:
    """Report what the frame can actually answer, and nothing it cannot.

    **A screenshot cannot measure text contrast.**  There is no way to tell a
    9px label from a 1px decorative border by looking at pixels, and the first
    two versions of this function tried: taking every colour above a size
    threshold and reporting the minimum contrast gave 1.00 on a *correct*
    palette, because a radial gradient and a hairline border both produce large
    areas one step from the field.  Raising the threshold until the number
    looked plausible would have been fitting the metric to the answer -- the
    same defect as the old ``test_the_soft_tints_stay_light``, which compared a
    tint against the wrong colour and so never failed.

    So this reports:

    * **the field colour actually painted** -- the wiring check.  If the theme
      did not apply, this is the wrong colour and the caller fails.
    * **the structural colours** (>=0.5% of the frame) with their contrast, as
      *information*, labelled as including decoration.  ``lowest_structural``
      is the least-contrasting large area on the frame; it is usually a border
      or a gradient stop, and it is not a text measurement.

    The per-token floor is asserted by ``tests/test_qml_tokens.py``, which knows
    which colour is text because the file says so.  This script is the wiring
    check beside it, not a replacement for it.
    """
    width, height = image.width(), image.height()
    counts: Counter[tuple[int, int, int]] = Counter()
    for y in range(0, height, 2):
        for x in range(0, width, 2):
            colour = image.pixelColor(x, y)
            counts[(colour.red(), colour.green(), colour.blue())] += 1

    total = sum(counts.values())
    background = counts.most_common(1)[0][0]
    threshold = max(1, int(total * 0.005))
    structural = [(c, n) for c, n in counts.items() if n >= threshold and c != background]

    lowest = None
    for colour, _ in structural:
        ratio = _contrast(colour, background)
        if lowest is None or ratio < lowest[1]:
            lowest = (colour, ratio)

    return {
        "size": [width, height],
        "distinct_colours": len(counts),
        "field": background,
        "structural": len(structural),
        # Named for what it is: the least-contrasting large area, decoration
        # included.  Not "min_contrast", which would claim a text measurement.
        "lowest_structural": round(lowest[1], 2) if lowest else None,
        "lowest_structural_colour": lowest[0] if lowest else None,
        "structural_colours": sorted(
            ((c, n, round(_contrast(c, background), 2)) for c, n in structural),
            key=lambda t: -t[1],
        )[:8],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--theme", default="pearl", choices=["pearl", "graphite", "verdant"])
    parser.add_argument("--width", type=int, default=1440)
    parser.add_argument("--height", type=int, default=900)
    parser.add_argument("--out", default="/tmp/qml.png")
    args = parser.parse_args()

    result = render(args.theme, args.width, args.height, Path(args.out))
    print(f"theme={args.theme} size={result['size']} colours={result['distinct_colours']}")
    print(f"field={result['field']} structural={result['structural']}")
    print(
        f"lowest_structural={result['lowest_structural']} "
        f"({result['lowest_structural_colour']}) -- decoration included, not a text measure"
    )
    for colour, count, ratio in result["structural_colours"]:
        print(f"  {colour}  {count:6d}px  {ratio:.2f}")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
