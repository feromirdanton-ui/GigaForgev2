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

import ast
import difflib
import re
from dataclasses import dataclass
from pathlib import Path

__all__ = ["normalize_creation_hunks", "normalize_hunk_indentation", "RepairReport"]

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


#: ``@@ -a,b +c,d @@`` — the hunk header.
_HUNK_HEADER = re.compile(r"^@@")


def _indent_of(line: str) -> int:
    return len(line) - len(line.lstrip())


def _reindent_hunk(
    header: str, body: list[str], target: Path
) -> tuple[str, list[str], bool]:
    """Re-indent one hunk to the file's real indentation.

    The live bench found this: asked to fix ``validate_email``, the model emitted
    a hunk whose *context* lines carried ten spaces where the file has four, and
    whose added line carried the same ten.  ``git apply`` accepts that (it
    matches on content, not on indentation, and fuzzes the offset), so the diff
    "applies" and the file no longer parses — ``unexpected indent`` at the line
    the model just wrote.

    The repair is deterministic and needs no model call: locate the hunk's old
    side in the file by *stripped* content, read the real indentation of each
    matched line, and shift the added lines by the same delta.  The result is
    accepted only if the reconstructed file parses — a repair that does not
    make the file valid is not a repair, and the original hunk is returned.
    """
    try:
        original = target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return header, body, False
    file_lines = original.splitlines()

    old_idx = [k for k, line in enumerate(body) if line[:1] in (" ", "-")]
    old_text = [body[k][1:] for k in old_idx]
    if not old_text:
        return header, body, False

    stripped_old = [line.strip() for line in old_text]
    best_pos, best_score = -1, 0
    span = max(1, len(file_lines) - len(old_text) + 1)
    for start in range(span):
        score = 0
        for off, want in enumerate(stripped_old):
            if start + off < len(file_lines) and file_lines[start + off].strip() == want:
                score += 1
        if score > best_score:
            best_score, best_pos = score, start
    # A hunk that does not mostly match the file is a different change; leave it.
    if best_score < max(1, int(len(old_text) * 0.6)):
        return header, body, False

    deltas: dict[int, int] = {}
    for off, k in enumerate(old_idx):
        if best_pos + off < len(file_lines):
            deltas[k] = _indent_of(file_lines[best_pos + off]) - _indent_of(body[k][1:])

    def _shift(content: str, delta: int) -> str:
        if not content.strip() or not delta:
            return content
        if delta > 0:
            return " " * delta + content
        return content[min(-delta, _indent_of(content)):]

    # Every line of the hunk is rewritten, not only the added ones.  A context
    # line left at the diff's indentation would still be written into the file
    # by ``git apply``, so repairing only the ``+`` lines produced a file whose
    # docstring sat at 11 spaces and whose body sat at 4 — still a syntax error.
    new_body: list[str] = []
    for k, line in enumerate(body):
        marker, content = line[:1], line[1:]
        if marker not in (" ", "-", "+"):
            new_body.append(line)
            continue
        delta = 0
        for back in range(k, -1, -1):
            if back in deltas:
                delta = deltas[back]
                break
        new_body.append(marker + _shift(content, delta))

    def _parses(lines: list[str]) -> bool:
        new_side = [line[1:] for line in lines if line[:1] in (" ", "+")]
        candidate = (
            file_lines[:best_pos] + new_side + file_lines[best_pos + len(old_text):]
        )
        try:
            ast.parse("\n".join(candidate) + "\n")
        except SyntaxError:
            return False
        return True

    # If the hunk already produces a file that parses, it is not broken and
    # must be left byte-for-byte alone.  The first version of the shift search
    # below ran unconditionally and "repaired" a correct hunk into an invalid
    # one -- a repair pass that can damage a working diff is worse than none.
    if _parses(new_body):
        return header, new_body, new_body != body

    # **The case the delta cannot see.**  When the hunk's *context* lines match
    # the file exactly, every delta is 0 and the added lines are left as the
    # model wrote them -- so a body indented four spaces too deep survives the
    # repair untouched.  Measured on the live bench: ``toy-validate-email``
    # failed 1 of 3 repeats with ``math_utils.py:21: unexpected indent`` while
    # the context matched perfectly, which is why the earlier fix (correct for
    # the mismatched-context case) did not cover it.
    #
    # The added block's own indentation is the unknown, so try the plausible
    # shifts and keep the first that parses.  Deterministic, bounded, and gated
    # by the same parser the product uses: a shift that does not make the file
    # valid is not a repair.
    added_idx = [k for k, line in enumerate(new_body) if line[:1] == "+"]
    if added_idx:
        # A uniform shift is not enough when the block is internally
        # inconsistent -- ``if`` at 8 with its body at 4 is not fixed by moving
        # every line the same amount.  Re-derive the block's indentation from
        # Python's own rule instead: a line after one ending in ``:`` is one
        # level deeper, a line that dedents returns to an enclosing level.
        # Then anchor the block at the indentation its context implies.
        anchor = 0
        for back in range(added_idx[0] - 1, -1, -1):
            if new_body[back][:1] in (" ", "-"):
                anchor = _indent_of(new_body[back][1:])
                if new_body[back][1:].rstrip().endswith(":"):
                    anchor += 4
                break
        trial = list(new_body)
        levels = [anchor]
        prev_indent = anchor
        prev_opens = False
        for k in added_idx:
            content = trial[k][1:]
            if not content.strip():
                trial[k] = "+"
                continue
            if prev_opens:
                indent = prev_indent + 4
                levels.append(indent)
            else:
                current = _indent_of(content)
                while len(levels) > 1 and levels[-1] > current:
                    levels.pop()
                indent = levels[-1]
            trial[k] = "+" + " " * indent + content.lstrip()
            prev_indent = indent
            prev_opens = content.rstrip().endswith(":")
        if trial != new_body and _parses(trial):
            return header, trial, True

    return header, body, False


def normalize_hunk_indentation(diff: str, root: Path) -> tuple[str, RepairReport]:
    """Re-indent every hunk whose context does not match the file's indentation.

    A diff with no ``@@`` header, or one whose hunks already match, is returned
    byte for byte — the normaliser must not perturb the common case.
    """
    report = RepairReport(repaired=[], skipped=[])
    if not diff or "@@" not in diff:
        return diff, report
    root = Path(root)
    lines = diff.splitlines(keepends=True)
    out: list[str] = []
    current_path: str | None = None
    changed_any = False
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("+++ "):
            raw = line[4:].strip()
            if raw.startswith("b/"):
                raw = raw[2:]
            current_path = raw.split("\t")[0]
            out.append(line)
            i += 1
            continue
        if _HUNK_HEADER.match(line) and current_path:
            j = i + 1
            body: list[str] = []
            while j < len(lines) and not lines[j].startswith(
                ("@@", "diff --git", "--- ", "+++ ")
            ):
                body.append(lines[j])
                j += 1
            target = root / current_path
            if target.is_file():
                new_header, new_body, ok = _reindent_hunk(line, body, target)
                if ok:
                    out.append(new_header)
                    out.extend(new_body)
                    changed_any = True
                    if current_path not in report.repaired:
                        report.repaired.append(current_path)
                    i = j
                    continue
            out.append(line)
            out.extend(body)
            i = j
            continue
        out.append(line)
        i += 1
    if not changed_any:
        return diff, RepairReport(repaired=[], skipped=[])
    return "".join(out), report


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
