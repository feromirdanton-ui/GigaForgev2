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
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, TypeVar

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
    truncator: Truncator | None = None,
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
    line_chars: int | None = None,
    marker: str = MARKER,
    truncator: Truncator | None = None,
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
    truncator: Truncator | None = None,
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
    #: When set, ``text`` returns this verbatim instead of a JSON rendering of
    #: ``data``.  Used by the passthrough path: a payload that is already the
    #: smallest form must not be wrapped in ``{"text": ...}``, which would add
    #: 12 characters to a 4-character answer and hand the model
    #: ``{"text":"391\n"}`` where it asked for ``391``.
    raw: str | None = None

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
        if self.raw is not None:
            return self.raw
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
    truncated_fields: Iterable[str] | None = None,
    meta: dict[str, Any] | None = None,
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


def coerce_result(
    value: Any,
    *,
    kind: str,
    source: str = "",
    chars_per_token: float = 4.0,
) -> CompressionResult:
    """Normalise whatever a compressor returned into a :class:`CompressionResult`.

    **Why this exists.**  The project has two generations of compressors: the
    newer ones in :mod:`gigaforge.firewall.compress` return a
    ``CompressionResult``, and the older ones in
    :mod:`gigaforge.firewall.compressors` return a bare ``dict``.  The public
    dispatcher ``gigaforge.firewall.compress()`` routed to both and returned
    whatever came back, so a caller that did the documented thing —

        packed = compress("grep", raw)
        prompt += packed.text

    — got ``AttributeError: 'dict' object has no attribute 'text'`` on the
    legacy path and a working object on the new one.  A public function whose
    return type depends on which internal branch it took is not a contract.

    A ``dict`` is wrapped, not re-parsed: its keys are already the payload, and
    re-running a compressor over its own output would double-count the
    truncation flags.  ``source`` is passed so the token accounting stays
    honest — a wrapped legacy payload still reports what it saved.
    """
    if isinstance(value, CompressionResult):
        return value
    if isinstance(value, dict):
        payload = dict(value)
        # Legacy compressors signal truncation with a private ``_truncated``
        # counter rather than the envelope's flag; lift it so a caller reading
        # ``.truncated`` is not told a cut payload was complete.
        truncated = bool(payload.pop("_truncated", 0))
        rendered = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        return CompressionResult(
            kind=kind,
            data=payload,
            source_chars=len(source),
            compressed_chars=len(rendered),
            truncated=truncated,
            chars_per_token=chars_per_token,
        )
    # A compressor that returned a string (or anything else) is still usable:
    # treat it as the payload body rather than raising inside a best-effort
    # path that runs on every tool observation.
    text = "" if value is None else str(value)
    return CompressionResult(
        kind=kind,
        data={"text": text},
        source_chars=len(source),
        compressed_chars=len(text),
        chars_per_token=chars_per_token,
    )


def shrink_or_passthrough(result: CompressionResult, source: str) -> CompressionResult:
    """Never hand back a payload larger than the raw output it replaced.

    **Measured, not assumed.**  A structured payload carries field names, so on
    a *small* input it is bigger than the input: two grep lines (44 chars) become
    a 142-char JSON object with ``file``/``line``/``symbol``/``content`` keys.
    The firewall's whole purpose is to spend fewer tokens, and a compressor that
    expands a short observation is doing the opposite of its job — the model
    pays for the schema and gets no information it did not already have.

    The fix is not to disable the structured form (it is what makes a *large*
    payload cheap); it is to compare and take the smaller one.  The result says
    which happened, so a report can tell "compressed 40x" from "passed through".

    The comparison is against the **rendered** text, not ``compressed_chars``:
    the first version of this function compared the wrong number and then
    wrapped the source in ``{"text": ...}``, so a 4-character answer became a
    16-character JSON object — the passthrough was itself an expansion.
    """
    if not source:
        return result
    rendered = result.text
    if len(rendered) <= len(source):
        return result
    return CompressionResult(
        kind=result.kind,
        data={"text": source},
        source_chars=len(source),
        compressed_chars=len(source),
        truncated=False,
        chars_per_token=result.chars_per_token,
        meta={**result.meta, "passthrough": "structured payload was larger"},
        # Verbatim: the source IS the smallest form.  Wrapping it would make the
        # passthrough an expansion, which is the defect it exists to prevent.
        raw=source,
    )


__all__ = [
    "MARKER",
    "Truncator",
    "CompressionResult",
    "coerce_result",
    "shrink_or_passthrough",
    "clip",
    "clip_multiline",
    "take",
    "squeeze",
    "relativise",
    "estimate_tokens",
    "finalise",
]
