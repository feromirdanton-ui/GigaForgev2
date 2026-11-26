"""The control-room panel is a page, so it is held to the page standard.

``gigaforge wcag`` measures a rendered page; nothing measured the panel the
product actually ships.  Measured before this test existed, on a fully
populated snapshot: the diff hunk header was ``#328df2`` on ``#f7fbff`` -- 3.24
against the 4.5 AA floor -- and the four-tab strip was 322px wide, 2px past a
320px viewport.  Both are the kind of defect that ships because no gate looks.

These tests render the real panel from a real snapshot and assert the two
properties the design work established:

* every text/background pair clears AA (the accent used as *text* is a darker
  token than the accent used as a *fill*);
* nothing overflows at 375px -- the task's adaptivity floor -- and the tab
  strip reflows rather than pushing the page sideways.

They are the regression guard for the fix, not a restatement of it: the
positive control is that the same page passes, so a check that fails
everything cannot pass this file.
"""
from __future__ import annotations

import pytest

from gigaforge.harness.browser_probe import chromium_available, unavailable_reason
from gigaforge.ui.demo_snapshot import populated_snapshot as _populated_snapshot
from gigaforge.ui.webui import render_html

PLAYWRIGHT = pytest.mark.skipif(
    not chromium_available(),
    reason=f"no launchable Chromium: {unavailable_reason()}",
)


@PLAYWRIGHT
def test_the_panel_meets_aa_contrast(tmp_path) -> None:
    """Every text/background pair on the panel clears WCAG AA."""
    from gigaforge.harness.wcag import measure

    page = tmp_path / "panel.html"
    page.write_text(render_html(_populated_snapshot()), encoding="utf-8")
    report = measure(page)

    assert not report.error, report.error
    assert report.pairs, "the panel rendered no measurable text"
    assert not report.failures, (
        "the panel has unreadable text: "
        + "; ".join(f"{p.tag} {p.ratio:.2f} < {p.required} ({p.sample!r})" for p in report.failures)
    )
    assert report.min_ratio >= 4.5


@PLAYWRIGHT
def test_the_panel_does_not_overflow_at_375px(tmp_path) -> None:
    """Nothing sticks out past a 375px viewport -- reflow, not clipping."""
    from gigaforge.harness.wcag import measure

    page = tmp_path / "panel.html"
    page.write_text(render_html(_populated_snapshot()), encoding="utf-8")
    report = measure(page)

    assert not report.error, report.error
    assert not report.overflow_375, (
        "the panel overflows at 375px: " + repr(report.overflow_375_offenders)
    )


def test_the_accent_used_as_text_is_darker_than_the_accent_used_as_fill() -> None:
    """The token split is the fix, so it is asserted directly.

    A single accent cannot be both a fill and readable text on a pale surface:
    ``#328df2`` is 3.24 on the diff background.  The panel therefore carries
    ``--accent`` (fill) and ``--accent-ink`` (text), and this test fails if a
    later edit collapses them back into one.
    """
    from gigaforge.harness.wcag import contrast_ratio
    from gigaforge.ui.webui import _CSS

    assert "--accent-ink" in _CSS, "the text accent token is gone"
    # The two tokens must differ, and the text one must clear AA on the palest
    # surface the panel paints text on.
    assert "--accent:#328df2" in _CSS
    assert "--accent-ink:#1766c2" in _CSS
    assert contrast_ratio("rgb(23,102,194)", "rgb(247,251,255)") >= 4.5
    # And the fill token, used as text, would not have.
    assert contrast_ratio("rgb(50,141,242)", "rgb(247,251,255)") < 4.5
