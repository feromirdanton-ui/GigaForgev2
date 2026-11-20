"""Repair a diff that *creates* a file which already exists.

The live probe found this on the first real run, and it is the single most
common way a correct-looking answer fails to apply:

    --- /dev/null
    +++ b/math_utils.py
    @@ -0,0 +1,4 @@
    +def add(a, b):
    +    return a + b

The model was asked to *fix* ``math_utils.py``.  It read the file, understood
the bug, and then emitted the corrected function as a **new file** — because
"here is the file as it should be" is a natural way to express a fix, and
nothing in the prompt forbade it.  ``git apply`` rejects it: the file exists, so
a creation hunk cannot apply.  The run then reports a diff that looks right and
does not apply, which is worse than reporting nothing.

The repair is deterministic and needs no model call: when a creation hunk names
a file that exists, turn it into a modification of that file.  Two shapes are
handled, because the model produces both:

* **whole-file replacement** — the hunk's added lines are the complete new
  content.  Diffed against the file on disk, this is exact.
* **partial addition** — the hunk adds lines that are not the whole file.  The
  added lines are appended at the hunk's stated position, which is what the
  model meant by ``@@ -0,0 +1,N @@``.

Anything that cannot be repaired is left untouched: a wrong diff is reported as
a wrong diff, never silently rewritten into a different change.
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from pathlib import Path

__all__ = ["normalize_creation_hunks", "RepairReport"]

#: ``--- /dev/null`` followed by ``+++ b/<path>`` and one or more hunks.
_CREATION_BLOCK = re.compile(
    r"^---\s+/dev/null\s*\n\+\+\+\s+(?:b/)?(?P<path>[^\s\t]+).*?\n(?P<body>(?:@@.*\n(?:[ +\-\\].*\n?)*)+)",
    re.MULTILINE,
)


@dataclass
class RepairReport:
    """What the normaliser changed, for the ledger and the report."""

    repaired: list[str]
    skipped: list[str]

    @property
    def changed(self) -> bool:
        return bool(self.repaired)

    def to_dict(self) -> dict[str, list[str]]:
        return {"repaired": list(self.repaired), "skipped": list(self.skipped)}


def _added_lines(body: str) -> list[str]:
    """The ``+`` lines of a hunk body, in order, without the marker."""
    out: list[str] = []
    for line in body.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            out.append(line[1:])
    return out


def _hunk_start(body: str) -> int:
    """The 1-based line the first hunk claims to start at (0 when absent)."""
    match = re.search(r"^@@\s+-\d+(?:,\d+)?\s+\+(\d+)", body, re.MULTILINE)
    return int(match.group(1)) if match else 0


def _modify_diff(rel: str, old: list[str], new: list[str]) -> str:
    """A unified diff turning *old* into *new*, in the shape ``git apply`` wants."""
    diff = difflib.unified_diff(
        old,
        new,
        fromfile=f"a/{rel}",
        tofile=f"b/{rel}",
        lineterm="",
        n=3,
    )
    return "\n".join(diff) + "\n"


def normalize_creation_hunks(diff: str, root: Path) -> tuple[str, RepairReport]:
    """Rewrite creation hunks that name an existing file into modifications.

    Returns the (possibly) rewritten diff and a report of what was touched.
    A diff with no creation hunks is returned unchanged, byte for byte — the
    normaliser must not perturb the common case.
    """
    report = RepairReport(repaired=[], skipped=[])
    if not diff or "/dev/null" not in diff:
        return diff, report

    root = Path(root)

    def _replace(match: re.Match[str]) -> str:
        rel = match.group("path").strip()
        body = match.group("body")
        target = root / rel
        if not target.is_file():
            # A genuine creation: leave it exactly as it is.
            return match.group(0)
        try:
            original = target.read_text(encoding="utf-8", errors="replace")
        except OSError:
            report.skipped.append(rel)
            return match.group(0)

        added = _added_lines(body)
        if not added:
            report.skipped.append(rel)
            return match.group(0)

        old_lines = original.splitlines()
        # Whole-file replacement: the added lines are the complete new content.
        # This is the shape the live probe produced, and diffing it against the
        # file on disk is exact — no guessing about where the change belongs.
        new_lines = added
        if _hunk_start(body) > 1:
            # A partial addition: splice the added lines in at the stated
            # position rather than replacing the file.
            at = max(0, min(len(old_lines), _hunk_start(body) - 1))
            new_lines = old_lines[:at] + added + old_lines[at:]

        if new_lines == old_lines:
            # The "fix" is identical to the file: nothing to apply, and saying
            # so is more useful than emitting an empty patch.
            report.skipped.append(rel)
            return match.group(0)

        report.repaired.append(rel)
        return _modify_diff(rel, old_lines, new_lines)

    rewritten = _CREATION_BLOCK.sub(_replace, diff)
    return rewritten, report
