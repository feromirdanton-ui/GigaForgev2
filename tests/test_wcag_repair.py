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
