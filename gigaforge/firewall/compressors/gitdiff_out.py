"""Firewall compressor for unified diff output (``git diff``).

Parses ``git diff`` unified diff format into:
- ``files_changed``: one entry per changed file with path, added, removed counts.
- ``hunks``: one entry per ``@@ -a,b +c,d @@`` hunk with path, best-effort
  ``symbol`` (last ``def name(``/``class Name(`` line in the hunk's context),
  and a ``lines`` summary ``+A/-R``.

Targets >=50x compression on a 2000-line diff.
"""
from __future__ import annotations

import re
from typing import Any

from gigaforge.firewall.base import cap_list, cap_str, enforce_token_budget, fail_dict

__all__ = ["GitDiffCompressor", "compress"]

# ``diff --git a/path b/path`` header.
_DIFF_HEADER_RE = re.compile(r"^diff --git a/(?P<a>\S+) b/(?P<b>\S+)")

# ``--- a/path`` / ``+++ b/path`` (path may be quoted with ``"`` if it has spaces).
_OLD_FILE_RE = re.compile(r'^---\s+(?:a/)?(?P<p>\S+?)\s*(?:\t|$)')
_NEW_FILE_RE = re.compile(r'^\+\+\+\s+(?:b/)?(?P<p>\S+?)\s*(?:\t|$)')

# Hunk header: ``@@ -a,b +c,d @@ optional_context``.
_HUNK_RE = re.compile(
    r"^@@\s+-(?P<o_start>\d+)(?:,(?P<o_count>\d+))?\s+\+(?P<n_start>\d+)(?:,(?P<n_count>\d+))?\s+@@"
)

# Best-effort symbol line. Look at the leading whitespace-tolerant form.
_DEF_RE = re.compile(r"^\s*(?:async\s+)?def\s+(?P<n>[A-Za-z_][A-Za-z0-9_]*)\s*\(")
_CLASS_RE = re.compile(r"^\s*class\s+(?P<n>[A-Za-z_][A-Za-z0-9_]*)\b")
# Other languages' equivalents.
_FUNC_RE = re.compile(r"^\s*function\s+(?P<n>[A-Za-z_][A-Za-z0-9_]*)\s*\(")
_PUB_FN_RE = re.compile(r"^\s*(?:pub(?:lic)?\s+)?fn\s+(?P<n>[A-Za-z_][A-Za-z0-9_]*)\s*\(")

_MAX_FILES = 20
_MAX_HUNKS = 30


class GitDiffCompressor:
    """Compress a unified ``git diff`` into a small JSON dict."""

    name = "gitdiff"

    def compress(self, raw: str, *, limit_tokens: int = 4000) -> dict[str, Any]:
        if not raw or not raw.strip():
            return {"files_changed": [], "hunks": [], "_truncated_files": 0, "_truncated_hunks": 0}

        try:
            result = self._parse(raw, limit_tokens=limit_tokens)
        except Exception as exc:
            return fail_dict(f"gitdiff parse failure: {exc.__class__.__name__}: {exc}", raw)

        return enforce_token_budget(result, limit_tokens)

    # ------------------------------------------------------------------ parse
    def _parse(self, raw: str, *, limit_tokens: int) -> dict[str, Any]:
        lines = raw.splitlines()

        files_changed: list[dict[str, Any]] = []
        hunks: list[dict[str, Any]] = []

        # State per-file.
        current_path: str | None = None
        # State per-hunk.
        in_hunk = False
        added = 0
        removed = 0
        last_symbol: str | None = None
        # Per-file accumulator (across all hunks in that file).
        file_added = 0
        file_removed = 0

        def _close_hunk() -> None:
            nonlocal in_hunk, added, removed, last_symbol
            if in_hunk and current_path is not None:
                hunks.append(
                    {
                        "path": current_path,
                        "symbol": last_symbol or "",
                        "lines": f"+{added}/-{removed}",
                    }
                )
            in_hunk = False
            added = 0
            removed = 0
            last_symbol = None

        def _close_file() -> None:
            nonlocal current_path, file_added, file_removed
            _close_hunk()
            if current_path is not None and (file_added or file_removed):
                files_changed.append(
                    {
                        "path": current_path,
                        "added": file_added,
                        "removed": file_removed,
                    }
                )
            file_added = 0
            file_removed = 0
            current_path = None

        for line in lines:
            if line.startswith("diff --git"):
                _close_file()
                m = _DIFF_HEADER_RE.match(line)
                if m:
                    # Prefer the b/ side (the new path).
                    current_path = m.group("b")
                continue
            if line.startswith("--- "):
                # Sometimes the only path signal (e.g. plain ``diff -u``); use it
                # if no diff --git header came first.
                if current_path is None:
                    m = _OLD_FILE_RE.match(line)
                    if m:
                        current_path = m.group("p")
                continue
            if line.startswith("+++ "):
                if current_path is None:
                    m = _NEW_FILE_RE.match(line)
                    if m:
                        current_path = m.group("p")
                continue
            if line.startswith("@@"):
                _close_hunk()
                m = _HUNK_RE.match(line)
                if m and current_path is not None:
                    in_hunk = True
                continue

            if not in_hunk:
                continue

            # We're inside a hunk's body.
            if line.startswith("+") and not line.startswith("+++"):
                added += 1
                file_added += 1
                sym = _find_symbol(line[1:])
                if sym:
                    last_symbol = sym
            elif line.startswith("-") and not line.startswith("---"):
                removed += 1
                file_removed += 1
                sym = _find_symbol(line[1:])
                if sym:
                    last_symbol = sym
            else:
                # Context line (starts with space) or "\ No newline at end of file".
                if line.startswith(" "):
                    sym = _find_symbol(line[1:])
                    if sym:
                        last_symbol = sym

        # Close the final hunk + file.
        _close_file()

        kept_files, dropped_files = cap_list(files_changed, _MAX_FILES)
        kept_hunks, dropped_hunks = cap_list(hunks, _MAX_HUNKS)

        per_field_cap = max(40, limit_tokens // 16)

        result: dict[str, Any] = {
            "files_changed": [
                {
                    "path": cap_str(f["path"], per_field_cap),
                    "added": f["added"],
                    "removed": f["removed"],
                }
                for f in kept_files
            ],
            "hunks": [
                {
                    "path": cap_str(h["path"], per_field_cap),
                    "symbol": cap_str(h["symbol"], 60),
                    "lines": h["lines"],
                }
                for h in kept_hunks
            ],
        }
        if dropped_files:
            result["_truncated_files"] = dropped_files
        if dropped_hunks:
            result["_truncated_hunks"] = dropped_hunks
        return result


# --------------------------------------------------------------------- helpers
def _find_symbol(line: str) -> str | None:
    """Best-effort symbol extraction from a single source line."""
    for rx in (_DEF_RE, _CLASS_RE, _FUNC_RE, _PUB_FN_RE):
        m = rx.match(line)
        if m:
            return m.group("n")
    return None


# Module-level instance + convenience function.
_gitdiff_compressor = GitDiffCompressor()


def compress(raw: str, *, limit_tokens: int = 4000) -> dict[str, Any]:
    """Compress ``git diff`` output. See :class:`GitDiffCompressor`."""
    return _gitdiff_compressor.compress(raw, limit_tokens=limit_tokens)
