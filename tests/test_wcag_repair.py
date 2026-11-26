"""The WCAG repair must actually change the colour it reports.

Measured live on the cyberpunk landing page: ``wcag --fix`` printed
``rgb(51, 51, 51) -> rgb(51, 51, 51)`` for a change that really happened.  The
cause is that ``getComputedStyle`` returns a *live* declaration — reading
``cs.color`` after ``setProperty`` returns the new value.  A report that says
"changed X to X" is worse than no report: it hides whether the repair ran.

These tests pin the pure parts (contrast maths, the threshold choice) and the
report shape, so the browser-dependent part is only the DOM walk.
"""
from __future__ import annotations

import pytest

from gigaforge.harness.wcag import (
    AA_LARGE,
    AA_NORMAL,
    ContrastPair,
    WcagReport,
    contrast_ratio,
)


def test_contrast_ratio_black_on_white_is_21():
    assert contrast_ratio("rgb(0, 0, 0)", "rgb(255, 255, 255)") == pytest.approx(21.0, abs=0.01)


def test_contrast_ratio_is_symmetric():
    a = contrast_ratio("rgb(255, 124, 0)", "rgb(255, 255, 255)")
    b = contrast_ratio("rgb(255, 255, 255)", "rgb(255, 124, 0)")
    assert a == pytest.approx(b)


def test_the_live_failure_is_below_aa():
    """White on the orange the model chose: 2.58, the number measured live."""
    ratio = contrast_ratio("rgb(255, 255, 255)", "rgb(255, 124, 0)")
    assert ratio < AA_NORMAL
    assert ratio == pytest.approx(2.58, abs=0.05)


def test_large_text_uses_the_lower_threshold():
    pair = ContrastPair(
        tag="h1", sample="Title", fg="rgb(120,120,120)", bg="rgb(255,255,255)",
        size=32.0, large=True, ratio=3.2,
    )
    assert pair.required == AA_LARGE
    assert pair.passes


def test_normal_text_at_the_same_ratio_fails():
    pair = ContrastPair(
        tag="p", sample="Body", fg="rgb(120,120,120)", bg="rgb(255,255,255)",
        size=16.0, large=False, ratio=3.2,
    )
    assert pair.required == AA_NORMAL
    assert not pair.passes


def test_report_is_not_ok_when_only_overflow_fails():
    """A readable page that scrolls sideways on a phone is still a failure."""
    report = WcagReport(
        pairs=[ContrastPair("p", "x", "rgb(0,0,0)", "rgb(255,255,255)", 16.0, False, 21.0)],
        overflow_375=True,
        min_ratio=21.0,
    )
    assert not report.ok
    assert report.failures == []


def test_report_serialises_the_numbers_a_reader_would_get():
    report = WcagReport(
        pairs=[ContrastPair("a", "Discover More", "rgb(255,255,255)", "rgb(255,124,0)",
                            16.0, False, 2.58)],
        min_ratio=2.58,
    )
    payload = report.to_dict()
    assert payload["ok"] is False
    assert payload["failures"][0]["ratio"] == 2.58
    assert payload["failures"][0]["required"] == AA_NORMAL


# ── the overflow detector must see what a clip hides ────────────────────────
#
# The original metric was ``documentElement.scrollWidth > innerWidth``.  Any
# ``overflow-x: hidden`` on html/body makes that comparison false by
# construction, because the content is clipped rather than scrolled -- and the
# harness itself used to inject exactly that rule.  Reproduced in a headless
# Chromium at 375x812 with a 1200px child: scrollWidth 375, innerWidth 375,
# overflow_detected False, child's right edge at 1208px.  The detector was
# blind to the defect it existed to find, so "overflow fixed" was unfalsifiable.

_HIDDEN_OVERFLOW_PAGE = """<!doctype html><html><head><style>
html, body { overflow-x: hidden; margin: 0; }
.wide { width: 1200px; height: 40px; background: #333; }
</style></head><body><div class="wide">wide block</div></body></html>
"""


def _playwright_available() -> bool:
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        return False
    return True


@pytest.mark.skipif(not _playwright_available(), reason="playwright is not installed")
def test_overflow_is_detected_even_when_an_ancestor_clips_it(tmp_path):
    """A clipped 1200px block at 375px is overflow, not a fixed layout.

    This is the regression the review reproduced: the metric said False while
    833px of the page sat outside the viewport with no way to scroll to it.
    """
    from gigaforge.harness.wcag import measure

    page = tmp_path / "hidden.html"
    page.write_text(_HIDDEN_OVERFLOW_PAGE, encoding="utf-8")
    report = measure(page)
    assert not report.error, report.error
    assert report.overflow_375 is True
    # The report must name the offender, not just the fact: a boolean leaves
    # the reader to find the element, and a repair needs the element.
    assert report.overflow_375_offenders
    worst = report.overflow_375_offenders[0]
    assert worst["tag"] == "div"
    assert worst["over"] > 800


@pytest.mark.skipif(not _playwright_available(), reason="playwright is not installed")
def test_a_reflowed_page_reports_no_overflow(tmp_path):
    """The control: the same content, reflowed, must measure clean.

    Without this the previous test would pass on a detector that always says
    True -- which is the same class of defect as one that always says False.
    """
    from gigaforge.harness.wcag import measure

    page = tmp_path / "reflowed.html"
    page.write_text(
        "<!doctype html><html><head><style>"
        "html, body { margin: 0; } .wide { max-width: 100%; height: 40px; }"
        "</style></head><body><div class='wide'>wide block</div></body></html>",
        encoding="utf-8",
    )
    report = measure(page)
    assert not report.error, report.error
    assert report.overflow_375 is False
    assert report.overflow_375_offenders == []


def test_the_harness_styles_do_not_clip_horizontally():
    """``overflow-x: hidden`` is a cover-up, not a fix (WCAG 1.4.10 Reflow).

    It also disables the overflow measurement, so its presence in the harness
    invariants made the WCAG gate unable to fail.

    The assertion is on the CSS the harness actually emits, not on the module's
    source text: the first version of this test scanned the file and failed on
    the comment that explains why the rule was removed.  A test that cannot
    tell a rule from a note about the rule is measuring the wrong thing.
    """
    from gigaforge.harness.generation import _layout_invariants

    css = _layout_invariants()
    assert "overflow-x: hidden" not in css
    # The reflow that replaces it must still be there, or the removal is just
    # a deletion: one column on a phone is the actual fix.
    assert "grid-template-columns: 1fr" in css


@pytest.mark.skipif(not _playwright_available(), reason="playwright is not installed")
def test_a_scroll_box_is_reflow_not_overflow(tmp_path):
    """A wide table in ``overflow-x:auto`` is reachable, so it is not a defect.

    The gate had only two states: a clipped page and a fitting page.  A correct
    responsive page -- wide table inside a scroll box -- measured as overflow,
    so the gate could not pass the very layout WCAG asks for.  A gate that can
    only fail is a gate people switch off, which returns the project to having
    no gate at all.

    The control is the next test: the same geometry with ``overflow:hidden``
    must still fail, or this change turned the detector off instead of teaching
    it the difference.
    """
    from gigaforge.harness.wcag import measure

    page = tmp_path / "scrollbox.html"
    page.write_text(
        "<!doctype html><html><head><style>"
        "html,body{margin:0} .scroll{overflow-x:auto;max-width:100%}"
        "table{width:465px;border-collapse:collapse}"
        "</style></head><body>"
        "<div class='scroll'><table><tr><th>a</th><th>b</th><th>c</th></tr>"
        "<tr><td>1</td><td>2</td><td>3</td></tr></table></div>"
        "</body></html>",
        encoding="utf-8",
    )
    report = measure(page)
    assert not report.error, report.error
    assert report.overflow_375 is False, report.overflow_375_offenders


@pytest.mark.skipif(not _playwright_available(), reason="playwright is not installed")
def test_the_same_geometry_clipped_still_fails(tmp_path):
    """``overflow:hidden`` hides the content instead of letting the reader reach it."""
    from gigaforge.harness.wcag import measure

    page = tmp_path / "clipped.html"
    page.write_text(
        "<!doctype html><html><head><style>"
        "html,body{margin:0} .clip{overflow:hidden;max-width:100%}"
        "table{width:900px;border-collapse:collapse}"
        "</style></head><body>"
        "<div class='clip'><table><tr><th>a</th><th>b</th><th>c</th></tr>"
        "<tr><td>1</td><td>2</td><td>3</td></tr></table></div>"
        "</body></html>",
        encoding="utf-8",
    )
    report = measure(page)
    assert not report.error, report.error
    assert report.overflow_375 is True, "clipped content must still count"
