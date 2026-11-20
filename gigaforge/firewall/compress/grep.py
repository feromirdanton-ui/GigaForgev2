"""``grep`` / ``ripgrep`` output → structured JSON.

Handles the three shapes a search tool emits:

* ``--vimgrep`` / ``-rn``: ``path:line:col:text`` and ``path:line:text``;
* plain ``-n``: ``line:text`` (no file column);
* context runs (``-C``/``-A``/``-B``): ``path-line-text`` separators for
  context lines and ``path:line:text`` for matches, plus ``--`` group breaks.

Output carries ``matches`` (bounded), ``count_by_file``, ``files``, ``totals``
and an optional ``context`` block for the lines surrounding each match.  Nothing
is invented: a line that cannot be parsed as a grep record is collected into
``unparsed`` rather than silently dropped.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from ..config import FirewallConfig
from .base import CompressionResult, Truncator, clip, finalise, squeeze, take

# ``path:line:col:text`` (vimgrep) or ``path:line:text`` (grep -n)
_MATCH_RE = re.compile(
    r"^(?P<file>[^:][^:]*?):(?P<line>\d+):(?:(?P<col>\d+):)?(?P<text>.*)$"
)
# context line: ``path-line-text`` / ``path:line:col-text``.  The separator is
# repeated around the line number, so we capture it once and back-reference it
# with Python's ``(?P=sep)`` (there is no ``\k<name>`` syntax in ``re``).
_CONTEXT_RE = re.compile(
    r"^(?P<file>[^:][^:]*?)(?P<sep>[-:])(?P<line>\d+)(?P=sep)(?:(?P<col>\d+)(?P=sep))?(?P<text>.*)$"
)
_BARE_RE = re.compile(r"^(?P<line>\d+):(?P<text>.*)$")
_GROUP_BREAK_RE = re.compile(r"^--\s*$")


class GrepCompressor:
    """Turns grep output into a per-file, per-line index with counts."""

    kind = "grep"

    def __init__(self, config: Optional[FirewallConfig] = None) -> None:
        self.config = config or FirewallConfig()

    def compress(self, source: str, **overrides: Any) -> CompressionResult:
        cfg = self.config.with_(**overrides) if overrides else self.config
        tr = Truncator()
        lines = squeeze(source).splitlines()

        matches: list[dict[str, Any]] = []
        context: list[dict[str, Any]] = []
        unparsed: list[str] = []
        counts: dict[str, int] = {}
        total = 0
        file_hint = ""

        for raw in lines:
            line = raw.rstrip()
            if not line or _GROUP_BREAK_RE.match(line):
                continue
            match = _MATCH_RE.match(line)
            if match:
                total += 1
                path = match.group("file")
                file_hint = path
                counts[path] = counts.get(path, 0) + 1
                if len(matches) < cfg.grep_max_matches:
                    matches.append(self._record(match, path, cfg, tr))
                else:
                    tr.note(True)
                continue
            ctx = _CONTEXT_RE.match(line)
            if ctx and (not _BARE_RE.match(line)):
                if len(context) < cfg.grep_max_matches * (cfg.grep_context_lines + 1):
                    context.append(
                        {
                            "file": ctx.group("file"),
                            "line": int(ctx.group("line")),
                            "kind": "context",
                            "text": clip(
                                ctx.group("text"),
                                cfg.grep_max_line_chars,
                                marker=cfg.truncation_marker,
                                truncator=tr,
                            ),
                        }
                    )
                else:
                    tr.note(True)
                continue
            bare = _BARE_RE.match(line)
            if bare and file_hint:
                total += 1
                counts[file_hint] = counts.get(file_hint, 0) + 1
                if len(matches) < cfg.grep_max_matches:
                    matches.append(
                        {
                            "file": file_hint,
                            "line": int(bare.group("line")),
                            "column": 0,
                            "text": clip(
                                bare.group("text"),
                                cfg.grep_max_line_chars,
                                marker=cfg.truncation_marker,
                                truncator=tr,
                            ),
                        }
                    )
                else:
                    tr.note(True)
                continue
            if len(unparsed) < 20:
                unparsed.append(clip(line, cfg.grep_max_line_chars, marker=cfg.truncation_marker, truncator=tr))
            else:
                tr.note(True)

        ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        if len(ranked) > cfg.grep_max_files:
            tr.note(True)
        files = [
            {"path": path, "count": n} for path, n in take(ranked, cfg.grep_max_files, truncator=tr)
        ]

        data: dict[str, Any] = {
            "totals": {
                "matches": total,
                "returned": len(matches),
                "files": len(counts),
                "truncated": total > len(matches) or len(ranked) > cfg.grep_max_files,
            },
            "files": files,
            "matches": matches,
            "context": self._bundle_context(context, cfg, tr) if context else [],
            **({"unparsed": unparsed} if unparsed else {}),
        }
        return finalise(
            self.kind,
            data,
            source,
            tr,
            chars_per_token=cfg.chars_per_token,
            truncated_fields=["matches"] if total > len(matches) else [],
        )

    # ── helpers ──────────────────────────────────────────────────────────
    @staticmethod
    def _record(
        match: re.Match[str], path: str, cfg: FirewallConfig, tr: Truncator
    ) -> dict[str, Any]:
        return {
            "file": path,
            "line": int(match.group("line")),
            "column": int(match.group("col") or 0),
            "text": clip(
                match.group("text"),
                cfg.grep_max_line_chars,
                marker=cfg.truncation_marker,
                truncator=tr,
            ),
        }

    def _bundle_context(
        self, context: list[dict[str, Any]], cfg: FirewallConfig, tr: Truncator
    ) -> list[dict[str, Any]]:
        """Group context lines by the file they belong to."""
        by_file: dict[str, list[dict[str, Any]]] = {}
        for item in context:
            by_file.setdefault(item["file"], []).append(item)
        out: list[dict[str, Any]] = []
        for path, items in by_file.items():
            body = "\n".join(f"{it['line']}: {it['text']}" for it in items)
            out.append(
                {
                    "path": path,
                    "lines": items,
                    "text": clip(body, cfg.grep_max_context_chars, marker=cfg.truncation_marker, truncator=tr),
                }
            )
        return out


__all__ = ["GrepCompressor"]
