"""A hunk whose context carries the wrong indentation must be repaired.

The live bench found this on the real model: asked to fix ``validate_email``,
it emitted a hunk whose *context* lines carried ten spaces where the file has
four, and whose added line carried the same ten.  ``git apply`` accepts that —
it matches on content, not on indentation, and fuzzes the offset — so the diff
"applies" and the file no longer parses: ``unexpected indent`` at the line the
model just wrote.  Two of seven live tasks failed this way.

The repair is deterministic: locate the hunk's old side in the file by stripped
content, read the real indentation, shift the added lines by the same delta, and
accept the result only if the reconstructed file parses.
"""
from __future__ import annotations

import ast
from pathlib import Path

from gigaforge.harness.diff_repair import normalize_hunk_indentation

_SOURCE = '''"""Toy math utilities."""

import re


def validate_email(email: str) -> bool:
    """Return True iff *email* looks like a standard address."""
    return bool(_EMAIL_RE.match(email))
'''


def _write(tmp_path: Path) -> Path:
    target = tmp_path / "math_utils.py"
    target.write_text(_SOURCE, encoding="utf-8")
    return target


def test_over_indented_context_is_repaired(tmp_path: Path) -> None:
    """The exact shape the live model produced: 10 spaces where the file has 4."""
    _write(tmp_path)
    diff = (
        "--- a/math_utils.py\n"
        "+++ b/math_utils.py\n"
        "@@ -6,4 +6,4 @@ def validate_email(email: str) -> bool:\n"
        '           """Return True iff *email* looks like a standard address."""\n'
        "-          return bool(_EMAIL_RE.match(email))\n"
        "+          return _EMAIL_RE.fullmatch(email) is not None\n"
    )
    repaired, report = normalize_hunk_indentation(diff, tmp_path)

    assert report.changed, "the over-indented hunk was not repaired"
    assert "math_utils.py" in report.repaired
    # The added line must now carry the file's real indentation (4 spaces).
    added = [ln for ln in repaired.splitlines() if ln.startswith("+") and not ln.startswith("+++")]
    assert added == ["+    return _EMAIL_RE.fullmatch(email) is not None"], added

    # And the reconstructed file must parse — that is the whole point.
    body = _SOURCE.replace(
        "    return bool(_EMAIL_RE.match(email))",
        "    return _EMAIL_RE.fullmatch(email) is not None",
    )
    ast.parse(body)


def test_correctly_indented_hunk_is_untouched(tmp_path: Path) -> None:
    """The normaliser must not perturb the common case, byte for byte."""
    _write(tmp_path)
    diff = (
        "--- a/math_utils.py\n"
        "+++ b/math_utils.py\n"
        "@@ -6,3 +6,3 @@\n"
        '     """Return True iff *email* looks like a standard address."""\n'
        "-    return bool(_EMAIL_RE.match(email))\n"
        "+    return _EMAIL_RE.fullmatch(email) is not None\n"
    )
    repaired, report = normalize_hunk_indentation(diff, tmp_path)
    assert repaired == diff
    assert not report.changed


def test_repair_that_would_not_parse_is_rejected(tmp_path: Path) -> None:
    """A repair that leaves the file invalid is not a repair."""
    _write(tmp_path)
    # A hunk that matches nothing in the file: the normaliser must leave it be
    # rather than invent a change.
    diff = (
        "--- a/math_utils.py\n"
        "+++ b/math_utils.py\n"
        "@@ -1,2 +1,2 @@\n"
        "-def something_else():\n"
        "+def something_else():\n"
        "     pass\n"
    )
    repaired, report = normalize_hunk_indentation(diff, tmp_path)
    assert repaired == diff
    assert not report.changed


def test_missing_file_is_skipped(tmp_path: Path) -> None:
    diff = (
        "--- a/nope.py\n"
        "+++ b/nope.py\n"
        "@@ -1,1 +1,1 @@\n"
        "-x = 1\n"
        "+x = 2\n"
    )
    repaired, report = normalize_hunk_indentation(diff, tmp_path)
    assert repaired == diff
    assert not report.changed
