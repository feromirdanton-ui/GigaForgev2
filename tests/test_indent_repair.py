"""A gate that can repair deterministically must repair, not reject.

``toy-validate-email`` failed 2 of 3 live repeats with
``math_utils.py:15: unexpected indent``.  The model wrote a body four spaces too
deep inside a function whose context matched perfectly, so the diff repair saw
every delta as zero and passed the hunk through untouched.  The contract then
rejected the file with the repair available and unused.

The repair is arithmetic: Python's rule says a line after one ending in ``:``
sits one level deeper.  These tests pin both the repair and its refusal.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gigaforge.harness.code_contract import repair_indentation  # noqa: E402


# ── the repair ─────────────────────────────────────────────────────────────
def test_a_line_deeper_than_its_previous_statement_is_dedented() -> None:
    """The only shape Python calls ``unexpected indent``.

    A block indented deeper than its parent is *legal* Python as long as it is
    consistent -- ``def f():`` followed by a body at eight spaces parses.  The
    error occurs when a line is deeper than the previous line and that line does
    not open a block, which is what the live failure was.
    """
    broken = (
        "def validate_email(value):\n"
        "    ok = '@' in value\n"
        "        return ok\n"
    )
    fixed, why = repair_indentation(broken)
    assert why
    ast.parse(fixed)
    assert fixed == (
        "def validate_email(value):\n"
        "    ok = '@' in value\n"
        "    return ok\n"
    )


def test_the_whole_over_indented_block_moves_together() -> None:
    """The offender's block, not only the offending line."""
    broken = (
        "def f():\n"
        "    x = 1\n"
        "        if x:\n"
        "            return 1\n"
    )
    fixed, why = repair_indentation(broken)
    assert why
    ast.parse(fixed)
    assert fixed == (
        "def f():\n"
        "    x = 1\n"
        "    if x:\n"
        "        return 1\n"
    )


def test_a_line_after_the_block_is_left_alone() -> None:
    """A line that dedents below the offender belongs to an enclosing block."""
    broken = (
        "def f():\n"
        "    x = 1\n"
        "        return x\n"
        "    return 2\n"
    )
    fixed, why = repair_indentation(broken)
    assert why
    ast.parse(fixed)
    assert "    return 2" in fixed


# ── the refusal ────────────────────────────────────────────────────────────
def test_a_valid_file_is_returned_byte_for_byte() -> None:
    """A repair pass that can damage a working file is worse than none."""
    good = "def f():\n    return 1\n"
    fixed, why = repair_indentation(good)
    assert fixed == good
    assert why == ""


def test_a_consistently_deep_block_is_legal_and_untouched() -> None:
    """Python accepts a body at eight spaces; repairing it would rewrite code
    that already works."""
    legal = "def f():\n        return 1\n"
    fixed, why = repair_indentation(legal)
    assert fixed == legal
    assert why == ""


def test_a_different_syntax_error_is_not_guessed_at() -> None:
    """Changing code the repair does not understand is not a repair."""
    broken = "def f(:\n    return 1\n"
    fixed, why = repair_indentation(broken)
    assert fixed == broken
    assert why == ""


def test_a_repair_that_still_does_not_parse_is_rejected() -> None:
    """A repair that does not make the file valid is not a repair."""
    broken = "def f():\n    x = 1\n        return (1\n"
    fixed, why = repair_indentation(broken)
    assert why == ""
    assert fixed == broken


def test_a_missing_indent_is_not_repaired_by_this_pass() -> None:
    """``expected an indented block`` is the opposite defect, and a shift up
    would be a guess about what the block should contain."""
    broken = "def f():\nreturn 1\n"
    fixed, why = repair_indentation(broken)
    assert why == ""
    assert fixed == broken
