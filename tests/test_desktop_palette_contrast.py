"""The desktop palette is a screen, so its text is held to the same floor.

The web panel had a contrast gate; the Tk/Qt desktop surface had none, and it
carried the same defect in a worse form.  Measured on the palette as it was:

    ok      on ok_soft    2.73      text_muted on bg        3.24
    accent  on bg         2.61      text_faint on bg        2.18
    warn    on warn_soft  3.21      diff_meta  on panel     3.39

Every one of those is a *text* pair -- a status badge, a tile label, a table
heading, a diff annotation -- so every one of them is read, and every one of
them was below the 4.5 AA floor.

The fix is to give each token the value that works in the role it is actually
used in.  ``accent``, ``violet`` and ``blue`` are used as text as well as fill,
so they carry the readable value; the ``*_soft`` tints stay light.  These tests
assert the palette, not the individual call sites, so a later edit that adds a
new label with a light token fails here instead of shipping unreadable.
"""
from __future__ import annotations

import pytest

from gigaforge.harness.wcag import contrast_ratio
from gigaforge.desktop.theme import Palette

#: Every surface the desktop paints text on.  ``raised`` is the darkest, so a
#: pair that clears AA there clears it everywhere.
SURFACES = {
    "bg": Palette.bg,
    "surface": Palette.surface,
    "panel": Palette.panel,
    "panel_alt": Palette.panel_alt,
    "raised": Palette.raised,
}

#: The tokens that are used as text somewhere in the desktop UI.
#:
#: ``accent``/``violet``/``blue`` are NOT here: they are fills (bars, carets,
#: outlines, chart tones).  Their text roles use ``accent_ink``/``violet_ink``/
#: ``blue_ink``.  Listing the fills here is what forced ``5e0842e`` to darken
#: every bar in the product to make a label readable.
TEXT_TOKENS = {
    "text": Palette.text,
    "text_dim": Palette.text_dim,
    "text_muted": Palette.text_muted,
    "text_faint": Palette.text_faint,
    "accent_ink": Palette.accent_ink,
    "accent_dim": Palette.accent_dim,
    "violet_ink": Palette.violet_ink,
    "blue_ink": Palette.blue_ink,
    "ok": Palette.ok,
    "warn": Palette.warn,
    "err": Palette.err,
    "info": Palette.info,
    "diff_hunk": Palette.diff_hunk,
    "diff_meta": Palette.diff_meta,
    "diff_add_fg": Palette.diff_add_fg,
    "diff_del_fg": Palette.diff_del_fg,
}


def _rgb(hex_colour: str) -> str:
    value = hex_colour.lstrip("#")
    return "rgb(%d,%d,%d)" % (
        int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)
    )


def _luminance(hex_colour: str) -> float:
    """WCAG relative luminance of an opaque colour."""
    value = hex_colour.lstrip("#")
    channels = [int(value[i : i + 2], 16) / 255.0 for i in (0, 2, 4)]
    linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


@pytest.mark.parametrize("token", sorted(TEXT_TOKENS))
def test_every_text_token_clears_aa_on_every_surface(token: str) -> None:
    """A colour used as text must be readable wherever it is painted."""
    fg = _rgb(TEXT_TOKENS[token])
    for surface, colour in SURFACES.items():
        ratio = contrast_ratio(fg, _rgb(colour))
        assert ratio >= 4.5, (
            f"{token} ({TEXT_TOKENS[token]}) on {surface} ({colour}) is "
            f"{ratio:.2f}, below the 4.5 AA floor"
        )


@pytest.mark.parametrize(
    "status",
    ["ok", "succeeded", "accepted", "pass", "running", "warn", "warning",
     "reject", "rejected", "failed", "fail", "error", "cached", "idle", "skipped"],
)
def test_every_status_badge_is_readable(status: str) -> None:
    """``Palette.status`` returns a text pair, so the pair must be legible."""
    fg, bg = Palette.status(status)
    ratio = contrast_ratio(_rgb(fg), _rgb(bg))
    assert ratio >= 4.5, f"status {status!r}: {fg} on {bg} is {ratio:.2f}"


def test_the_soft_tints_stay_light() -> None:
    """The fix darkened the text, not the backgrounds.

    If someone "fixes" contrast by darkening ``ok_soft``/``accent_soft``, the
    badges lose their soft look and the whole surface turns muddy.  The tints
    stay light and the foreground carries the contrast.

    The metric is the tint's own luminance, not its contrast against ``text``.
    The old version compared each tint with the dark body colour and asserted
    >= 4.5 -- which is true of *any* pale tint and of a mid-grey one too, so it
    passed on every mutation of a tint and measured nothing.  A soft tint is
    defined by being near-white: relative luminance >= 0.80, which a darkened
    tint cannot reach.
    """
    for name, tint in (
        ("ok_soft", Palette.ok_soft),
        ("warn_soft", Palette.warn_soft),
        ("err_soft", Palette.err_soft),
        ("info_soft", Palette.info_soft),
        ("accent_soft", Palette.accent_soft),
        ("violet_soft", Palette.violet_soft),
    ):
        luminance = _luminance(tint)
        assert luminance >= 0.80, (
            f"{name} ({tint}) has luminance {luminance:.3f}; a soft tint must stay "
            "near-white (>= 0.80) -- darkening it is not a contrast fix"
        )


def test_the_soft_tint_metric_catches_a_darkened_tint() -> None:
    """The negative control for the metric above.

    Without this, a metric that passes everything looks identical to a metric
    that works.  A tint darkened to mid-grey must fail.
    """
    assert _luminance("#7a8a86") < 0.80
    assert _luminance("#4b6b94") < 0.80


def test_the_text_ladder_is_ordered_by_brightness() -> None:
    """``text`` > ``text_dim`` > ``text_muted`` > ``text_faint``.

    ``5e0842e`` cleared the AA floor but left ``text_faint`` (#556a80, 5.18)
    darker than ``text_muted`` (#5a6f85, 4.81), so the quietest rung shouted
    louder than the one above it.  A floor without an order is half a fix.
    """
    ratios = [
        contrast_ratio(_rgb(colour), _rgb(Palette.bg))
        for colour in (Palette.text, Palette.text_dim, Palette.text_muted, Palette.text_faint)
    ]
    assert ratios == sorted(ratios, reverse=True), (
        "text ladder is out of order: "
        + ", ".join(
            f"{n}={r:.2f}"
            for n, r in zip(("text", "text_dim", "text_muted", "text_faint"), ratios)
        )
    )


def test_the_action_fill_carries_white_text() -> None:
    """The primary button is a fill with a white label, so it is measured as one.

    ``accent`` is now a light fill again (#4b9fea, 2.82 against white) and must
    never be used behind white text.  ``action`` is the darker value that can
    be: 5.65.
    """
    assert contrast_ratio(_rgb(Palette.action), _rgb("#ffffff")) >= 4.5
    assert contrast_ratio(_rgb(Palette.accent), _rgb("#ffffff")) < 4.5, (
        "accent is a fill for bars and carets, not a button background"
    )


def test_the_fill_and_text_accents_are_different_tokens() -> None:
    """The split is the fix, so it is asserted directly."""
    for fill, ink in (
        ("accent", "accent_ink"),
        ("violet", "violet_ink"),
        ("blue", "blue_ink"),
    ):
        fill_value = getattr(Palette, fill)
        ink_value = getattr(Palette, ink)
        assert fill_value != ink_value, f"{fill} and {ink} collapsed back into one value"
        assert contrast_ratio(_rgb(ink_value), _rgb(Palette.bg)) > contrast_ratio(
            _rgb(fill_value), _rgb(Palette.bg)
        ), f"{ink} is not the darker (readable) value"
