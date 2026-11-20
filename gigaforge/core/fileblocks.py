"""Whole-file blocks from a model answer.

A unified diff is the right format for *editing* code: it is small, reviewable and
forces the model to quote the lines it touches.  It is the wrong format for
*creating* a large file: the model must count lines, repeat the whole body with a
``+`` prefix, and one miscounted hunk header rejects everything.  That is exactly
how a real request ("write two good HTML pages") failed.

So the harness accepts a second, simpler protocol alongside diffs::

    ### FILE: index.html
    <!DOCTYPE html>
    ...

Anything that names a path and follows it with a fenced or raw body becomes a
whole-file write.  The blocks are converted into ordinary "new file" diffs, so
they still pass through the same path-safety, backup, apply and verification code —
one write mechanism, not two.
"""
from __future__ import annotations

import re
from typing import Dict

__all__ = [
    "extract_file_blocks",
    "files_to_diff",
    "has_file_blocks",
    "FILE_BLOCK_INSTRUCTION",
]

FILE_BLOCK_INSTRUCTION = """For NEW files, prefer whole-file blocks over diffs:

### FILE: path/to/new_file.html
```
<the complete file content>
```

One block per file, the complete content, no placeholders, no "...". For files that
already exist, use a unified diff instead."""

#: ``### FILE: p``, ``## File: p``, ``**FILE:** p``, ``==> p <==``, ``FILE: p``.
_MARKER_RE = re.compile(
    r"^[ \t]*(?:#{2,4}[ \t]*|[-*][ \t]+|\*\*)?"
    r"(?:file|файл|path|путь)[ \t]*[:»]\s*[*`\"']?(?P<path>[^\s`*\"'<>|]+)"
    r"[*`\"']?[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)
_ALT_MARKER_RE = re.compile(r"^[ \t]*==>[ \t]*(?P<path>[^\s<]+)[ \t]*<==[ \t]*$",
                            re.MULTILINE)
_FENCE_RE = re.compile(r"^[ \t]*(?:```|~~~)(?P<info>[^\n]*)$")


def has_file_blocks(text: str) -> bool:
    return bool(_MARKER_RE.search(text or "") or _ALT_MARKER_RE.search(text or ""))


def extract_file_blocks(text: str) -> Dict[str, str]:
    """Map ``path -> full content`` for every file block in *text*.

    Accepts a fenced body (```lang ... ```) or, when no fence follows the marker,
    everything up to the next marker.  A body shorter than 20 characters is
    ignored: that is a model stalling, not a file.
    """
    source = (text or "").replace("\r\n", "\n")
    markers: list[tuple[int, int, str]] = []
    for pattern in (_MARKER_RE, _ALT_MARKER_RE):
        for match in pattern.finditer(source):
            path = _clean_path(match.group("path"))
            if path:
                markers.append((match.start(), match.end(), path))
    if not markers:
        return {}
    markers.sort(key=lambda item: item[0])

    files: Dict[str, str] = {}
    for index, (_start, body_start, path) in enumerate(markers):
        end = markers[index + 1][0] if index + 1 < len(markers) else len(source)
        body = source[body_start:end]
        body = _strip_fence(body)
        content = body.strip("\n")
        if len(content) < 20:
            continue
        files.setdefault(path, content + "\n")
    return files


def _clean_path(raw: str) -> str:
    value = (raw or "").strip().strip("`*\"'")
    value = value.replace("\\", "/").lstrip("./")
    if not value or " " in value:
        return ""
    if value in {"/dev/null", "dev/null"}:
        return ""
    return value


def _strip_fence(body: str) -> str:
    """Drop the outer code fence (and its language tag) if the body has one."""
    lines = body.split("\n")
    start = 0
    while start < len(lines) and not lines[start].strip():
        start += 1
    if start < len(lines) and _FENCE_RE.match(lines[start]):
        lines = lines[start + 1:]
        end = len(lines) - 1
        while end >= 0 and not lines[end].strip():
            end -= 1
        if end >= 0 and re.fullmatch(r"[ \t]*(?:```|~~~)[ \t]*", lines[end]):
            lines = lines[:end]
    return "\n".join(lines)


def files_to_diff(files: Dict[str, str]) -> str:
    """Render whole-file writes as ``new file`` diffs (the apply path's input)."""
    chunks: list[str] = []
    for path, content in files.items():
        body = content.split("\n")
        if body and body[-1] == "":
            body.pop()
        header = [
            f"diff --git a/{path} b/{path}",
            "new file mode 100644",
            "--- /dev/null",
            f"+++ b/{path}",
            f"@@ -0,0 +1,{len(body)} @@",
        ]
        chunks.append("\n".join(header + [f"+{line}" for line in body]))
    return "\n".join(chunks) + ("\n" if chunks else "")
