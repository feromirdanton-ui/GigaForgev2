"""Read the QML design tokens and measure them the way a screen shows them.

``ThemeTokens.qml`` is the surface the product actually ships: ``gigaforge
--ui`` defaults to ``qml`` and Tk is the fallback.  It had no contrast gate at
all -- not one test in the repository mentioned QML -- so the same defect the
web panel and the Tk palette were fixed for was still live on the primary
surface: ``ink4`` at ``#8498A0`` is 2.58 on the pearl field, and it is the
colour of 28 labels set at 7.5-9px.

Three things make QML harder to measure than CSS, and all three are handled
here:

* **The tokens are QML expressions, not values.**  ``ink4: light ? "#8498A0" :
  graphiteTheme ? "#65727D" : "#648B79"`` is three palettes in one line, so a
  measurement has to evaluate the expression for a named theme.  A regex that
  grabs the first literal measures pearl and silently reports it as graphite.
* **The surfaces are translucent.**  ``surface3`` is ``#F7FCFEFF`` in pearl but
  ``#E51E2B33`` in graphite -- an ARGB colour with alpha.  Contrast against a
  half-transparent background is not defined, so every surface is composited
  over the field it sits on before it is measured.
* **Not every token sits on the field.**  ``railInk`` is text on the dark rail,
  ``consoleInk`` is text on the console.  Measuring them against the pale field
  reports a 1.03 "failure" that no user can see.  Each token therefore carries
  the surfaces it is actually painted on.

The module is deliberately dependency-free (no Qt import): it parses the QML
source, so it runs in CI where PySide6 is not installed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from gigaforge.harness.wcag import contrast_ratio

__all__ = [
    "THEMES",
    "TokenSet",
    "tokens",
    "composite",
    "contrast",
    "text_pairs",
    "badge_pairs",
    "audit",
    "QML_DIR",
]

QML_DIR = Path(__file__).resolve().parent / "qml"
THEME_FILE = QML_DIR / "ThemeTokens.qml"

#: The three palettes ``ThemeTokens.qml`` carries.
THEMES = ("pearl", "graphite", "verdant")

#: Surfaces text is painted on, in the order a page stacks them.  Each is
#: composited over ``field`` before use, because they carry alpha.
SURFACES = ("field", "surface", "surface2", "surface3")

#: ``token -> surfaces it is painted on``.  A token that is only ever a fill
#: (``primaryTint``, ``mintGlass``, ``railHover``) is absent: it has to be seen,
#: not read, and holding it to 4.5 would force every tint dark.
TEXT_TOKENS: dict[str, tuple[str, ...]] = {
    "ink": SURFACES,
    "ink2": SURFACES,
    "ink3": SURFACES,
    "ink4": SURFACES,
    "graphite": SURFACES,
    "mist": SURFACES,
    "primaryInk": SURFACES,
    "primaryDeep": SURFACES,
    "aquaInk": SURFACES,
    "mintInk": SURFACES,
    "mintDeep": SURFACES,
    "amberInk": SURFACES,
    "redInk": SURFACES,
    "cobaltInk": SURFACES,
    "lilacInk": SURFACES,
    # The rail is a dark strip inside a light page; its text never touches the
    # field, so it is measured against the rail it is drawn on.
    "railInk": ("rail", "rail2"),
    "railInk2": ("rail", "rail2"),
    "navInk": ("rail", "rail2"),
    # The console is its own dark surface.
    "consoleInk": ("consoleBg", "console2", "console3"),
    "consoleDim": ("consoleBg", "console2", "console3"),
}

#: Foregrounds drawn on a *fill* rather than on a surface: the label on a
#: primary button.  ``actionInk`` is white in pearl (4.74 on ``primary``) and
#: near-black in the dark themes, because the fill is light there.
ON_FILL = (("actionInk", "primary"),)

#: Badge pairs: a foreground token on the tint it is drawn on.  These are the
#: status chips -- the colour is information, not decoration.
BADGES = (
    ("primaryInk", "primaryTint"),
    ("aquaInk", "aquaTint"),
    ("mintInk", "mintTint"),
    ("amberInk", "amberTint"),
    ("redInk", "redTint"),
    ("lime", "limeTint"),
)

_PROP = re.compile(
    r"readonly\s+property\s+color\s+(\w+)\s*:\s*(.+?)(?=\n\s*readonly\s|\n\s*property\s|\Z)",
    re.S,
)


@dataclass(frozen=True)
class TokenSet:
    """The resolved colours of one theme, plus the surfaces they sit on."""

    theme: str
    colours: dict[str, str]
    surfaces: dict[str, str]

    def __getitem__(self, name: str) -> str:
        return self.colours[name]


def _parse_colour(literal: str) -> tuple[float, int, int, int] | None:
    """``#RRGGBB`` or ``#AARRGGBB`` -> ``(alpha, r, g, b)``."""
    value = literal.strip().strip('"')
    if not value.startswith("#"):
        return None
    digits = value[1:]
    try:
        if len(digits) == 6:
            return 1.0, int(digits[0:2], 16), int(digits[2:4], 16), int(digits[4:6], 16)
        if len(digits) == 8:
            return (
                int(digits[0:2], 16) / 255.0,
                int(digits[2:4], 16),
                int(digits[4:6], 16),
                int(digits[6:8], 16),
            )
    except ValueError:
        return None
    return None


def _split_ternary(text: str) -> tuple[str, str, str] | None:
    """Split ``cond ? a : b`` at the top-level ``?`` and its matching ``:``.

    ``b`` may itself be a ternary, which is exactly the shape the file uses
    (``light ? A : graphiteTheme ? B : C``), so the split is right-associative
    and the ``:`` is found by counting nested ternaries.
    """
    depth = 0
    question = -1
    for i, ch in enumerate(text):
        if ch == "?":
            if depth == 0 and question < 0:
                question = i
            depth += 1
        elif ch == ":":
            depth -= 1
            if depth == 0 and question >= 0:
                return text[:question].strip(), text[question + 1 : i].strip(), text[i + 1 :].strip()
    return None


def _evaluate(expression: str, theme: str, colours: dict[str, str]) -> str | None:
    """Evaluate a QML colour expression for one theme.

    Only the two flags the file actually uses are known (``light`` and
    ``graphiteTheme``); an expression that needs anything else returns ``None``
    so the caller reports it instead of measuring the wrong branch.
    """
    text = " ".join(expression.split())
    split = _split_ternary(text)
    if split is None:
        value = text.strip().strip('"')
        if value.startswith("#"):
            return value
        # An alias to another token (``railInk: ink`` in the dark themes).
        return colours.get(value)
    condition, when_true, when_false = split
    if condition == "light":
        chosen = when_true if theme == "pearl" else when_false
    elif condition == "graphiteTheme":
        chosen = when_true if theme == "graphite" else when_false
    else:
        return None
    return _evaluate(chosen, theme, colours)


def tokens(theme: str = "pearl", path: Path | None = None) -> TokenSet:
    """Resolve every colour token of *theme* from the QML source."""
    if theme not in THEMES:
        raise ValueError(f"unknown theme {theme!r}; expected one of {THEMES}")
    source = (path or THEME_FILE).read_text(encoding="utf-8")
    # Strip ``//`` comments before parsing.  A comment that mentions a colour
    # (and the ones in this file do, to record the before/after numbers) would
    # otherwise be captured as part of the preceding expression, and the
    # evaluator would measure a sentence.  That is not hypothetical: it made
    # verdant's ``failTint`` resolve to a paragraph of prose.
    source = re.sub(r"//[^\n]*", "", source)
    expressions = dict(_PROP.findall(source))

    colours: dict[str, str] = {}
    # Two passes so an alias can point at a token defined later in the file.
    for _ in range(3):
        for name, expression in expressions.items():
            if name in colours:
                continue
            value = _evaluate(expression, theme, colours)
            if value and value.startswith("#"):
                colours[name] = value

    field = colours.get("field", "#FFFFFF")
    surfaces: dict[str, str] = {"field": _hex(_parse_colour(field))}
    for name in SURFACES[1:]:
        surfaces[name] = composite(colours.get(name, field), field)
    # The rail and console are opaque-ish strips; composite them over the field
    # too, so a translucent rail is measured as it renders.
    for name in ("rail", "rail2", "consoleBg", "console2", "console3"):
        if name in colours:
            surfaces[name] = composite(colours[name], field)
    return TokenSet(theme=theme, colours=colours, surfaces=surfaces)


def _hex(rgba: tuple[float, int, int, int] | None) -> str:
    if rgba is None:
        return "#000000"
    _, r, g, b = rgba
    return "#%02x%02x%02x" % (r, g, b)


def composite(foreground: str, background: str) -> str:
    """Flatten an ARGB colour onto an opaque one, as the screen does."""
    fg = _parse_colour(foreground)
    bg = _parse_colour(background)
    if fg is None:
        return background
    if bg is None:
        bg = (1.0, 255, 255, 255)
    alpha, r, g, b = fg
    _, br, bgc, bb = bg
    return "#%02x%02x%02x" % (
        int(round(r * alpha + br * (1 - alpha))),
        int(round(g * alpha + bgc * (1 - alpha))),
        int(round(b * alpha + bb * (1 - alpha))),
    )


def _rgb(colour: str) -> str:
    value = colour.lstrip("#")
    if len(value) == 8:
        value = value[2:]
    return "rgb(%d,%d,%d)" % (
        int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)
    )


def contrast(foreground: str, background: str) -> float:
    """Contrast of *foreground* on *background*, both possibly translucent."""
    return contrast_ratio(_rgb(composite(foreground, background)), _rgb(background))


def text_pairs(theme: str = "pearl") -> list[tuple[str, str, str, float]]:
    """``(token, surface, colour, ratio)`` for every text token on its surfaces."""
    resolved = tokens(theme)
    rows: list[tuple[str, str, str, float]] = []
    for name, surfaces in TEXT_TOKENS.items():
        colour = resolved.colours.get(name)
        if colour is None:
            continue
        for surface in surfaces:
            if surface not in resolved.surfaces:
                continue
            rows.append((name, surface, colour, contrast(colour, resolved.surfaces[surface])))
    return rows


def badge_pairs(theme: str = "pearl") -> list[tuple[str, str, float]]:
    """``(pair, tint, ratio)`` for every status badge."""
    resolved = tokens(theme)
    rows: list[tuple[str, str, float]] = []
    for fg_name, bg_name in BADGES:
        fg = resolved.colours.get(fg_name)
        bg = resolved.colours.get(bg_name)
        if fg is None or bg is None:
            continue
        tint = composite(bg, resolved.surfaces["field"])
        rows.append((f"{fg_name}/{bg_name}", bg_name, contrast(fg, tint)))
    return rows


def audit(theme: str = "pearl", floor: float = 4.5) -> dict[str, list]:
    """Everything that fails the floor, grouped so a report can name it."""
    failures: list[dict] = []
    for name, surface, colour, ratio in text_pairs(theme):
        if ratio < floor:
            failures.append(
                {"kind": "text", "token": name, "on": surface, "colour": colour, "ratio": ratio}
            )
    for pair, tint, ratio in badge_pairs(theme):
        if ratio < floor:
            failures.append(
                {"kind": "badge", "token": pair, "on": tint, "ratio": ratio}
            )
    resolved = tokens(theme)
    for fg_name, fill_name in ON_FILL:
        fg = resolved.colours.get(fg_name)
        fill = resolved.colours.get(fill_name)
        if fg is None or fill is None:
            continue
        ratio = contrast(fg, fill)
        if ratio < floor:
            failures.append(
                {"kind": "on_fill", "token": f"{fg_name}/{fill_name}",
                 "on": fill_name, "ratio": ratio}
            )
    return {"theme": theme, "failures": failures}
