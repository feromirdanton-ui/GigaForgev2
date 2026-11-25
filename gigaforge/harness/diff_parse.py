"""One parser for every diff the harness reads.

**Why this module exists.**  Three functions in ``core/pipeline.py`` each
decided where one file's hunks ended and the next began, and all three knew
only one header form::

    if line.startswith("diff --git "):

``_drop_hunks_for``, ``_keep_hunks_for`` and ``_merge_repaired_diff`` exist to
protect files the harness assembled itself.  The harness's own normaliser,
``firewall.validator._add_hinted_file_headers``, produces the *other* form — a
bare ``--- a/X`` / ``+++ b/X`` pair with no ``diff --git`` line — and hands it
straight back to them.  Measured on the real pipeline::

    hinted shape: '--- a/index.html\\n+++ b/index.html\\n@@ -1 +1 @@\\n-HARNESS\\n+MODEL\\n'
    drop(hinted) == hinted ? True      # not dropped
    drop(full)   == full   ? False     # dropped

so ``final_diff`` described a model edit that was not in the tree, and the
report said "file restored" while the diff still carried the edit.  A green
suite of 1062 tests did not see it: not one of the eleven ownership tests fed
a bare hunk.

The fix is not a second ``startswith`` in three places.  It is one parser that
answers "which file does this line belong to?" for both forms, so a fourth
caller cannot get it wrong.

**What counts as a file block.**  A block starts at either

* ``diff --git a/X b/X`` — the canonical form, and
* a ``--- a/X`` line immediately followed by ``+++ b/X`` — the bare form.

A ``---``/``+++`` pair that names the *same* path as the block already open is
that block's hunk header, not a new file: a multi-hunk diff repeats it only
once, but a repaired diff can carry both a ``diff --git`` line and the pair.

``/dev/null`` on either side means creation or deletion; the path is taken from
the side that names a real file.
"""
from __future__ import annotations

from dataclasses import dataclass

__all__ = ["FileBlock", "split_diff", "normalise_path", "paths_in"]


@dataclass(frozen=True)
class FileBlock:
    """One file's worth of a unified diff, header lines included."""

    path: str
    text: str

    def __bool__(self) -> bool:  # a block with no path is not a block
        return bool(self.path and self.text)


def normalise_path(path: str) -> str:
    """The comparison form of a path: forward slashes, no ``a/``/``b/``/``./``.

    ``index.html`` and ``./index.html`` and ``b/index.html`` are the same file;
    ``sub/index.html`` is not, which is why the prefix is stripped only once and
    only when it is exactly ``a/`` or ``b/``.
    """
    text = (path or "").strip().replace("\\", "/")
    if text.startswith(("a/", "b/")):
        text = text[2:]
    while text.startswith("./"):
        text = text[2:]
    return text


def _marker_path(line: str) -> str:
    """The path on a ``---``/``+++`` line, or ``""`` for ``/dev/null``."""
    rest = line[4:].strip()
    if not rest or rest.startswith("/dev/null"):
        return ""
    # A timestamp may follow a tab; it is not part of the path.
    rest = rest.split("\t")[0].strip()
    return normalise_path(rest)


def _git_header_path(line: str) -> str:
    """The path named by a ``diff --git a/X b/X`` line."""
    for token in line.split()[2:]:
        if token.startswith(("a/", "b/")):
            return normalise_path(token)
    return ""


def split_diff(diff: str) -> list[FileBlock]:
    """Split *diff* into per-file blocks, understanding both header forms.

    Text that is not a diff at all returns ``[]`` — the caller decides what
    that means.  Order is preserved, so a caller that concatenates the blocks
    back gets the input unchanged.
    """
    if not diff:
        return []
    lines = diff.splitlines(keepends=True)
    blocks: list[FileBlock] = []
    current: list[str] = []
    current_path = ""

    def flush() -> None:
        if current and current_path:
            blocks.append(FileBlock(current_path, "".join(current)))

    index = 0
    while index < len(lines):
        line = lines[index]

        if line.startswith("diff --git "):
            flush()
            current = [line]
            current_path = _git_header_path(line)
            index += 1
            continue

        if line.startswith("--- ") and index + 1 < len(lines) and lines[index + 1].startswith("+++ "):
            old = _marker_path(line)
            new = _marker_path(lines[index + 1])
            path = new or old
            if path and path == current_path:
                # The hunk header of the block already open.
                current.append(line)
                index += 1
                continue
            if path:
                flush()
                current = [line, lines[index + 1]]
                current_path = path
                index += 2
                continue

        if current_path:
            current.append(line)
        index += 1

    flush()
    return blocks


def paths_in(diff: str) -> list[str]:
    """Every path the diff touches, in first-seen order."""
    seen: list[str] = []
    for block in split_diff(diff):
        if block.path not in seen:
            seen.append(block.path)
    return seen
