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
TEXT_TOKENS = {
    "text": Palette.text,
    "text_dim": Palette.text_dim,
    "text_muted": Palette.text_muted,
    "text_faint": Palette.text_faint,
    "accent": Palette.accent,
    "accent_dim": Palette.accent_dim,
    "violet": Palette.violet,
    "blue": Palette.blue,
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
    stay light and the foreground carries the contrast -- this pins that.
    """
    for name, tint in (
        ("ok_soft", Palette.ok_soft),
        ("warn_soft", Palette.warn_soft),
        ("err_soft", Palette.err_soft),
        ("info_soft", Palette.info_soft),
        ("accent_soft", Palette.accent_soft),
        ("violet_soft", Palette.violet_soft),
    ):
        # A soft tint is a near-white: it must be far lighter than the text it
        # sits behind, which is what makes the pair readable.
        assert contrast_ratio(_rgb(tint), _rgb(Palette.text)) >= 4.5, name
