"""``git diff`` / ``git show`` output → structured JSON.

The compressor keeps the *shape* of a change (which files, how many hunks, how
many added/removed lines, the actual hunks bounded to a line budget) and throws
away the noise: full index hashes, absolute timestamps, and long context runs.

Binary diffs, mode changes and renames are represented explicitly so the agent
is never misled into thinking a file's content is the whole story.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from ..config import FirewallConfig
from .base import CompressionResult, Truncator, clip, finalise, squeeze, take

_DIFF_HEADER_RE = re.compile(r"^diff --git a/(?P<a>.+?) b/(?P<b>.+?)$")
_HUNK_RE = re.compile(
    r"^@@\s+-(?P<old_start>\d+)(?:,(?P<old_count>\d+))?\s+\+(?P<new_start>\d+)(?:,(?P<new_count>\d+))?\s*@@(?P<tail>.*)$"
)
_INDEX_RE = re.compile(r"^index [0-9a-f]+\.\.[0-9a-f]+(?:\s+\d+)?$")
_RENAME_RE = re.compile(r"^rename from (?P<src>.+)$")
_RENAME_TO_RE = re.compile(r"^rename to (?P<dst>.+)$")
_NEW_FILE_RE = re.compile(r"^new file mode (?P<mode>\d+)$")
_DEL_FILE_RE = re.compile(r"^deleted file mode (?P<mode>\d+)$")
_MODE_RE = re.compile(r"^(?P<kind>old|new) mode (?P<mode>\d+)$")
_STAT_LINE_RE = re.compile(
    r"^\s*(?P<path>.+?)\s*\|\s*(?P<changes>\d+)\s*(?P<bar>[+\-]+)?\s*$"
)
_STAT_TOTAL_RE = re.compile(
    r"^\s*(?P<files>\d+)\s+files? changed(?:,\s*(?P<ins>\d+)\s+insertions?\(\+\))?(?:,\s*(?P<del>\d+)\s+deletions?\(-\))?"
)


class GitDiffCompressor:
    """Compresses unified diffs and ``--stat`` output into a bounded change map."""

    kind = "gitdiff"

    def __init__(self, config: Optional[FirewallConfig] = None) -> None:
        self.config = config or FirewallConfig()

    # ── public ───────────────────────────────────────────────────────────
    def compress(self, source: str, **overrides: Any) -> CompressionResult:
        cfg = self.config.with_(**overrides) if overrides else self.config
        tr = Truncator()
        lines = squeeze(source).splitlines()

        if self._looks_like_stat(lines):
            data = self._stat(lines, cfg, tr)
        else:
            data = self._unified(lines, cfg, tr)

        return finalise(
            self.kind,
            data,
            source,
            tr,
            chars_per_token=cfg.chars_per_token,
            truncated_fields=data.pop("_cut", []) if "_cut" in data else [],
        )

    # ── unified diff ─────────────────────────────────────────────────────
    def _unified(self, lines: list[str], cfg: FirewallConfig, tr: Truncator) -> dict[str, Any]:
        files: list[dict[str, Any]] = []
        current: Optional[dict[str, Any]] = None
        current_hunk: Optional[dict[str, Any]] = None
        pending_a = ""
        cut: list[str] = []
        total_add = 0
        total_del = 0

        def close_hunk() -> None:
            nonlocal current_hunk
            if current is not None and current_hunk is not None:
                current["hunks"].append(current_hunk)
            current_hunk = None

        def close_file() -> None:
            nonlocal current
            close_hunk()
            if current is not None:
                files.append(current)
            current = None

        for line in lines:
            head = _DIFF_HEADER_RE.match(line)
            if head:
                close_file()
                pending_a = head.group("a")
                current = {
                    "path": head.group("b"),
                    "old_path": pending_a,
                    "status": "modified",
                    "binary": False,
                    "additions": 0,
                    "deletions": 0,
                    "hunks": [],
                    "hunks_total": 0,
                }
                continue
            if current is None:
                continue
            if _INDEX_RE.match(line):
                continue
            rename = _RENAME_RE.match(line)
            if rename:
                current["old_path"] = rename.group("src")
                current["status"] = "renamed"
                continue
            rename_to = _RENAME_TO_RE.match(line)
            if rename_to:
                current["path"] = rename_to.group("dst")
                current["status"] = "renamed"
                continue
            if _NEW_FILE_RE.match(line):
                current["status"] = "added"
                continue
            if _DEL_FILE_RE.match(line):
                current["status"] = "deleted"
                continue
            if line.startswith("Binary files ") or line.startswith("GIT binary patch"):
                current["binary"] = True
                continue
            mode = _MODE_RE.match(line)
            if mode:
                current.setdefault("mode", {})[mode.group("kind")] = mode.group("mode")
                continue
            if line.startswith("---") or line.startswith("+++"):
                continue

            hunk = _HUNK_RE.match(line)
            if hunk:
                close_hunk()
                current["hunks_total"] += 1
                if len(current["hunks"]) < cfg.diff_max_hunks_per_file:
                    current_hunk = {
                        "header": clip(line, cfg.diff_max_line_chars, marker=cfg.truncation_marker, truncator=tr),
                        "old_start": int(hunk.group("old_start")),
                        "old_count": int(hunk.group("old_count") or 1),
                        "new_start": int(hunk.group("new_start")),
                        "new_count": int(hunk.group("new_count") or 1),
                        "section": hunk.group("tail").strip(),
                        "lines": [],
                        "lines_total": 0,
                        "additions": 0,
                        "deletions": 0,
                    }
                else:
                    tr.note(True)
                    current_hunk = None
                    cut.append("hunks")
                continue

            if current_hunk is None:
                continue
            # Hunk body line: ' ', '+', '-' or '\ No newline...'
            prefix = line[:1]
            if prefix not in {" ", "+", "-"} and not line.startswith("\\"):
                if line.startswith("diff --git"):
                    continue
            current_hunk["lines_total"] += 1
            if prefix == "+":
                current_hunk["additions"] += 1
                current["additions"] += 1
                total_add += 1
            elif prefix == "-":
                current_hunk["deletions"] += 1
                current["deletions"] += 1
                total_del += 1
            if len(current_hunk["lines"]) < cfg.diff_max_hunk_lines:
                current_hunk["lines"].append(
                    clip(line, cfg.diff_max_line_chars, marker=cfg.truncation_marker, truncator=tr)
                )
            else:
                tr.note(True)
                cut.append("hunk_lines")

        close_file()

        if len(files) > cfg.diff_max_files:
            tr.note(True)
            cut.append("files")
        kept = take(files, cfg.diff_max_files, truncator=tr)
        return {
            "format": "unified",
            "totals": {
                "files": len(files),
                "returned": len(kept),
                "additions": total_add,
                "deletions": total_del,
                "hunks": sum(f["hunks_total"] for f in files),
            },
            "files": kept,
            "truncated": tr.hit,
            "_cut": sorted(set(cut)),
        }

    # ── --stat ───────────────────────────────────────────────────────────
    def _looks_like_stat(self, lines: list[str]) -> bool:
        for line in lines[:40]:
            if _DIFF_HEADER_RE.match(line) or _HUNK_RE.match(line):
                return False
        return any(_STAT_LINE_RE.match(ln) for ln in lines[:40])

    def _stat(self, lines: list[str], cfg: FirewallConfig, tr: Truncator) -> dict[str, Any]:
        files: list[dict[str, Any]] = []
        total_line = ""
        for line in lines:
            total = _STAT_TOTAL_RE.match(line)
            if total:
                total_line = line.strip()
                continue
            match = _STAT_LINE_RE.match(line)
            if match:
                path = match.group("path").strip()
                bar = match.group("bar") or ""
                files.append(
                    {
                        "path": path,
                        "changes": int(match.group("changes")),
                        "additions": bar.count("+"),
                        "deletions": bar.count("-"),
                    }
                )
        if len(files) > cfg.diff_max_stat_files:
            tr.note(True)
        totals: dict[str, Any] = {"files": len(files), "summary": total_line}
        match = _STAT_TOTAL_RE.match(total_line) if total_line else None
        if match:
            totals["insertions"] = int(match.group("ins") or 0)
            totals["deletions"] = int(match.group("del") or 0)
        return {
            "format": "stat",
            "totals": totals,
            "files": take(files, cfg.diff_max_stat_files, truncator=tr),
            "truncated": tr.hit,
        }


__all__ = ["GitDiffCompressor"]
