"""Unified-diff parsing and three-strategy application.

The validator's job is to take whatever a model produced and either apply it
faithfully or say precisely why it could not.  Models are unreliable about
diff formatting, so the parser is deliberately forgiving — and the applier is
deliberately strict.

**Parsing** (:func:`parse` / :func:`parse_file` / :func:`parse_hunk`)

* strips ```-fences (```diff … ```), stray prose before the first ``diff``
  header, and trailing explanations after the last hunk;
* accepts a bare hunk with no file header (``@@`` first) and infers the path;
* tolerates malformed hunk counters (``@@ -1 +1 @@``, ``@@ -3,0 +4,2 @@``,
  missing counts, wrong counts) — the declared numbers are recorded but the
  *actual* line counts are recomputed from the body;
* understands ``new file`` / ``deleted file`` / ``rename from|to`` / mode lines;
* honours the ``\\ No newline at end of file`` marker and never invents a
  trailing newline.

**Matching** (:func:`locate`) — three strategies, tried in order, each strictly
weaker than the last:

1. ``exact`` — the hunk's old-side lines appear verbatim at the declared
   ``old_start`` (allowing the usual off-by-N drift);
2. ``fuzzy`` — a ``difflib.SequenceMatcher`` over a sliding window around the
   hint scores above ``fuzzy_threshold``;
3. ``char`` — a character-level comparison tolerating whitespace and
   trailing-space differences, used when a model re-indented the context.

Every attempt records which strategy won, so the validator can report *how* a
diff was applied rather than only *that* it was.

**Application** (:class:`Applier`) — path safety first.  A diff may never touch
a path outside ``apply_allow_paths``, inside ``apply_deny_globs``, or escaping
the repo root via ``..``.  Writes are atomic across the whole diff: a failure on
file N restores files 1..N-1, and a backup of every touched file is kept so the
restore is exact.
"""

from __future__ import annotations

import difflib
import fnmatch
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Sequence

from gigaforge.workspace_paths import is_sensitive_workspace_path

from .config import FirewallConfig
from .errors import (
    ApplyRollbackError,
    ContextMismatchError,
    DiffParseError,
    PathNotAllowedError,
)

log = logging.getLogger(__name__)

# ── regexes ──────────────────────────────────────────────────────────────
_DIFF_GIT_RE = re.compile(r"^diff --git\s+(?P<a>\S+)\s+(?P<b>\S+)\s*$")
_HUNK_RE = re.compile(
    r"^@@\s+-(?P<old_start>\d+)(?:,(?P<old_count>\d+))?\s+"
    r"\+(?P<new_start>\d+)(?:,(?P<new_count>\d+))?\s*@@(?P<tail>.*)$"
)
_MINUS_FILE_RE = re.compile(r"^---\s+(?P<path>.+?)(?:\t.*)?$")
_PLUS_FILE_RE = re.compile(r"^\+\+\+\s+(?P<path>.+?)(?:\t.*)?$")
_RENAME_FROM_RE = re.compile(r"^rename from\s+(?P<src>.+)$")
_RENAME_TO_RE = re.compile(r"^rename to\s+(?P<dst>.+)$")
_NEW_FILE_RE = re.compile(r"^new file mode\s+(?P<mode>\d+)$")
_DELETED_FILE_RE = re.compile(r"^deleted file mode\s+(?P<mode>\d+)$")
_INDEX_RE = re.compile(r"^index\s+[0-9a-fA-F]+\.\.[0-9a-fA-F]+(?:\s+\d+)?$")
_MODE_RE = re.compile(r"^(?P<which>old|new) mode\s+(?P<mode>\d+)$")
_NO_NEWLINE = "\\ No newline at end of file"
_FENCE_RE = re.compile(r"^\s*(?:```+|~~~+)\s*(?P<lang>[\w+-]*)\s*$")

#: Line prefixes that carry meaning inside a hunk body.
_BODY_PREFIXES = (" ", "+", "-")
#: Sentinel used when a hunk terminator is absent.
NO_NEWLINE_FLAG = r"\ No newline at end of file"


# ── data model ───────────────────────────────────────────────────────────
@dataclass
class HunkLine:
    """One body line with its diff prefix preserved (``' '``, ``'+'``, ``'-'``)."""

    prefix: str
    text: str
    no_newline: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"prefix": self.prefix, "text": self.text, "no_newline": self.no_newline}


@dataclass
class Hunk:
    """A parsed ``@@`` hunk."""

    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: list[HunkLine] = field(default_factory=list)
    section: str = ""
    header: str = ""
    #: Recomputed from the body — the declared counters are frequently wrong.
    actual_old: int = 0
    actual_new: int = 0

    @property
    def counters_ok(self) -> bool:
        return self.old_count == self.actual_old and self.new_count == self.actual_new

    @property
    def old_lines(self) -> list[str]:
        """The old-side text of this hunk (context + deletions)."""
        return [ln.text for ln in self.lines if ln.prefix in (" ", "-")]

    @property
    def new_lines(self) -> list[str]:
        """The new-side text of this hunk (context + additions)."""
        return [ln.text for ln in self.lines if ln.prefix in (" ", "+")]

    def to_dict(self) -> dict[str, Any]:
        return {
            "old_start": self.old_start,
            "old_count": self.old_count,
            "new_start": self.new_start,
            "new_count": self.new_count,
            "actual_old": self.actual_old,
            "actual_new": self.actual_new,
            "section": self.section,
            "additions": sum(1 for ln in self.lines if ln.prefix == "+"),
            "deletions": sum(1 for ln in self.lines if ln.prefix == "-"),
            "lines": [ln.to_dict() for ln in self.lines],
        }


@dataclass
class DiffFile:
    """One file's worth of changes."""

    path: str
    old_path: str = ""
    status: str = "modified"  # modified | added | deleted | renamed
    hunks: list[Hunk] = field(default_factory=list)
    binary: bool = False
    mode_old: str = ""
    mode_new: str = ""
    is_new: bool = False
    #: Whole-file content for a rewrite that arrived as a 'new file' diff.
    replacement: str = ""
    is_delete: bool = False
    header_seen: bool = False

    @property
    def additions(self) -> int:
        return sum(1 for h in self.hunks for ln in h.lines if ln.prefix == "+")

    @property
    def deletions(self) -> int:
        return sum(1 for h in self.hunks for ln in h.lines if ln.prefix == "-")

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "old_path": self.old_path or self.path,
            "status": self.status,
            "binary": self.binary,
            "additions": self.additions,
            "deletions": self.deletions,
            "hunks": [h.to_dict() for h in self.hunks],
        }


@dataclass
class ParsedDiff:
    """The full parse result plus the diagnostics the validator needs."""

    files: list[DiffFile] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    stripped_fences: bool = False
    dropped_preamble_lines: int = 0

    @property
    def ok(self) -> bool:
        return bool(self.files) and any(f.hunks or f.binary or f.is_delete for f in self.files)

    @property
    def paths(self) -> list[str]:
        return [f.path for f in self.files]

    def hunk_count(self) -> int:
        return sum(len(f.hunks) for f in self.files)

    def to_dict(self) -> dict[str, Any]:
        return {
            "files": [f.to_dict() for f in self.files],
            "warnings": list(self.warnings),
            "stripped_fences": self.stripped_fences,
            "dropped_preamble_lines": self.dropped_preamble_lines,
            "files_count": len(self.files),
            "hunk_count": self.hunk_count(),
        }


# ── fence stripping ──────────────────────────────────────────────────────
def strip_code_fences(text: str) -> tuple[str, bool]:
    """Remove ``` / ~~~ fences, keeping only the fenced body.

    Returns ``(clean_text, stripped)``.  A diff is usually the single fenced
    block in a model's reply, so when at least one fence is found we prefer the
    *largest* fenced body that looks like a diff; otherwise we fall back to the
    whole text with any fence lines removed.
    """
    if "```" not in text and "~~~" not in text:
        return text, False

    lines = text.splitlines()
    blocks: list[list[str]] = []
    current: Optional[list[str]] = None
    fence_count = 0
    outside: list[str] = []

    for line in lines:
        match = _FENCE_RE.match(line)
        if match:
            fence_count += 1
            if current is None:
                current = []
            else:
                blocks.append(current)
                current = None
            continue
        if current is not None:
            current.append(line)
        else:
            outside.append(line)
    if current is not None:  # unterminated fence: treat the tail as a block
        blocks.append(current)

    if not blocks:
        # only stray fence markers; drop them and scan the remainder
        return "\n".join(outside), True

    def looks_like_diff(block: Sequence[str]) -> int:
        text_block = "\n".join(block)
        score = 0
        if _DIFF_GIT_RE.search(text_block):
            score += 3
        if "@@" in text_block:
            score += 2
        if text_block.startswith(("---", "+++", "@@", "diff ")):
            score += 2
        score += min(2, sum(1 for ln in block if ln[:1] in _BODY_PREFIXES) // 5)
        return score

    best = max(blocks, key=lambda b: (looks_like_diff(b), len(b)))
    if looks_like_diff(best) == 0 and outside:
        # no fenced block is diff-like; fall back to everything minus fences
        merged = "\n".join([*outside])
        return merged, True
    return "\n".join(best), True


def _trim_to_diff(text: str) -> tuple[str, int]:
    """Drop prose before the first diff-ish line; return ``(text, dropped)``."""
    lines = text.splitlines()
    start = None
    for idx, line in enumerate(lines):
        if (
            _DIFF_GIT_RE.match(line)
            or line.startswith("--- ")
            or _HUNK_RE.match(line)
            or _INDEX_RE.match(line)
        ):
            start = idx
            break
    if start is None:
        return text, 0
    return "\n".join(lines[start:]), start


# ── parsing ──────────────────────────────────────────────────────────────
def parse(text: str, config: Optional[FirewallConfig] = None, *, path_hint: str = "") -> ParsedDiff:
    """Parse a unified diff, tolerating fences, prose and bad counters."""
    cfg = config or FirewallConfig()
    result = ParsedDiff()
    if not text or not text.strip():
        raise DiffParseError("empty diff", detail={"reason": "empty"})

    body = text
    if cfg.strip_code_fences:
        body, stripped = strip_code_fences(body)
        result.stripped_fences = stripped
    body, dropped = _trim_to_diff(body)
    result.dropped_preamble_lines = dropped

    lines = body.splitlines()
    current: Optional[DiffFile] = None
    current_hunk: Optional[Hunk] = None
    pending_old = ""
    pending_new = ""

    def close_hunk() -> None:
        nonlocal current_hunk
        if current is not None and current_hunk is not None:
            _finalise_hunk(current_hunk)
            current.hunks.append(current_hunk)
        current_hunk = None

    def close_file() -> None:
        nonlocal current
        close_hunk()
        if current is not None:
            result.files.append(current)
        current = None

    for raw in lines:
        line = raw.rstrip("\n")

        header = _DIFF_GIT_RE.match(line)
        if header:
            close_file()
            a = _clean_git_path(header.group("a"))
            b = _clean_git_path(header.group("b"))
            current = DiffFile(path=b or a, old_path=a, header_seen=True)
            continue

        minus = _MINUS_FILE_RE.match(line)
        if minus and not line.startswith("---@"):
            # Remember the old path even when no ``diff --git`` header opened the
            # section.  Models usually emit a bare ``--- a`` / ``+++ b`` pair; the
            # old path is the only thing that tells an edit apart from a creation.
            pending_old = _clean_git_path(minus.group("path"))
            if current is not None and not current.header_seen and not current.path:
                current.path = pending_old
            continue

        plus = _PLUS_FILE_RE.match(line)
        if plus:
            pending_new = _clean_git_path(plus.group("path"))
            if current is None:
                # a bare ``--- / +++`` pair with no ``diff --git`` header
                current = DiffFile(path=pending_new, old_path=pending_old)
            elif not current.path or current.path == current.old_path:
                current.path = pending_new
                current.old_path = pending_old or current.old_path
            continue

        rename_from = _RENAME_FROM_RE.match(line)
        if rename_from and current is not None:
            current.old_path = rename_from.group("src").strip()
            current.status = "renamed"
            continue
        rename_to = _RENAME_TO_RE.match(line)
        if rename_to and current is not None:
            current.path = rename_to.group("dst").strip()
            current.status = "renamed"
            continue

        if _NEW_FILE_RE.match(line) and current is not None:
            current.is_new = True
            current.status = "added"
            continue
        if _DELETED_FILE_RE.match(line) and current is not None:
            current.is_delete = True
            current.status = "deleted"
            continue
        mode = _MODE_RE.match(line)
        if mode and current is not None:
            if mode.group("which") == "old":
                current.mode_old = mode.group("mode")
            else:
                current.mode_new = mode.group("mode")
            continue
        if _INDEX_RE.match(line) or line.startswith("similarity index"):
            continue
        if line.startswith("Binary files ") or line.startswith("GIT binary patch"):
            if current is not None:
                current.binary = True
                result.warnings.append(f"binary file skipped: {current.path}")
            continue

        hunk_match = _HUNK_RE.match(line)
        if hunk_match:
            close_hunk()
            if current is None:
                # a bare hunk: synthesise a file from the hint
                if not path_hint:
                    raise DiffParseError(
                        "hunk without a file header and no path hint",
                        detail={"line": line},
                    )
                current = DiffFile(path=path_hint, old_path=path_hint)
            current_hunk = Hunk(
                old_start=int(hunk_match.group("old_start")),
                old_count=int(hunk_match.group("old_count") or 1),
                new_start=int(hunk_match.group("new_start")),
                new_count=int(hunk_match.group("new_count") or 1),
                section=hunk_match.group("tail").strip(),
                header=line,
            )
            continue

        if current_hunk is not None:
            if line == _NO_NEWLINE or line.rstrip() == _NO_NEWLINE:
                if current_hunk.lines:
                    current_hunk.lines[-1].no_newline = True
                continue
            if not line:
                # an empty line inside a hunk body means a context blank line
                current_hunk.lines.append(HunkLine(prefix=" ", text=""))
                continue
            prefix = line[0]
            if prefix in _BODY_PREFIXES:
                current_hunk.lines.append(HunkLine(prefix=prefix, text=line[1:]))
                continue
            if prefix == "\\":
                continue
            # Anything else terminates the hunk (prose, next header, ...).
            close_hunk()
            continue

        # outside any hunk — ignore, but remember it as a warning when it is not
        # obviously structural
        if current is not None and line.strip() and not line.startswith(("---", "+++")):
            if len(result.warnings) < 20:
                result.warnings.append(f"ignored line: {line[:120]}")

    close_file()
    _normalise_file_targets(result, config)
    if not result.files:
        raise DiffParseError("no file sections found in diff", detail={"reason": "no_files"})
    return result


def _normalise_file_targets(parsed: ParsedDiff, config: Optional[FirewallConfig] = None) -> None:
    """Repair the two header mistakes models make most often.

    Models routinely write ``--- index1.html`` / ``+++ index1_new.html`` when they
    mean "edit index1.html", and they append a creation hunk (``@@ -0,0 +1,N @@``)
    under an existing file's header when they mean "create index2.html".  Both were
    observed on a live HTML task and both made an otherwise correct diff
    unapplicable — the run ended "not applied" with no file touched.

    A third, costlier mistake: the model prefixes every path with the repository's
    own folder name (``_site_demo/index.html``) after seeing that name in a prompt.
    The applier then creates *a nested copy of the repository* and the user sees
    "task done" with no files where they asked.  Absolute paths that point inside
    the root are rewritten relative for the same reason.

    Rules, in order:

    * every hunk is a pure insertion and the ``+++`` path differs -> the ``+++``
      path is the *new file* (creation);
    * otherwise, if the ``---`` path is not ``/dev/null`` and differs from ``+++``,
      the ``---`` path wins: the diff edits the file it started from, and the
      ``+++`` name was invented;
    * ``--- /dev/null`` always means "create the ``+++`` path".
    """
    root: Optional[Path] = None
    raw_root = str(getattr(config, "repo_root", "") or "") if config is not None else ""
    if raw_root:
        root = Path(raw_root)

    for diff_file in parsed.files:
        _normalise_path(diff_file, config, parsed)
        old_path = (diff_file.old_path or "").strip()
        new_path = (diff_file.path or "").strip()
        if not new_path:
            continue
        # Any diff that *declares* a new file while the file exists is a rewrite in
        # disguise — whether it used `/dev/null` or `new file mode`, and whatever the
        # parser made of the old side.  Accept it as a whole-file replacement when no
        # top-level definition of the existing file disappears.
        declares_new = bool(diff_file.is_new) or diff_file.status == "added"
        if declares_new and root is not None and (root / new_path).is_file():
            body = "\n".join(
                line.text for hunk in diff_file.hunks for line in hunk.lines
                if line.prefix in {"+", " "}
            )
            old_text = (root / new_path).read_text(encoding="utf-8", errors="replace")
            lost = [name for name in _definitions(old_text) if not _mentions(body, name)]
            if body.strip() and not lost:
                diff_file.status = "modified"
                diff_file.is_new = False
                diff_file.path = new_path
                diff_file.replacement = body
                parsed.warnings.append(
                    f"{new_path}: whole-file replacement (the diff declared a new "
                    "file, but it already exists)")
                continue
            parsed.warnings.append(
                f"{new_path}: 'new file' header for an existing file (would drop "
                f"{', '.join(lost[:3]) or 'everything'}) — send an edit diff instead")
            continue
        if old_path in {"/dev/null", "dev/null"}:
            # A model that "fills a stub" often sends `new file mode` / `/dev/null`
            # for a file that already exists.  That is a replacement, and refusing it
            # rejected every attempt of a live "implement the stubs" goal.  It is
            # accepted as a whole-file rewrite, but only when no top-level definition
            # of the existing file disappears — a rewrite must never delete code.
            if root is not None and (root / new_path).is_file():
                body = "\n".join(
                    line.text for hunk in diff_file.hunks for line in hunk.lines
                    if line.prefix in {"+", " "}
                )
                old_text = (root / new_path).read_text(encoding="utf-8", errors="replace")
                lost = [name for name in _definitions(old_text) if not _mentions(body, name)]
                if body.strip() and not lost:
                    diff_file.status = "modified"
                    diff_file.is_new = False
                    diff_file.replacement = body
                    parsed.warnings.append(
                        f"{new_path}: whole-file replacement (header said 'new file', "
                        "but the file exists)")
                else:
                    parsed.warnings.append(
                        f"{new_path}: 'new file' header for an existing file "
                        f"(would drop {', '.join(lost[:3]) or 'everything'}) — send an "
                        "edit diff instead")
            continue
        if _same_path(old_path, new_path):
            continue
        pure_insertion = bool(diff_file.hunks) and all(
            h.old_count == 0 and not h.old_lines for h in diff_file.hunks
        )
        if pure_insertion:
            diff_file.status = "added"
            diff_file.is_new = True
            # A model that "fills a stub" often sends `new file mode` / `/dev/null`
            # for a file that already exists.  That is a replacement, and refusing
            # it rejected every attempt of a live "implement the stubs" goal.
            # It is accepted as a whole-file rewrite, but only when no definition of
            # the existing file disappears — a rewrite must never delete code.
            if root is not None:
                target = root / new_path
                try:
                    exists = target.is_file()
                except OSError:
                    exists = False
                if exists:
                    body = "\n".join(
                        line.text for hunk in diff_file.hunks for line in hunk.lines
                        if line.prefix in {"+", " "}
                    )
                    old_text = target.read_text(encoding="utf-8", errors="replace")
                    lost = [name for name in _definitions(old_text)
                            if not _mentions(body, name)]
                    if body.strip() and not lost:
                        diff_file.status = "modified"
                        diff_file.is_new = False
                        diff_file.replacement = body
                        parsed.warnings.append(
                            f"{new_path}: whole-file replacement (the header said "
                            f"'new file' but the file exists)")
                        continue
                    parsed.warnings.append(
                        f"{new_path}: 'new file' header for an existing file "
                        f"(would drop {', '.join(lost[:3]) or 'everything'}) — "
                        "send an edit diff instead")
            continue
        # A pure-insertion hunk (`@@ -0,0 ...`) inside a file that also has real
        # edits belongs to *another* file the model forgot to open a header for.
        # Keeping it would append the other file's content to this one.
        stray = [h for h in diff_file.hunks if h.old_start == 0 and not h.old_lines]
        if stray and len(stray) < len(diff_file.hunks):
            diff_file.hunks = [h for h in diff_file.hunks if h not in stray]
            parsed.warnings.append(
                f"dropped {len(stray)} creation hunk(s) merged into {old_path}; "
                "each file needs its own 'diff --git' block"
            )
        # The file was renamed in the header but the body edits the original:
        # keep the original path so the hunks can anchor.
        parsed.warnings.append(
            f"edited {old_path} although the header named {new_path}"
        )
        diff_file.path = old_path
        diff_file.status = "modified"
        diff_file.is_new = False


def _normalise_path(diff_file: DiffFile, config: Optional[FirewallConfig],
                    parsed: ParsedDiff) -> None:
    """Rewrite a path that cannot be right: absolute-inside-root or repo-prefixed."""
    root = Path(str(getattr(config, "repo_root", "") or "")).resolve() if config else None
    patch_note = ""
    for attr in ("path", "old_path"):
        raw = str(getattr(diff_file, attr) or "").strip()
        if not raw:
            continue
        fixed = raw.replace("\\", "/")
        if fixed in {"/dev/null", "dev/null"}:
            continue
        candidate = Path(fixed)
        if candidate.is_absolute():
            if root is not None:
                try:
                    fixed = str(candidate.resolve().relative_to(root)).replace("\\", "/")
                    patch_note = "absolute path made repo-relative"
                except ValueError:
                    patch_note = "path points outside the repository"
        if root is not None and not Path(fixed).is_absolute():
            parts = fixed.lstrip("./").split("/")
            # ``_site_demo/index.html`` when the repo itself is ``_site_demo``.
            if len(parts) > 1 and parts[0].lower() == root.name.lower():
                fixed = "/".join(parts[1:])
                patch_note = "stripped a leading repository-folder segment"
        setattr(diff_file, attr, fixed)
    if patch_note:
        parsed.warnings.append(
            f"{diff_file.path}: {patch_note} (the model copied the folder name or "
            "an absolute path into the diff header)"
        )


def _definitions(text: str) -> list[str]:
    """Top-level names a file defines (functions/classes), in order."""
    names: list[str] = []
    for line in text.splitlines():
        if line[:1].isspace():
            continue
        stripped = line.strip()
        for prefix in ("async def ", "def ", "class "):
            if stripped.startswith(prefix):
                name = stripped[len(prefix):].split("(", 1)[0].split(":", 1)[0].strip()
                if name and name.isidentifier() and name not in names:
                    names.append(name)
                break
    return names


def _mentions(text: str, name: str) -> bool:
    return re.search(rf"\b{re.escape(name)}\b", text) is not None


def _same_path(left: str, right: str) -> bool:
    return left.replace("\\", "/").lstrip("./").lower() == right.replace(
        "\\", "/"
    ).lstrip("./").lower()


def parse_file(text: str, config: Optional[FirewallConfig] = None) -> DiffFile:
    """Parse a single-file diff, raising when it contains more than one file."""
    parsed = parse(text, config)
    if len(parsed.files) != 1:
        raise DiffParseError(
            f"expected exactly one file, found {len(parsed.files)}",
            detail={"paths": parsed.paths},
        )
    return parsed.files[0]


def parse_hunk(text: str, config: Optional[FirewallConfig] = None) -> Hunk:
    """Parse a lone hunk (``@@`` first, no file header)."""
    cfg = config or FirewallConfig()
    body = text
    if cfg.strip_code_fences:
        body, _ = strip_code_fences(body)
    lines = body.splitlines()
    while lines and not _HUNK_RE.match(lines[0]):
        lines.pop(0)
    if not lines:
        raise DiffParseError("no @@ header found", detail={"reason": "no_hunk"})
    match = _HUNK_RE.match(lines[0])
    assert match is not None  # guarded by the loop above
    hunk = Hunk(
        old_start=int(match.group("old_start")),
        old_count=int(match.group("old_count") or 1),
        new_start=int(match.group("new_start")),
        new_count=int(match.group("new_count") or 1),
        section=match.group("tail").strip(),
        header=lines[0],
    )
    for line in lines[1:]:
        if _HUNK_RE.match(line):
            break
        if line.rstrip() == _NO_NEWLINE:
            if hunk.lines:
                hunk.lines[-1].no_newline = True
            continue
        if not line:
            hunk.lines.append(HunkLine(prefix=" ", text=""))
            continue
        if line[0] in _BODY_PREFIXES:
            hunk.lines.append(HunkLine(prefix=line[0], text=line[1:]))
    _finalise_hunk(hunk)
    return hunk


def _finalise_hunk(hunk: Hunk) -> None:
    """Recompute the real line counts from the body."""
    hunk.actual_old = sum(1 for ln in hunk.lines if ln.prefix in (" ", "-"))
    hunk.actual_new = sum(1 for ln in hunk.lines if ln.prefix in (" ", "+"))


def _clean_git_path(value: str) -> str:
    """Strip ``a/`` / ``b/`` prefixes, quotes and ``/dev/null``."""
    path = value.strip().strip('"')
    if path in {"/dev/null", "dev/null"}:
        return ""
    if path.startswith(("a/", "b/")) and len(path) > 2:
        return path[2:]
    return path


# ── location strategies ──────────────────────────────────────────────────
@dataclass
class MatchResult:
    """Where a hunk landed, and by which strategy."""

    strategy: str  # exact | fuzzy | char | none
    index: int  # 0-based line index of the hunk's first old-side line
    ratio: float = 1.0
    offset: int = 0  # index - hunk.old_start + 1

    @property
    def matched(self) -> bool:
        return self.strategy != "none"

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "index": self.index,
            "ratio": round(self.ratio, 4),
            "offset": self.offset,
        }


def locate(
    target_lines: Sequence[str],
    hunk: Hunk,
    config: Optional[FirewallConfig] = None,
) -> MatchResult:
    """Find where ``hunk`` belongs in ``target_lines``.

    Tries exact → fuzzy → character matching, returning the first strategy that
    lands.  ``hunk.old_start`` is only a *hint*: models routinely miscount, so
    every strategy also searches a window around it.
    """
    cfg = config or FirewallConfig()
    old = hunk.old_lines
    if not old:
        # a pure-insertion hunk with no context: clamp to the declared line
        idx = max(0, min(len(target_lines), hunk.old_start - 1))
        return MatchResult(strategy="exact", index=idx, ratio=1.0, offset=0)

    # Models routinely pad an "append at the end" hunk with trailing *context*
    # blanks that do not exist in the file.  Those blanks carry no content, so they
    # are dropped unconditionally — requiring them to "fit" would keep a hunk that
    # can never anchor (observed live on stats.py, where the whole change died).
    if any(line.strip() for line in old):
        while old and old[-1].strip() == "" and any(line.strip() for line in old[:-1] or [""]):
            old = old[:-1]
    elif not any(line.strip() for line in old):
        old = []

    exact = _match_exact(target_lines, old, hunk.old_start)
    if exact is not None:
        return exact

    fuzzy = _match_fuzzy(target_lines, old, hunk.old_start, cfg)
    if fuzzy is not None:
        return fuzzy

    char = _match_char(target_lines, old, hunk.old_start, cfg)
    if char is not None:
        return char

    # Last resort: compare lines with indentation stripped.  Models frequently
    # re-indent the context they quote (a live run wrote an 8-space `return` for a
    # 4-space one), and a pure-whitespace difference must not reject an otherwise
    # correct change.
    loose = _match_loose(target_lines, old, hunk.old_start)
    if loose is not None:
        return loose

    return MatchResult(strategy="none", index=-1, ratio=0.0)


def _match_loose(
    target: Sequence[str], old: Sequence[str], hint: int
) -> Optional[MatchResult]:
    """Match ignoring leading/trailing whitespace on every line."""
    want = [line.strip() for line in old]
    have = [line.strip() for line in target]
    if not want or len(want) > len(have):
        return None
    window = len(want)
    best: Optional[int] = None
    for start in range(len(have) - window + 1):
        if have[start:start + window] == want:
            if best is None:
                best = start
            elif abs(start - (hint - 1)) < abs(best - (hint - 1)):
                best = start
    if best is None:
        return None
    return MatchResult(strategy="loose", index=best, ratio=0.9,
                       offset=best - (hint - 1))


def _sequence_fits(target: Sequence[str], old: Sequence[str]) -> bool:
    """True when *old* occurs in *target* as a contiguous block (any offset)."""
    if not old:
        return True
    window = len(old)
    if window > len(target):
        return False
    for start in range(len(target) - window + 1):
        if list(target[start:start + window]) == list(old):
            return True
    return False


def _match_exact(
    target: Sequence[str], old: Sequence[str], hint: int
) -> Optional[MatchResult]:
    """Verbatim run at (or near) the declared line."""
    n = len(old)
    if n > len(target):
        return None
    candidates = _hint_offsets(hint, len(target), n)
    for start in candidates:
        if list(target[start : start + n]) == list(old):
            return MatchResult(
                strategy="exact",
                index=start,
                ratio=1.0,
                offset=start - (hint - 1),
            )
    return None


def _hint_offsets(hint: int, total: int, n: int) -> list[int]:
    """Line indexes to try, nearest the hint first."""
    base = max(0, min(total - n, hint - 1))
    offsets = [base]
    for delta in range(1, 60):
        for candidate in (base - delta, base + delta):
            if 0 <= candidate <= total - n:
                offsets.append(candidate)
    # de-duplicate while keeping order
    seen: set[int] = set()
    out: list[int] = []
    for idx in offsets:
        if idx not in seen:
            seen.add(idx)
            out.append(idx)
    return out


def _match_fuzzy(
    target: Sequence[str], old: Sequence[str], hint: int, cfg: FirewallConfig
) -> Optional[MatchResult]:
    """Sliding-window SequenceMatcher around the hint."""
    n = len(old)
    if n > len(target):
        return None
    window = cfg.fuzzy_search_window
    hay = "\n".join(old)
    lo = max(0, hint - 1 - window)
    hi = min(len(target) - n, hint - 1 + window)
    if hi < lo:
        lo, hi = 0, max(0, len(target) - n)
    best: Optional[MatchResult] = None
    step = 1 if (hi - lo) <= 400 else 2
    for start in range(lo, hi + 1, step):
        chunk = "\n".join(target[start : start + n])
        ratio = difflib.SequenceMatcher(None, hay, chunk).ratio()
        if ratio >= cfg.fuzzy_threshold and (best is None or ratio > best.ratio):
            best = MatchResult(
                strategy="fuzzy",
                index=start,
                ratio=ratio,
                offset=start - (hint - 1),
            )
    return best


def _match_char(
    target: Sequence[str], old: Sequence[str], hint: int, cfg: FirewallConfig
) -> Optional[MatchResult]:
    """Whitespace-insensitive character comparison.

    Catches the common case where a model re-indented the context (tabs vs
    spaces, extra nesting) so the lines differ literally but are obviously the
    same code.
    """
    n = len(old)
    if n > len(target):
        return None
    normalised_old = [_squash(line) for line in old]
    non_empty = [s for s in normalised_old if s]
    if not non_empty:
        return None

    hint_low = max(0, hint - 1)
    window = cfg.fuzzy_search_window
    lo = max(0, hint_low - window)
    hi = min(len(target) - n, hint_low + window)
    if hi < lo:
        lo, hi = 0, max(0, len(target) - n)

    best: Optional[MatchResult] = None
    for start in range(lo, hi + 1):
        chunk = [_squash(line) for line in target[start : start + n]]
        shared = sum(1 for a, b in zip(normalised_old, chunk) if a and a == b)
        ratio = shared / max(1, len(non_empty))
        if ratio >= cfg.char_match_min_ratio and (best is None or ratio > best.ratio):
            best = MatchResult(
                strategy="char",
                index=start,
                ratio=ratio,
                offset=start - (hint - 1),
            )
    return best


def _squash(line: str) -> str:
    """Collapse all whitespace so indentation differences stop mattering."""
    return " ".join(line.split())


# ── applying a hunk to a line list ───────────────────────────────────────
def apply_hunk_lines(
    target: Sequence[str], hunk: Hunk, match: MatchResult
) -> list[str]:
    """Return ``target`` with ``hunk`` applied at ``match.index``.

    The hunk's *body* is authoritative: context and deletions must line up with
    what is really there (we already located it), and additions are inserted in
    place.  A mismatch here means the locate step was too lax, so it raises.
    """
    if not match.matched:
        raise ContextMismatchError(
            "hunk has no located position", detail={"hunk": hunk.header}
        )
    out = list(target)
    cursor = match.index
    for line in hunk.lines:
        if line.prefix == " ":
            if cursor >= len(out):
                # A trailing context line past EOF is padding the model invented
                # for "append at the end of file" — it carries no content, so it is
                # skipped instead of failing the whole change.  A *removal* past
                # EOF is still an error: that would delete nothing and misreport.
                if line.text == "":
                    continue
                raise ContextMismatchError(
                    "context line ran past end of file",
                    detail={"line": line.text, "hunk": hunk.header},
                )
            cursor += 1
        elif line.prefix == "-":
            if cursor >= len(out):
                raise ContextMismatchError(
                    "deletion ran past end of file",
                    detail={"line": line.text, "hunk": hunk.header},
                )
            del out[cursor]
        elif line.prefix == "+":
            out.insert(cursor, line.text)
            cursor += 1
    return out


def apply_hunks(
    target: Sequence[str],
    hunks: Sequence[Hunk],
    config: Optional[FirewallConfig] = None,
    *,
    origin: int = 0,
) -> tuple[list[str], list[MatchResult]]:
    """Apply hunks in order, re-locating each against the *updated* text.

    Later hunks in a file were written against the original line numbers, so
    once an earlier hunk changed the line count the hints drift.  We therefore
    locate each hunk against the text as it stands, which handles both gap
    hunks and overlapping edits without a separate offset bookkeeping pass.
    """
    cfg = config or FirewallConfig()
    text = list(target)
    matches: list[MatchResult] = []
    for hunk in hunks:
        match = locate(text, hunk, cfg)
        if not match.matched:
            raise ContextMismatchError(
                "no match strategy could anchor the hunk",
                detail={
                    "hunk": hunk.header,
                    "old_start": hunk.old_start,
                    "old_preview": hunk.old_lines[:3],
                },
            )
        text = apply_hunk_lines(text, hunk, match)
        matches.append(match)
    return text, matches


def render(text: str) -> str:
    """Normalise the reconstructed file text (preserve a trailing newline)."""
    return text


def _has_trailing_newline(hunk: Hunk) -> bool:
    if not hunk.lines:
        return True
    return not hunk.lines[-1].no_newline


# ── path safety ──────────────────────────────────────────────────────────
def is_path_allowed(path: str, config: Optional[FirewallConfig] = None) -> tuple[bool, str]:
    """Return ``(allowed, reason)`` for a diff target path."""
    cfg = config or FirewallConfig()
    if not path:
        return False, "empty path"
    normalised = path.replace("\\", "/").lstrip("/")
    if any(part == ".." for part in normalised.split("/")):
        return False, "path escapes the repository root"
    if normalised.startswith(("~", "/")):
        return False, "absolute or home-relative path"
    casefolded = normalised.casefold()
    if is_sensitive_workspace_path(normalised):
        return False, "protected local secrets path"
    if cfg.apply_allow_paths:
        allowed = any(
            fnmatch.fnmatchcase(normalised, pattern) for pattern in cfg.apply_allow_paths
        )
        if not allowed:
            return False, f"outside allow-list: {sorted(cfg.apply_allow_paths)}"
    is_env_template = casefolded.rsplit("/", 1)[-1] == ".env.example"
    for pattern in cfg.apply_deny_globs:
        folded_pattern = pattern.casefold()
        if is_env_template and folded_pattern in {".env.*", "**/.env.*", "*.env.*"}:
            continue
        if fnmatch.fnmatchcase(casefolded, folded_pattern) or fnmatch.fnmatchcase(
            casefolded, folded_pattern.lstrip("*")
        ):
            return False, f"matches deny-glob: {pattern}"
        if "/" not in pattern and fnmatch.fnmatchcase(
            casefolded.rsplit("/", 1)[-1], folded_pattern
        ):
            return False, f"matches deny-glob: {pattern}"
    return True, ""


# ── application to a worktree ────────────────────────────────────────────
@dataclass
class FileOutcome:
    """What happened to one file while applying a diff."""

    path: str
    status: str  # applied | created | deleted | renamed | skipped | failed
    strategy: str = ""
    hunks_applied: int = 0
    hunks_total: int = 0
    additions: int = 0
    deletions: int = 0
    error: str = ""
    offset: int = 0
    strategies: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "status": self.status,
            "strategy": self.strategy,
            "strategies": list(self.strategies),
            "hunks_applied": self.hunks_applied,
            "hunks_total": self.hunks_total,
            "additions": self.additions,
            "deletions": self.deletions,
            "offset": self.offset,
            "error": self.error,
        }


@dataclass
class ApplyResult:
    """The whole-diff apply outcome, including rollback bookkeeping."""

    ok: bool
    files: list[FileOutcome] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    rolled_back: bool = False
    dry_run: bool = False
    backups: dict[str, str] = field(default_factory=dict)

    @property
    def applied_paths(self) -> list[str]:
        return [f.path for f in self.files if f.status in {"applied", "created", "renamed", "deleted"}]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "dry_run": self.dry_run,
            "rolled_back": self.rolled_back,
            "files": [f.to_dict() for f in self.files],
            "errors": list(self.errors),
            "applied": self.applied_paths,
        }


class Applier:
    """Applies parsed diffs to a worktree, atomically and path-safely."""

    def __init__(self, config: Optional[FirewallConfig] = None) -> None:
        self.config = config or FirewallConfig()
        self.root = self.config.root

    # ── public ───────────────────────────────────────────────────────────
    def apply(
        self,
        parsed: ParsedDiff,
        *,
        dry_run: bool = False,
        paths: Optional[Sequence[str]] = None,
    ) -> ApplyResult:
        """Apply ``parsed`` under the repo root.

        Validation happens in two phases: **pre-flight** (path safety, file
        counts, hunk counts, existence) fails the whole diff before anything is
        written; **execution** writes each file and, on the first failure,
        restores everything already written when ``apply_atomic`` is set.
        """
        cfg = self.config
        result = ApplyResult(ok=True, dry_run=dry_run)
        if not cfg.apply_enabled:
            result.ok = False
            result.errors.append("diff application is disabled (GF_FW__APPLY_ENABLED=false)")
            return result

        selected = set(paths) if paths else None
        targets = [f for f in parsed.files if selected is None or f.path in selected]
        if not targets:
            result.ok = False
            result.errors.append("no files selected to apply")
            return result

        # ── pre-flight ───────────────────────────────────────────────────
        if len(targets) > cfg.apply_max_files:
            result.ok = False
            result.errors.append(
                f"diff touches {len(targets)} files, limit is {cfg.apply_max_files}"
            )
            return result
        total_hunks = sum(len(f.hunks) for f in targets)
        if total_hunks > cfg.apply_max_hunks:
            result.ok = False
            result.errors.append(
                f"diff contains {total_hunks} hunks, limit is {cfg.apply_max_hunks}"
            )
            return result
        for target in targets:
            allowed, reason = is_path_allowed(target.path, cfg)
            if not allowed:
                result.ok = False
                result.errors.append(f"{target.path}: {reason}")
            if target.status == "added" and not cfg.apply_allow_new_files:
                result.ok = False
                result.errors.append(f"{target.path}: creating new files is disabled")
            if target.status in {"deleted", "renamed"} and not cfg.apply_allow_deletes:
                result.ok = False
                result.errors.append(f"{target.path}: deleting/renaming is disabled")
        if not result.ok:
            return result

        prepared: list[tuple[DiffFile, Path, str, list[str], list[MatchResult]]] = []
        replacements: list[DiffFile] = []
        for target in targets:
            if getattr(target, "replacement", ""):
                # A whole-file rewrite that arrived as a "new file" diff has no
                # hunks to anchor — the body *is* the file, so it is written whole
                # (with the usual backup) instead of going through hunk matching.
                replacements.append(target)
                continue
            try:
                prepared.append(self._prepare(target))
            except Exception as exc:  # noqa: BLE001 - surfaced as a structured error
                # The hunk could not anchor. Before failing the file, check whether
                # the diff's *new side* is a complete rewrite that keeps every
                # top-level definition: models rewording a stub file produce exactly
                # that, and the write is safe (backup + definition guard).
                recovered = False
                try:
                    body = "\n".join(
                        line.text for hunk in target.hunks for line in hunk.lines
                        if line.prefix in {"+", " "}
                    )
                    abs_target = self._safe_path(target.path)
                    current = (abs_target.read_text(encoding="utf-8", errors="replace")
                               if abs_target.is_file() else "")
                    lost = [name for name in _definitions(current)
                            if not _mentions(body, name)]
                    if body.strip() and current and not lost:
                        target.replacement = body
                        target.status = "modified"
                        replacements.append(target)
                        recovered = True
                        result.errors.append(
                            f"{target.path}: hunks did not anchor; using whole-file "
                            "replacement (definitions preserved)")
                except Exception:  # noqa: BLE001 - fall through to the failure
                    recovered = False
                if recovered:
                    continue
                result.ok = False
                result.errors.append(f"{target.path}: {exc}")
        if not result.ok:
            return result

        # ── data-loss guards ─────────────────────────────────────────────
        # A model with no context often "adds" a file that already exists
        # (``new file mode`` + ``--- /dev/null``).  Applying that literally either
        # replaces the real content or, if the body is empty, truncates the file to
        # zero bytes.  Both happened on a live run, so a diff that would destroy
        # content is now refused *before* a single byte is written.
        for target, abs_path, original, new_lines, _matches in prepared:
            exists = abs_path.exists()
            declares_new = target.status == "added" or target.is_new
            if declares_new and exists and not cfg.apply_overwrite_existing:
                result.ok = False
                result.errors.append(
                    f"{target.path}: the diff creates a file that already exists "
                    f"(refusing to overwrite {abs_path.name})"
                )
            current = original if exists else ""
            if current.strip() and not "".join(new_lines).strip():
                result.ok = False
                result.errors.append(
                    f"{target.path}: the diff would empty a non-empty file"
                )
        if not result.ok:
            return result

        if dry_run:
            for target in targets:
                outcome = FileOutcome(
                    path=target.path,
                    status="skipped",
                    hunks_total=len(target.hunks),
                    additions=target.additions,
                    deletions=target.deletions,
                )
                try:
                    _, _, _, _, matches = next(p for p in prepared if p[0] is target)
                    outcome.strategy = matches[-1].strategy if matches else "exact"
                    outcome.strategies = sorted({m.strategy for m in matches})
                    outcome.hunks_applied = len(matches)
                    outcome.offset = matches[0].offset if matches else 0
                except StopIteration:  # pragma: no cover - prepared mirrors targets
                    pass
                result.files.append(outcome)
            return result

        # ── execution ────────────────────────────────────────────────────
        # Each file is applied independently: one hallucinated hunk in a *test*
        # file must not discard a correct edit to the module beside it.  A file
        # that fails is reported with its own error and the rest proceed; the whole
        # diff only counts as failed when nothing applied (`apply_partial`).
        written: list[tuple[Path, Optional[str]]] = []  # (path, original_content|None)
        created: list[Path] = []
        applied_any = False
        for target in replacements:
            try:
                abs_path = self._safe_path(target.path)
                original = (abs_path.read_text(encoding="utf-8", errors="replace")
                            if abs_path.is_file() else "")
                self._backup(abs_path, original, result)
                abs_path.parent.mkdir(parents=True, exist_ok=True)
                body = target.replacement
                if not body.endswith("\n"):
                    body += "\n"
                if not dry_run:
                    abs_path.write_text(body, encoding="utf-8")
                written.append((abs_path, original))
                applied_any = True
                result.files.append(FileOutcome(
                    path=target.path, status="applied", strategy="replace",
                    hunks_total=0, additions=len(body.splitlines()), deletions=0))
            except Exception as exc:  # noqa: BLE001 - one file, not the whole diff
                result.files.append(FileOutcome(path=target.path, status="failed",
                                                error=str(exc)[:300]))
                result.errors.append(f"{target.path}: {exc}")
        for target, abs_path, original, new_lines, matches in prepared:
            try:
                if target.status == "deleted":
                    if abs_path.exists():
                        self._backup(abs_path, original, result)
                        abs_path.unlink()
                        written.append((abs_path, original))
                    result.files.append(
                        FileOutcome(
                            path=target.path,
                            status="deleted",
                            hunks_total=len(target.hunks),
                            deletions=target.deletions,
                        )
                    )
                    applied_any = True
                    continue
                if target.status == "renamed" and target.old_path and target.old_path != target.path:
                    old_abs = self._safe_path(target.old_path)
                    if old_abs.exists():
                        self._backup(old_abs, None, result)
                        created.append(old_abs)
                        old_content = old_abs.read_text(encoding="utf-8", errors="replace")
                        old_abs.unlink()
                        abs_path.parent.mkdir(parents=True, exist_ok=True)
                        abs_path.write_text(old_content, encoding="utf-8")
                        written.append((abs_path, None))
                        applied_any = True
                    result.files.append(
                        FileOutcome(
                            path=target.path,
                            status="renamed",
                            hunks_total=len(target.hunks),
                            strategies=sorted({m.strategy for m in matches}),
                        )
                    )
                    continue

                self._backup(abs_path, original, result)
                if not abs_path.exists():
                    created.append(abs_path)
                abs_path.parent.mkdir(parents=True, exist_ok=True)
                newline = "\n"
                text = newline.join(new_lines)
                if new_lines and _ends_with_newline(target):
                    text += newline
                abs_path.write_text(text, encoding="utf-8")
                written.append((abs_path, original))
                applied_any = True
                result.files.append(
                    FileOutcome(
                        path=target.path,
                        status="created" if target.status == "added" else "applied",
                        strategy=matches[-1].strategy if matches else "exact",
                        strategies=sorted({m.strategy for m in matches}),
                        hunks_applied=len(matches),
                        hunks_total=len(target.hunks),
                        additions=target.additions,
                        deletions=target.deletions,
                        offset=matches[0].offset if matches else 0,
                    )
                )
            except Exception as exc:  # noqa: BLE001 - one file, not the whole diff
                # A model that rewrites a whole small file usually gets the *shape*
                # right and the quoted context wrong: the hunk cannot anchor although
                # the new body is exactly what the file should become.  Before giving
                # up, take the new side of the diff as a whole-file replacement —
                # accepted only when no top-level definition of the current file
                # disappears, so a bad rewrite can never delete code.
                recovered = False
                try:
                    body = "\n".join(
                        line.text for hunk in target.hunks for line in hunk.lines
                        if line.prefix in {"+", " "}
                    )
                    current = (abs_path.read_text(encoding="utf-8", errors="replace")
                               if abs_path.is_file() else "")
                    lost = [name for name in _definitions(current)
                            if not _mentions(body, name)]
                    if body.strip() and current and not lost:
                        self._backup(abs_path, current, result)
                        text = body if body.endswith("\n") else body + "\n"
                        abs_path.write_text(text, encoding="utf-8")
                        written.append((abs_path, current))
                        applied_any = True
                        recovered = True
                        result.files.append(FileOutcome(
                            path=target.path, status="applied", strategy="replace",
                            hunks_total=len(target.hunks),
                            additions=len(body.splitlines()), deletions=0))
                        result.errors.append(
                            f"{target.path}: hunks did not anchor; whole-file "
                            "replacement used (definitions preserved)")
                except Exception:  # noqa: BLE001 - fall through to the plain failure
                    recovered = False
                if recovered:
                    continue
                result.files.append(FileOutcome(
                    path=target.path,
                    status="failed",
                    hunks_total=len(target.hunks),
                    additions=target.additions,
                    deletions=target.deletions,
                    error=str(exc)[:400],
                ))
                result.errors.append(f"{target.path}: {exc}")
                if not cfg.apply_partial:
                    result.ok = False
                    if cfg.apply_atomic:
                        result.rolled_back = self._rollback(written, created, result)
                    return result

        # Success means "something was written".  An empty run (no file prepared,
        # every file skipped) used to fall through to ``ok = True`` and the harness
        # reported a task as done while the working tree was untouched — the one
        # answer a user must be able to trust.
        result.ok = bool(applied_any)
        if not applied_any:
            if not result.errors:
                result.errors.append("the diff produced no changes to apply")
            if cfg.apply_atomic:
                result.rolled_back = self._rollback(written, created, result)
        return result

    def apply_text(
        self,
        text: str,
        *,
        dry_run: bool = False,
        path_hint: str = "",
        config: Optional[FirewallConfig] = None,
    ) -> ApplyResult:
        """Convenience: parse ``text`` and apply it."""
        parsed = parse(text, config or self.config, path_hint=path_hint)
        return self.apply(parsed, dry_run=dry_run)

    # ── preparation ──────────────────────────────────────────────────────
    def _prepare(
        self, target: DiffFile
    ) -> tuple[DiffFile, Path, str, list[str], list[MatchResult]]:
        """Read the target, locate and apply every hunk in memory."""
        abs_path = self._safe_path(target.path)
        if target.status == "added" or target.is_new:
            original = ""
            base_lines: list[str] = []
        else:
            if not abs_path.exists():
                raise ContextMismatchError(
                    f"target file does not exist: {target.path}",
                    detail={"path": target.path},
                )
            original = abs_path.read_text(encoding="utf-8", errors="replace")
            base_lines = original.splitlines()

        if not target.hunks:
            return target, abs_path, original, base_lines, []

        new_lines, matches = apply_hunks(base_lines, target.hunks, self.config)
        return target, abs_path, original, new_lines, matches

    def _safe_path(self, path: str) -> Path:
        if is_sensitive_workspace_path(self.root.name):
            raise PathNotAllowedError(
                "the selected project root is a protected local secrets directory",
                detail={"root": str(self.root)},
            )
        allowed, reason = is_path_allowed(path, self.config)
        if not allowed:
            raise PathNotAllowedError(f"{path}: {reason}", detail={"path": path})
        candidate = (self.root / path).resolve()
        try:
            resolved_relative = candidate.relative_to(self.root).as_posix()
        except ValueError as exc:
            raise PathNotAllowedError(
                f"{path}: resolves outside the repository root",
                detail={"path": path, "resolved": str(candidate)},
            ) from exc
        allowed, reason = is_path_allowed(resolved_relative, self.config)
        if not allowed:
            raise PathNotAllowedError(
                f"{path}: resolved target is protected: {reason}",
                detail={"path": path, "resolved_path": resolved_relative},
            )
        return candidate

    def _backup(self, abs_path: Path, original: Optional[str], result: ApplyResult) -> None:
        """Remember what a path looked like *now*, whoever claims it is new.

        The backup used to be skipped when the caller passed ``None`` (a "new"
        file), so a failed verification could not restore the content the applier
        had just destroyed.  Reading the file here closes that hole: the backup is
        what is actually on disk.
        """
        if not self.config.apply_write_backups:
            return
        content = original
        if content is None:
            try:
                if abs_path.exists():
                    content = abs_path.read_text(encoding="utf-8", errors="replace")
            except OSError:  # pragma: no cover - unreadable is treated as absent
                content = None
        if content is None:
            return
        try:
            result.backups[str(abs_path)] = content
        except Exception:  # pragma: no cover - backups are best-effort
            log.debug("could not record a backup for %s", abs_path, exc_info=True)

    def _rollback(
        self,
        written: Sequence[tuple[Path, Optional[str]]],
        created: Sequence[Path],
        result: ApplyResult,
    ) -> bool:
        """Restore every file written so far; report whether it fully worked."""
        ok = True
        for path, original in reversed(list(written)):
            try:
                if original is None:
                    if path.exists() and path not in created:
                        path.unlink()
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(original, encoding="utf-8")
            except OSError as exc:
                ok = False
                result.errors.append(
                    ApplyRollbackError(
                        f"failed to restore {path}: {exc}", detail={"path": str(path)}
                    ).message
                )
        for path in created:
            try:
                if path.exists() and path.suffix and not any(
                    p == path for p, _ in written
                ):
                    path.unlink()
            except OSError as exc:  # pragma: no cover - best-effort cleanup
                ok = False
                result.errors.append(f"failed to remove created file {path}: {exc}")
        return ok


def _ends_with_newline(target: DiffFile) -> bool:
    """True unless the diff's last line explicitly says there is no newline."""
    for hunk in reversed(target.hunks):
        if hunk.lines:
            return not hunk.lines[-1].no_newline
    return True


__all__ = [
    "HunkLine",
    "Hunk",
    "DiffFile",
    "ParsedDiff",
    "MatchResult",
    "FileOutcome",
    "ApplyResult",
    "Applier",
    "parse",
    "parse_file",
    "parse_hunk",
    "locate",
    "apply_hunk_lines",
    "apply_hunks",
    "strip_code_fences",
    "is_path_allowed",
]
