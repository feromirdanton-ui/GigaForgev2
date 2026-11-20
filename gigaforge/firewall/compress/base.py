"""Compression primitives shared by every firewall compressor.

The firewall never summarises with a model.  Every compressor is a
**deterministic parser** that turns raw tool output (pytest, grep, git diff,
compiler diagnostics) into a small structured JSON payload with hard field
limits.  Whenever something is cut, the payload records it — either inline with
the ``[truncated]`` marker or via the boolean ``truncated`` flag plus counters
— so the agent always knows the view it received is partial.

This module owns the mechanics that are identical across compressors:

* :class:`Truncator` — appends :data:`MARKER` and flips a flag;
* :func:`clip`, :func:`clip_multiline`, :func:`take` — bounded field helpers;
* :class:`CompressionResult` — the envelope every compressor returns.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional, Sequence, TypeVar

T = TypeVar("T")

#: Inline marker appended to any string that had to be cut.
MARKER = "[truncated]"


class Truncator:
    """Tracks whether anything was cut while building a payload."""

    __slots__ = ("_hit",)

    def __init__(self) -> None:
        self._hit = False

    @property
    def hit(self) -> bool:
        return self._hit

    def note(self, cut: bool = True) -> bool:
        if cut:
            self._hit = True
        return cut

    def mark(self, text: str, limit: int, *, marker: str = MARKER) -> str:
        """Clip ``text`` to ``limit`` chars, appending ``marker`` when cut."""
        return clip(text, limit, marker=marker, truncator=self)

    def take(self, seq: Sequence[T], limit: int) -> list[T]:
        """Return at most ``limit`` items, flagging when the tail was dropped."""
        return take(seq, limit, truncator=self)


def clip(
    text: str,
    limit: int,
    *,
    marker: str = MARKER,
    truncator: Optional[Truncator] = None,
) -> str:
    """Clip ``text`` to ``limit`` characters on a word-ish boundary.

    The marker is appended (and a truncator flagged) only when the text was
    actually longer than ``limit``, so an untouched field never grows a
    misleading marker.
    """
    if limit <= 0:
        if truncator is not None:
            truncator.note(bool(text))
        return "" if not text else ""
    if text is None:
        return ""
    if len(text) <= limit:
        return text
    if truncator is not None:
        truncator.note(True)
    room = max(0, limit - len(marker) - 1)
    body = text[:room]
    # Prefer cutting at the last space so we do not bisect a token.
    space = body.rfind(" ")
    if space > room * 0.6:
        body = body[:space]
    return body.rstrip() + " " + marker


def clip_multiline(
    text: str,
    max_lines: int,
    max_chars: int,
    *,
    line_chars: Optional[int] = None,
    marker: str = MARKER,
    truncator: Optional[Truncator] = None,
) -> str:
    """Clip a multi-line excerpt by line count and by total characters."""
    lines = text.splitlines()
    if len(lines) > max_lines:
        if truncator is not None:
            truncator.note(True)
        lines = lines[:max_lines] + [marker]
    if line_chars:
        lines = [clip(ln, line_chars, marker=marker, truncator=truncator) for ln in lines]
    out = "\n".join(lines)
    return clip(out, max_chars, marker=marker, truncator=truncator)


def take(
    seq: Sequence[T],
    limit: int,
    *,
    truncator: Optional[Truncator] = None,
) -> list[T]:
    """Return at most ``limit`` items, flagging when items were dropped."""
    if limit < 0:
        limit = 0
    if len(seq) <= limit:
        return list(seq)
    if truncator is not None:
        truncator.note(True)
    return list(seq[:limit])


def estimate_tokens(chars: int, chars_per_token: float = 4.0) -> int:
    """Estimate tokens from a character count (the shared IGAFORGE heuristic)."""
    if chars <= 0:
        return 0
    divisor = chars_per_token if chars_per_token and chars_per_token > 0 else 4.0
    return int(chars / divisor + 0.999)


def squeeze(text: str) -> str:
    """Collapse noisy whitespace so limits are spent on content, not blanks."""
    lines = [ln.rstrip() for ln in text.splitlines()]
    out: list[str] = []
    blanks = 0
    for ln in lines:
        if ln.strip():
            blanks = 0
            out.append(ln)
        else:
            blanks += 1
            if blanks <= 1:
                out.append("")
    while out and not out[0].strip():
        out.pop(0)
    while out and not out[-1].strip():
        out.pop()
    return "\n".join(out)


def relativise(path: str, root: str) -> str:
    """Best-effort path shortening: prefer a repo-relative form."""
    if not path:
        return ""
    cleaned = path.replace("\\", "/")
    if root:
        r = root.replace("\\", "/").rstrip("/")
        if r and cleaned.startswith(r + "/"):
            return cleaned[len(r) + 1 :]
    return cleaned


@dataclass
class CompressionResult:
    """Envelope returned by every compressor.

    ``data`` is the structured payload; ``text`` is a compact JSON rendering of
    it, ready to drop into a prompt.  ``source_chars`` / ``compressed_chars``
    drive the savings metrics reported to the pipeline.
    """

    kind: str
    data: dict[str, Any]
    source_chars: int
    compressed_chars: int
    truncated: bool = False
    truncated_fields: list[str] = field(default_factory=list)
    chars_per_token: float = 4.0
    meta: dict[str, Any] = field(default_factory=dict)

    # ── derived ──────────────────────────────────────────────────────────
    @property
    def tokens_before(self) -> int:
        return estimate_tokens(self.source_chars, self.chars_per_token)

    @property
    def tokens_after(self) -> int:
        return estimate_tokens(self.compressed_chars, self.chars_per_token)

    @property
    def tokens_saved(self) -> int:
        return max(0, self.tokens_before - self.tokens_after)

    @property
    def savings_ratio(self) -> float:
        if self.source_chars <= 0:
            return 0.0
        return max(0.0, 1.0 - (self.compressed_chars / self.source_chars))

    # ── rendering ────────────────────────────────────────────────────────
    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "truncated": self.truncated,
            "truncated_fields": list(self.truncated_fields),
            "source_chars": self.source_chars,
            "compressed_chars": self.compressed_chars,
            "tokens_before": self.tokens_before,
            "tokens_after": self.tokens_after,
            "tokens_saved": self.tokens_saved,
            "savings_ratio": round(self.savings_ratio, 4),
            **({"meta": self.meta} if self.meta else {}),
            "data": self.data,
        }

    @property
    def text(self) -> str:
        return json.dumps(self.data, ensure_ascii=False, separators=(",", ":"))

    def render(self) -> str:
        """Full JSON envelope (payload + compression accounting)."""
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"))


def finalise(
    kind: str,
    data: dict[str, Any],
    source: str,
    truncator: Truncator,
    *,
    chars_per_token: float = 4.0,
    truncated_fields: Optional[Iterable[str]] = None,
    meta: Optional[dict[str, Any]] = None,
) -> CompressionResult:
    """Build the result envelope, computing the compressed size from ``data``."""
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return CompressionResult(
        kind=kind,
        data=data,
        source_chars=len(source),
        compressed_chars=len(payload),
        truncated=truncator.hit,
        truncated_fields=sorted(set(truncated_fields or ())),
        chars_per_token=chars_per_token,
        meta=dict(meta or {}),
    )


__all__ = [
    "MARKER",
    "Truncator",
    "CompressionResult",
    "clip",
    "clip_multiline",
    "take",
    "squeeze",
    "relativise",
    "estimate_tokens",
    "finalise",
]
