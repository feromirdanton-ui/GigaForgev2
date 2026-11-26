"""The report's contrast numbers must be the ones the code produces.

Three numbers in ``qa/REPORT.md`` did not reproduce: ``10.48 -> 9.85``,
``4.81 -> 4.52`` and ``5.23 -> 5.29``.  None was a before/after pair -- each
was one token measured on two different surfaces, written into a "before ->
after" column.  A reader comparing the columns concludes the fix made contrast
worse, which is the opposite of what happened.

The numbers were typed by hand from a one-off measurement, so nothing could
catch the drift.  ``scripts/contrast_table.py`` now generates the table from
the palettes, and this test asserts the report still contains that generated
output.  If a token changes and the report is not regenerated, this fails.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "qa" / "REPORT.md"
SCRIPT = ROOT / "scripts" / "contrast_table.py"


def _generated() -> str:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--markdown"],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=True,
    )
    return result.stdout.strip()


def test_the_report_contains_the_generated_contrast_table() -> None:
    """The table in the report is the table the code produces, verbatim."""
    report = REPORT.read_text(encoding="utf-8")
    generated = _generated()
    assert generated in report, (
        "qa/REPORT.md no longer matches scripts/contrast_table.py -- "
        "regenerate with `python scripts/contrast_table.py --markdown`"
    )


def test_the_report_does_not_claim_a_surface_pair_is_a_before_after() -> None:
    """The three mislabelled numbers are explained, not silently deleted.

    Deleting them would hide the mistake.  The report has to say what they
    actually were, so the next reader does not repeat the reading.
    """
    report = REPORT.read_text(encoding="utf-8")
    for number in ("10.48", "9.85", "4.81", "4.52", "5.23", "5.29"):
        assert number in report, f"{number} vanished from the report instead of being explained"
    assert "одним токеном на двух разных поверхностях" in report


def test_the_webui_comment_matches_the_measured_value() -> None:
    """``webui.py`` claimed 5.23 on the page background; it is 5.29.

    A comment that quotes a number is a claim, and this one was wrong by 0.06
    -- small, but it is the number a reader would check first.
    """
    from gigaforge.harness.wcag import contrast_ratio

    def rgb(h: str) -> str:
        v = h.lstrip("#")
        return "rgb(%d,%d,%d)" % (int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16))

    measured = contrast_ratio(rgb("#1766c2"), rgb("#f3f8fd"))
    assert round(measured, 2) == 5.29
    source = (ROOT / "gigaforge" / "ui" / "webui.py").read_text(encoding="utf-8")
    assert "5.29 on the page background" in source
    assert "5.23 on the page background" not in source
