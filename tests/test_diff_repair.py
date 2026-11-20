"""The diff repair, tested on the exact shape the live probe produced.

The live run asked the model to *fix* ``math_utils.py`` and got back a creation
hunk for a file that already exists.  ``git apply`` rejects it, so the run
reported a diff that looked right and did not apply — the worst of both worlds.

These tests use the real text from that run, and they apply the result with
``git apply`` rather than asserting on the string: a repair that produces a
plausible-looking patch the repository still rejects has fixed nothing.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from gigaforge.harness.diff_repair import normalize_creation_hunks

#: Verbatim from the live probe run against GigaChat.
LIVE_DIFF = (
    "--- /dev/null\n"
    "+++ b/math_utils.py\n"
    "@@ -0,0 +1,4 @@\n"
    "+def add(a, b):\n"
    '+    """\n'
    "+    This function adds two numbers correctly.\n"
    '+    """\n'
    "+    return a + b\n"
)


def _repo(tmp: Path) -> Path:
    (tmp / "math_utils.py").write_text("def add(a: int, b: int) -> int:\n    return a - b\n")
    subprocess.run(["git", "init", "-q"], cwd=tmp, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
        cwd=tmp,
        check=True,
    )
    return tmp


def _applies(root: Path, diff: str) -> bool:
    patch = root / "p.patch"
    patch.write_text(diff if diff.endswith("\n") else diff + "\n")
    proc = subprocess.run(
        ["git", "apply", "--check", str(patch)], cwd=root, capture_output=True, text=True
    )
    return proc.returncode == 0


def test_the_live_creation_hunk_is_repaired_into_a_modification(tmp_path: Path) -> None:
    """The exact diff from the live run must become one that applies."""
    root = _repo(tmp_path)
    assert not _applies(root, LIVE_DIFF), "the raw diff must be the broken one"

    repaired, report = normalize_creation_hunks(LIVE_DIFF, root)

    assert report.repaired == ["math_utils.py"]
    assert "/dev/null" not in repaired
    assert "--- a/math_utils.py" in repaired
    assert _applies(root, repaired), repaired


def test_the_repaired_diff_actually_fixes_the_bug(tmp_path: Path) -> None:
    """Applying the repair must make the repository's own test pass."""
    root = _repo(tmp_path)
    (root / "test_math_utils.py").write_text(
        "from math_utils import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"
    )
    repaired, _ = normalize_creation_hunks(LIVE_DIFF, root)
    patch = root / "p.patch"
    patch.write_text(repaired)
    subprocess.run(["git", "apply", str(patch)], cwd=root, check=True)

    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "test_math_utils.py"],
        cwd=root,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_a_genuine_creation_is_left_alone(tmp_path: Path) -> None:
    """A creation hunk for a file that does not exist must not be touched."""
    root = _repo(tmp_path)
    diff = "--- /dev/null\n+++ b/new_file.py\n@@ -0,0 +1,1 @@\n+x = 1\n"
    repaired, report = normalize_creation_hunks(diff, root)
    assert repaired == diff
    assert report.repaired == []
    assert _applies(root, repaired)


def test_a_diff_without_creation_hunks_is_byte_identical(tmp_path: Path) -> None:
    """The normaliser must not perturb the common case."""
    root = _repo(tmp_path)
    diff = (
        "--- a/math_utils.py\n+++ b/math_utils.py\n@@ -1,2 +1,2 @@\n"
        " def add(a: int, b: int) -> int:\n-    return a - b\n+    return a + b\n"
    )
    repaired, report = normalize_creation_hunks(diff, root)
    assert repaired == diff
    assert report.changed is False


def test_a_no_op_repair_is_reported_not_emitted(tmp_path: Path) -> None:
    """A "fix" identical to the file on disk must not become an empty patch."""
    root = _repo(tmp_path)
    diff = (
        "--- /dev/null\n+++ b/math_utils.py\n@@ -0,0 +1,2 @@\n"
        "+def add(a: int, b: int) -> int:\n+    return a - b\n"
    )
    repaired, report = normalize_creation_hunks(diff, root)
    assert report.repaired == []
    assert report.skipped == ["math_utils.py"]
    assert repaired == diff


@pytest.mark.parametrize("empty", ["", "   ", "no diff here"])
def test_non_diffs_pass_through(tmp_path: Path, empty: str) -> None:
    root = _repo(tmp_path)
    repaired, report = normalize_creation_hunks(empty, root)
    assert repaired == empty
    assert report.changed is False
