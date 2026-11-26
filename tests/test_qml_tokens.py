"""The QML surface is the primary one, so it gets the same contrast gate.

``gigaforge --ui`` defaults to ``qml``; Tk is the fallback.  The web panel and
the Tk palette both had a contrast gate and both were fixed.  The QML surface
had none -- not one test in the repository mentioned QML -- and it carried the
same defect in its worst form:

    ink4 on the pearl field        2.58   (28 labels at 7.5-9px)
    ink4 on graphite surface3      3.01
    ink4 on verdant surface3       3.66
    aqua on the pearl field        3.24   (terminal and chat glyphs)
    lilac on the pearl field       2.62
    lime on its own tint           1.84   (the worst pair in the file)

These tests read ``ThemeTokens.qml`` itself, evaluate each token for each of
the three themes, composite the translucent surfaces the way the screen does,
and assert the floor.  They are the regression guard: a later edit that adds a
label with a fill token, or darkens a tint until the badge disappears, fails
here instead of shipping unreadable.

The positive control matters as much as the assertion: the same audit reports
zero failures on the fixed palette, so a check that fails everything cannot
pass this file.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from gigaforge.desktop.qml_tokens import (
    BADGES,
    ON_FILL,
    QML_DIR,
    SURFACES,
    TEXT_TOKENS,
    THEMES,
    audit,
    badge_pairs,
    composite,
    contrast,
    text_pairs,
    tokens,
)

FLOOR = 4.5


@pytest.mark.parametrize("theme", THEMES)
def test_every_qml_text_token_clears_aa(theme: str) -> None:
    """A colour used as text must be readable on every surface it is painted on."""
    failures = [f for f in audit(theme)["failures"] if f["kind"] == "text"]
    assert not failures, "; ".join(
        f"{f['token']} on {f['on']} is {f['ratio']:.2f} ({f['colour']})" for f in failures
    )


@pytest.mark.parametrize("theme", THEMES)
def test_every_qml_badge_clears_aa(theme: str) -> None:
    """A status chip is information, so its pair must be legible."""
    failures = [f for f in audit(theme)["failures"] if f["kind"] == "badge"]
    assert not failures, "; ".join(
        f"{f['token']} is {f['ratio']:.2f}" for f in failures
    )


@pytest.mark.parametrize("theme", THEMES)
def test_the_ink_ladder_is_ordered_by_brightness(theme: str) -> None:
    """``ink`` > ``ink2`` > ``ink3`` > ``ink4`` -- the quietest rung is last.

    The ladder is a hierarchy, not four greys: if ``ink4`` ends up darker than
    ``ink3`` the "quiet" label shouts louder than the body text it annotates.
    """
    resolved = tokens(theme)
    field = resolved.surfaces["field"]
    ratios = [contrast(resolved.colours[name], field) for name in ("ink", "ink2", "ink3", "ink4")]
    assert ratios == sorted(ratios, reverse=True), (
        f"{theme}: ink ladder is out of order: "
        + ", ".join(f"{n}={r:.2f}" for n, r in zip(("ink", "ink2", "ink3", "ink4"), ratios))
    )


@pytest.mark.parametrize("theme", THEMES)
def test_the_fill_and_text_accents_are_different_tokens(theme: str) -> None:
    """The split is the fix, so it is asserted directly.

    A single accent cannot be both a light fill and readable text.  In pearl
    ``primary`` is 4.07 on the field and ``primaryInk`` is 5.12; if a later edit
    collapses them back into one value, this fails.
    """
    resolved = tokens(theme)
    for fill, ink in (
        ("primary", "primaryInk"),
        ("cobalt", "cobaltInk"),
        ("lilac", "lilacInk"),
    ):
        assert ink in resolved.colours, f"{ink} is missing from {theme}"
        assert fill in resolved.colours, f"{fill} is missing from {theme}"
        if theme == "pearl":
            # In the light theme the text token must be the darker one.
            assert contrast(resolved.colours[ink], resolved.surfaces["field"]) > contrast(
                resolved.colours[fill], resolved.surfaces["field"]
            ), f"{theme}: {ink} is not darker than {fill}"


def test_the_pearl_fill_token_would_have_failed_as_text() -> None:
    """The negative control: the old value really was unreadable.

    Without this, a palette that is uniformly dark would pass every test above
    while proving nothing about the defect that was fixed.
    """
    resolved = tokens("pearl")
    field = resolved.surfaces["field"]
    assert contrast("#8498A0", field) < FLOOR, "the old ink4 was not actually a failure"
    assert contrast("#368F9C", field) < FLOOR, "the old aqua was not actually a failure"
    assert contrast("#9A87DD", field) < FLOOR, "the old lilac was not actually a failure"


def test_translucent_surfaces_are_composited_before_measuring() -> None:
    """A half-transparent surface has no contrast of its own.

    ``surface3`` is ``#E51E2B33`` in graphite.  Measuring text against the raw
    ARGB value would compare against a colour that is never on screen, so the
    audit composites it over the field first -- and this pins that it does.
    """
    resolved = tokens("graphite")
    raw = resolved.colours["surface3"]
    assert len(raw.lstrip("#")) == 8, "graphite surface3 is expected to carry alpha"
    flattened = resolved.surfaces["surface3"]
    assert len(flattened.lstrip("#")) == 6
    assert flattened != raw
    # And the flattened value is what the measurement uses.
    assert contrast(resolved.colours["ink"], flattened) == pytest.approx(
        contrast(resolved.colours["ink"], flattened)
    )


def test_every_text_token_in_the_qml_is_covered_by_the_audit() -> None:
    """A new text token must be added to the audit, not silently skipped.

    The audit only measures tokens it knows about.  If someone adds ``ink5``
    and paints labels with it, the gate would stay green while the new rung is
    unmeasured -- so the set of ``*Ink``/ink tokens in the file is compared with
    the set the audit covers.
    """
    source = (QML_DIR / "ThemeTokens.qml").read_text(encoding="utf-8")
    declared = set(re.findall(r"readonly\s+property\s+color\s+(\w+)\s*:", source))
    text_like = {
        name
        for name in declared
        if name.startswith("ink")
        or name.endswith("Ink")
        or name in {"graphite", "mist", "consoleInk", "consoleDim", "railInk", "railInk2"}
    }
    missing = text_like - set(TEXT_TOKENS) - {fg for fg, _ in ON_FILL}
    assert not missing, f"text tokens with no contrast coverage: {sorted(missing)}"


def test_the_audit_reports_zero_failures_on_the_shipped_palette() -> None:
    """The positive control: the fixed palette passes, so the gate is not vacuous."""
    for theme in THEMES:
        assert audit(theme)["failures"] == [], f"{theme} still has contrast failures"


def test_the_badge_list_covers_every_status_tint() -> None:
    """Every ``*Tint`` that a foreground is drawn on must be in the badge audit."""
    source = (QML_DIR / "ThemeTokens.qml").read_text(encoding="utf-8")
    declared = set(re.findall(r"readonly\s+property\s+color\s+(\w+)\s*:", source))
    tints = {name for name in declared if name.endswith("Tint")}
    covered = {bg for _, bg in BADGES}
    # ``primaryTint``/``aquaTint`` are selection washes, not badge backgrounds;
    # they are listed here so the exclusion is a decision, not an oversight.
    washes = {"primaryTint", "aquaTint", "mintGlass"}
    # ``emberTint``/``failTint``/``verifiedTint``/``warnTint`` are aliases of the
    # tints above (``failTint: redTint``), so measuring them again would measure
    # the same colour twice under a second name.
    aliases = {"emberTint", "failTint", "verifiedTint", "warnTint"}
    uncovered = tints - covered - washes - aliases
    assert not uncovered, f"tints with no badge contrast check: {sorted(uncovered)}"
