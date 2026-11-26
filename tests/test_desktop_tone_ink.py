"""A fill tone used as a glyph must be readable.

``SegmentedControl`` (``widgets.py:409``) and ``IconBadge`` (``:450``) paint a
glyph with the *tone* -- and a glyph is text: it is read, not just seen.  The
tones are fills, chosen to be visible as a dot or a bar, so three of the five
were below AA when used as a glyph:

    accent  #4b9fea  2.78 on surface   2.49 on accent_soft
    violet  #817ce6  3.48 on surface   3.13 on violet_soft
    blue    #48b7c7  2.33 on surface   2.09 on accent_soft

``Palette.ink_for`` maps a fill tone to the value that may be used as text.
These tests pin the mapping and, more importantly, pin that the mapping is
*applied* -- the defect was never a missing colour, it was a colour used in the
wrong role.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from gigaforge.desktop.theme import Palette
from gigaforge.harness.wcag import contrast_ratio

ROOT = Path(__file__).resolve().parents[1]
FLOOR = 4.5


def _rgb(hex_colour: str) -> str:
    value = hex_colour.lstrip("#")
    return "rgb(%d,%d,%d)" % (
        int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)
    )


def _cr(fg: str, bg: str) -> float:
    return contrast_ratio(_rgb(fg), _rgb(bg))


@pytest.mark.parametrize(
    "tone,soft",
    [
        (Palette.accent, Palette.accent_soft),
        (Palette.violet, Palette.violet_soft),
        (Palette.blue, Palette.accent_soft),
        (Palette.ok, Palette.ok_soft),
        (Palette.info, Palette.info_soft),
    ],
)
def test_a_tone_used_as_a_glyph_clears_aa(tone: str, soft: str) -> None:
    """The mapped value is readable on the surface and on the badge fill."""
    ink = Palette.ink_for(tone)
    assert _cr(ink, Palette.surface) >= FLOOR, f"{tone} -> {ink} on surface"
    assert _cr(ink, soft) >= FLOOR, f"{tone} -> {ink} on {soft}"


def test_the_unmapped_fill_really_was_unreadable() -> None:
    """The negative control: without the mapping, three tones fail.

    A test that passes on the raw fill would prove nothing about the defect.
    """
    failing = [
        tone
        for tone in (Palette.accent, Palette.violet, Palette.blue)
        if _cr(tone, Palette.surface) < FLOOR
    ]
    assert len(failing) == 3, f"expected three unreadable fills, got {failing}"


def test_ink_for_leaves_unknown_tones_alone() -> None:
    """A caller passing an ink value (or a one-off) must not be rewritten."""
    assert Palette.ink_for("#123456") == "#123456"
    assert Palette.ink_for(Palette.accent_ink) == Palette.accent_ink


def test_the_glyph_sites_use_ink_for() -> None:
    """The mapping is only a fix if the two glyph sites call it.

    This is the assertion that would have caught the original defect: the
    colours existed, the helper exists, and the glyph was still painted with
    the fill.
    """
    source = (ROOT / "gigaforge" / "desktop" / "widgets.py").read_text(encoding="utf-8")
    glyph_sites = re.findall(r"create_text\([^)]*?fill=([^,)]+)", source, re.S)
    offenders = [
        site.strip()
        for site in glyph_sites
        if site.strip() in {"tone", "self.tone"}
    ]
    assert not offenders, f"glyph painted with a raw fill tone: {offenders}"
    assert source.count("ink_for(") >= 2, "the two glyph sites must both map the tone"
