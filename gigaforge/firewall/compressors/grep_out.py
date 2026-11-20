"""Firewall compressor for ripgrep / grep output.

Parses lines in the canonical ``path:line:content`` form (ripgrep default,
``grep -n`` with ``:`` separator) into a compact list of the top-N most
relevant matches. Relevance scores by:

- a symbol definition appearing on the line (``def name(``, ``class Name(``,
  ``function name(``, ``const NAME =``) — strong signal that the line is the
  symbol's definition,
- a target symbol name appearing in the content (caller/callee match),
- dedup by file: at most one match per file path (configurable).

Targets >=20x compression on 200 matches.
"""
from __future__ import annotations

import re
from typing import Any

from gigaforge.firewall.base import cap_str, enforce_token_budget, fail_dict

__all__ = ["GrepCompressor", "compress"]

# ripgrep / grep -n: ``path:line:content``. Path may contain colons on Windows
# (``C:\...``) — handled by anchoring on the FIRST ``:`` after a line-number.
# We split into (path, line_no, content) by finding ``path : int : content``.
_MATCH_RE = re.compile(r"^(?P<file>.+?):(?P<line>\d+):(?P<content>.*)$")

# Heuristic: the line defines a symbol. Group 1 is the symbol name.
_DEF_RES = [
    re.compile(r"\bdef\s+(?P<n>[A-Za-z_][A-Za-z0-9_]*)\s*\("),
    re.compile(r"\bclass\s+(?P<n>[A-Za-z_][A-Za-z0-9_]*)\b"),
    re.compile(r"\bfunction\s+(?P<n>[A-Za-z_][A-Za-z0-9_]*)\s*\("),
    re.compile(r"\b(?:const|let|var)\s+(?P<n>[A-Za-z_][A-Za-z0-9_]*)\s*="),
]

_DEFAULT_TOP_N = 10
_DEFAULT_PER_FILE = 1


class GrepCompressor:
    """Compress ripgrep / grep output to a small JSON dict."""

    name = "grep"

    def compress(
        self,
        raw: str,
        *,
        limit_tokens: int = 4000,
        target_symbol: str | None = None,
        top_n: int = _DEFAULT_TOP_N,
    ) -> dict[str, Any]:
        if not raw or not raw.strip():
            return {"matches": [], "_truncated": 0}

        try:
            result = self._parse(
                raw,
                limit_tokens=limit_tokens,
                target_symbol=target_symbol,
                top_n=top_n,
            )
        except Exception as exc:
            return fail_dict(f"grep parse failure: {exc.__class__.__name__}: {exc}", raw)

        return enforce_token_budget(result, limit_tokens)

    # ------------------------------------------------------------------ parse
    def _parse(
        self,
        raw: str,
        *,
        limit_tokens: int,
        target_symbol: str | None,
        top_n: int,
    ) -> dict[str, Any]:
        target = (target_symbol or "").strip()
        # word-boundary regex for an exact target_symbol occurrence.
        target_re = re.compile(rf"\b{re.escape(target)}\b") if target else None

        scored: list[dict[str, Any]] = []
        for line in raw.splitlines():
            if not line.strip():
                continue
            m = _MATCH_RE.match(line)
            if not m:
                # Could be a filename-only header (``--heading`` mode) or a
                # ``path:line-content`` variant; skip silently.
                continue
            file = m.group("file")
            try:
                line_no = int(m.group("line"))
            except ValueError:
                continue
            content = m.group("content")

            symbol = _extract_symbol(content)
            score = _score(symbol, content, target, target_re)
            scored.append(
                {
                    "file": file,
                    "line": line_no,
                    "symbol": symbol,
                    "content": content,
                    "_score": score,
                }
            )

        # Sort by score desc; stable secondary by (file, line).
        scored.sort(key=lambda d: (-d["_score"], d["file"], d["line"]))

        # Dedup by file: keep the top match per file.
        seen_files: set[str] = set()
        deduped: list[dict[str, Any]] = []
        for item in scored:
            if item["file"] in seen_files:
                continue
            seen_files.add(item["file"])
            deduped.append(item)
            if len(deduped) >= top_n:
                break

        dropped = max(0, len(scored) - len(deduped))

        # Per-field token caps. The result should fit comfortably below
        # ``limit_tokens``; with top_n matches we target ~limit_tokens/4 for the
        # whole serialized result (leaves headroom for the ``_truncated`` marker
        # and any per-field JSON overhead). Content gets the lion's share but is
        # capped aggressively enough that the per-match cost is bounded.
        target_total_tokens = max(50, limit_tokens // 4)
        per_match_tokens = target_total_tokens // max(1, top_n)
        # Content: cap at 30 tokens (120 chars) — enough for a def/class header
        # plus a short comment, but bounded.
        content_cap_tokens = max(15, min(30, (per_match_tokens * 3) // 5))
        # File: ~80 chars (20 tokens) at most.
        file_cap_tokens = max(10, min(20, per_match_tokens // 5))
        # Symbol: ~40 chars (10 tokens) at most.
        symbol_cap_tokens = max(5, min(10, per_match_tokens // 10))

        matches_out: list[dict[str, Any]] = []
        for item in deduped:
            file_field = cap_str(item["file"], file_cap_tokens)
            content_field = cap_str(item["content"], content_cap_tokens)
            symbol_field = cap_str(item["symbol"], symbol_cap_tokens)
            matches_out.append(
                {
                    "file": file_field,
                    "line": item["line"],
                    "symbol": symbol_field,
                    "content": content_field,
                }
            )

        result: dict[str, Any] = {"matches": matches_out}
        if dropped:
            result["_truncated"] = dropped
        else:
            result["_truncated"] = 0
        return result


# --------------------------------------------------------------------- helpers
def _extract_symbol(content: str) -> str:
    """Best-effort symbol name from a grep match line."""
    for rx in _DEF_RES:
        m = rx.search(content)
        if m:
            return m.group("n")
    # Fallback: a callable form ``foo(...)`` at line start (after whitespace).
    m = re.match(r"\s*(?:async\s+)?(?P<n>[A-Za-z_][A-Za-z0-9_]*)\s*\(", content)
    if m:
        return m.group("n")
    return ""


def _score(
    symbol: str,
    content: str,
    target: str,
    target_re: re.Pattern[str] | None,
) -> int:
    """Relevance score; higher is more relevant."""
    s = 0
    if symbol:
        s += 5  # line declares a recognizable symbol
    if target:
        if symbol == target:
            s += 30  # the target symbol's own definition
        if target_re and target_re.search(content):
            s += 15  # the target is referenced (caller / callee)
    return s


# Module-level instance + convenience function.
_grep_compressor = GrepCompressor()


def compress(
    raw: str,
    *,
    limit_tokens: int = 4000,
    target_symbol: str | None = None,
    top_n: int = _DEFAULT_TOP_N,
) -> dict[str, Any]:
    """Compress grep / ripgrep output. See :class:`GrepCompressor`."""
    return _grep_compressor.compress(
        raw,
        limit_tokens=limit_tokens,
        target_symbol=target_symbol,
        top_n=top_n,
    )
