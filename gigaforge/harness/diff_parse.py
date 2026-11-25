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


#: Escapes git emits inside a quoted path.  Octal is the one that matters: git
#: quotes any path with a non-ASCII byte and writes each byte as ``\NNN``.
_GIT_ESCAPES = {
    "a": 0x07, "b": 0x08, "t": 0x09, "n": 0x0A, "v": 0x0B, "f": 0x0C,
    "r": 0x0D, '"': 0x22, "\\": 0x5C,
}


def unquote_git_path(token: str) -> str:
    """Undo git's C-style quoting of a path, or return *token* unchanged.

    **Why this exists.**  ``git diff`` quotes any path containing a non-ASCII
    byte or a control character and writes the bytes as octal escapes::

        diff --git "a/\\321\\201.html" "b/\\321\\201.html"

    ``split_diff`` used to take that token literally, so the path came out as
    ``"b//321/201.html"`` — quotes kept, backslashes turned into slashes by
    :func:`normalise_path`.  The file was then unrecognised, ``_drop_hunks_for``
    did not drop its hunk, and ``final_diff`` described a file that was not in
    the tree.  A Russian-speaking user names a file in Russian, so this is the
    common case, not an edge case.

    The decoding is byte-exact: escapes are collected as bytes and decoded as
    UTF-8 at the end, because a multi-byte character is several octal escapes
    and decoding them one at a time would corrupt it.
    """
    text = (token or "").strip()
    if len(text) < 2 or not (text.startswith('"') and text.endswith('"')):
        return text
    body = text[1:-1]
    out = bytearray()
    index = 0
    length = len(body)
    while index < length:
        char = body[index]
        if char != "\\" or index + 1 >= length:
            out.extend(char.encode("utf-8", "surrogateescape"))
            index += 1
            continue
        nxt = body[index + 1]
        if nxt in "01234567":
            # Up to three octal digits, as git writes them.
            digits = ""
            cursor = index + 1
            while cursor < length and len(digits) < 3 and body[cursor] in "01234567":
                digits += body[cursor]
                cursor += 1
            out.append(int(digits, 8) & 0xFF)
            index = cursor
            continue
        if nxt in _GIT_ESCAPES:
            out.append(_GIT_ESCAPES[nxt])
            index += 2
            continue
        # An escape git does not emit: keep it verbatim rather than guess.
        out.extend(char.encode("utf-8", "surrogateescape"))
        index += 1
    return out.decode("utf-8", "surrogateescape")


def normalise_path(path: str) -> str:
    """The comparison form of a path: forward slashes, no ``a/``/``b/``/``./``.

    ``index.html`` and ``./index.html`` and ``b/index.html`` are the same file;
    ``sub/index.html`` is not, which is why the prefix is stripped only once and
    only when it is exactly ``a/`` or ``b/``.
    """
    text = unquote_git_path(path or "").strip().replace("\\", "/")
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
    # A quoted path may contain spaces, so it is taken whole; an unquoted one
    # ends at the first tab (a timestamp may follow).
    if rest.startswith('"'):
        end = rest.find('"', 1)
        while end != -1 and rest[end - 1] == "\\":
            end = rest.find('"', end + 1)
        if end != -1:
            return normalise_path(rest[: end + 1])
    rest = rest.split("\t")[0].strip()
    return normalise_path(rest)


def _git_header_path(line: str) -> str:
    """The path named by a ``diff --git a/X b/X`` line, or ``""`` if ambiguous.

    Two shapes reach here:

    * both sides quoted \u2014 ``diff --git "a/\\321\\201.html" "b/\\321\\201.html"``;
    * neither quoted \u2014 ``diff --git a/my page.html b/my page.html``.

    The unquoted shape is genuinely ambiguous: git does **not** quote a space in
    this header, so ``str.split()`` cuts ``my page.html`` in half and the block
    is opened under the path ``my``.  The separator is the literal `` b/``, and
    the two sides must agree after normalisation \u2014 if they do not, the header
    is not trusted and ``""`` is returned so the following ``---``/``+++`` pair
    (which *is* tab-terminated) opens the block instead.
    """
    rest = line[len("diff --git "):].strip()
    if rest.startswith('"'):
        tokens: list[str] = []
        index = 0
        while index < len(rest) and len(tokens) < 2:
            if rest[index] == '"':
                end = rest.find('"', index + 1)
                while end != -1 and rest[end - 1] == "\\":
                    end = rest.find('"', end + 1)
                if end == -1:
                    return ""
                tokens.append(rest[index: end + 1])
                index = end + 1
            else:
                end = rest.find(" ", index)
                if end == -1:
                    tokens.append(rest[index:])
                    break
                tokens.append(rest[index:end])
                index = end + 1
            while index < len(rest) and rest[index] == " ":
                index += 1
        if len(tokens) == 2 and normalise_path(tokens[0]) == normalise_path(tokens[1]):
            return normalise_path(tokens[1])
        return ""

    split_at = rest.find(" b/")
    if split_at == -1:
        return ""
    left, right = rest[:split_at], rest[split_at + 1:]
    if not left.startswith("a/") or not right.startswith("b/"):
        return ""
    if normalise_path(left) != normalise_path(right):
        return ""
    return normalise_path(right)


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
