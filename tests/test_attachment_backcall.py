"""Re-attaching the same file: the harness registers the change and calls back.

The behaviour under test is the one the user asked for: when the same file
arrives twice, the system must notice, say what changed, and hand the model a
correction rather than a second copy of the whole file.
"""
from __future__ import annotations

from pathlib import Path

from gigaforge.harness.attachments import (
    CHANGED,
    NEW,
    UNCHANGED,
    AttachmentTracker,
)


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# ── the three verdicts ──────────────────────────────────────────────────────
def test_a_first_sighting_is_new_and_needs_its_body(tmp_path: Path) -> None:
    target = _write(tmp_path / "parser.py", "def parse(x):\n    return x\n")
    call = AttachmentTracker().observe(target)
    assert call.verdict == NEW
    assert call.needs_body is True
    assert call.is_repeat is False


def test_the_same_bytes_again_is_unchanged_and_needs_no_body(tmp_path: Path) -> None:
    """The token win: a 40 KB file re-sent unchanged costs one line."""
    target = _write(tmp_path / "parser.py", "def parse(x):\n    return x\n")
    tracker = AttachmentTracker()
    tracker.observe(target)
    call = tracker.observe(target)
    assert call.verdict == UNCHANGED
    assert call.needs_body is False, "an unchanged file must not be re-sent"
    assert "byte-identical" in call.note


def test_a_changed_file_produces_a_diff_and_a_correction(tmp_path: Path) -> None:
    """The core ask: register the change and give a back call with corrections."""
    target = _write(tmp_path / "parser.py", "def parse(x):\n    return x\n")
    tracker = AttachmentTracker()
    tracker.observe(target)

    _write(tmp_path / "parser.py", "def parse(x):\n    return x + 1\n")
    call = tracker.observe(target)

    assert call.verdict == CHANGED
    assert call.needs_body is True, "the model must work against the new revision"
    assert call.added == 1 and call.removed == 1
    assert "-    return x" in call.diff
    assert "+    return x + 1" in call.diff
    assert "must be corrected" in call.note


def test_the_diff_is_against_the_previous_revision_not_the_first(tmp_path: Path) -> None:
    """Three sightings: the second diff must compare v2→v3, not v1→v3."""
    target = _write(tmp_path / "a.py", "one\n")
    tracker = AttachmentTracker()
    tracker.observe(target)
    _write(tmp_path / "a.py", "one\ntwo\n")
    tracker.observe(target)
    _write(tmp_path / "a.py", "one\ntwo\nthree\n")
    call = tracker.observe(target)
    assert call.verdict == CHANGED
    # v2→v3 adds exactly one line.  A v1→v3 diff would add two.
    assert call.added == 1 and call.removed == 0
    assert "+three" in call.diff
    assert "+two" not in call.diff


# ── identity is the name, not the path ──────────────────────────────────────
def test_the_same_name_from_a_different_directory_is_a_repeat(tmp_path: Path) -> None:
    """A re-attachment routinely arrives from a download folder."""
    first = _write(tmp_path / "src" / "parser.py", "v1\n")
    second = _write(tmp_path / "downloads" / "parser.py", "v2\n")
    tracker = AttachmentTracker()
    assert tracker.observe(first).verdict == NEW
    call = tracker.observe(second)
    assert call.verdict == CHANGED, "the user means 'the same file', not 'the same path'"
    assert call.previous is not None and call.previous.path == str(first)


def test_two_different_files_are_both_new(tmp_path: Path) -> None:
    tracker = AttachmentTracker()
    assert tracker.observe(_write(tmp_path / "a.py", "a\n")).verdict == NEW
    assert tracker.observe(_write(tmp_path / "b.py", "b\n")).verdict == NEW


# ── the prompt block ────────────────────────────────────────────────────────
def test_the_prompt_block_is_empty_when_nothing_repeats(tmp_path: Path) -> None:
    """A first sighting needs no announcement — that is noise the model pays for."""
    tracker = AttachmentTracker()
    calls = [tracker.observe(_write(tmp_path / "a.py", "a\n"))]
    assert tracker.prompt_block(calls) == ""


def test_the_prompt_block_carries_the_diff_for_a_change(tmp_path: Path) -> None:
    target = _write(tmp_path / "a.py", "v1\n")
    tracker = AttachmentTracker()
    tracker.observe(target)
    _write(tmp_path / "a.py", "v2\n")
    call = tracker.observe(target)
    block = tracker.prompt_block([call])
    assert "```diff" in block
    assert "+v2" in block


# ── robustness ──────────────────────────────────────────────────────────────
def test_an_unreadable_file_is_new_not_an_exception(tmp_path: Path) -> None:
    """An unreadable attachment must not abort a run."""
    missing = tmp_path / "gone.py"
    call = AttachmentTracker().observe(missing)
    assert call.verdict == NEW
    assert call.current is not None and call.current.size == 0


def test_a_huge_diff_is_truncated_not_inlined(tmp_path: Path) -> None:
    target = _write(tmp_path / "big.py", "\n".join(f"line {i}" for i in range(2000)))
    tracker = AttachmentTracker(max_diff_lines=50)
    tracker.observe(target)
    _write(tmp_path / "big.py", "\n".join(f"changed {i}" for i in range(2000)))
    call = tracker.observe(target)
    assert call.verdict == CHANGED
    assert "[diff truncated]" in call.diff
    assert len(call.diff.splitlines()) <= 51


def test_describe_reports_the_run_history(tmp_path: Path) -> None:
    target = _write(tmp_path / "a.py", "v1\n")
    tracker = AttachmentTracker()
    tracker.observe(target)
    tracker.observe(target)
    _write(tmp_path / "a.py", "v2\n")
    tracker.observe(target)
    described = tracker.describe()
    assert described["tracked"] == 1
    assert described["repeats"] == 2
    assert described["unchanged"] == 1
    assert described["changed"] == 1
