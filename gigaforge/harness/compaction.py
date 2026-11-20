"""Trajectory compaction — what the context guard was always missing.

``ContextWindowGuard`` detects that a request is about to overflow and raises
``ContextOverflowError``.  Both engines then **stop the run** with
``finish_reason="context_overflow"``.  That is detection without recovery: a
long ReAct episode dies at 80% of the window even though most of its trajectory
is stale observation text nobody needs any more.

This module is the recovery half.  It rewrites a trajectory in place so the
request fits, preserving what the model actually needs to continue:

* the **system message** (GigaChat rejects a system message that is not first
  with a hard 400, so it is never moved or dropped);
* the **goal** — the first user message;
* the **last N turns** verbatim — the model is mid-thought and needs its own
  recent reasoning intact;
* a **compressed digest** of everything in between: each dropped tool
  observation is replaced by its first line plus a size marker, and each
  dropped assistant thought by its first sentence.

The digest is deliberately lossy and cheap: it costs no model call.  A
summariser-based compaction is available separately (``SummarizerAgent``) for
callers that can afford one; this one must work inside a budget that is already
under pressure, so it is pure text processing.

Invariants this module guarantees (and the tests pin):

1. The result is **strictly smaller** than the input, or the input is returned
   unchanged — never a "compaction" that grows the request.
2. The system message stays first, and the goal stays second.
3. The last ``keep_tail`` messages are byte-identical.
4. Compaction is **idempotent**: compacting an already-compact trajectory is a
   no-op, so a retry loop cannot oscillate.
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from gigaforge.firewall.tokens import estimate_tokens
from gigaforge.providers.messages import normalize_messages

__all__ = [
    "COMPACTION_MARKER",
    "compact_trajectory",
    "compaction_fits",
]

#: Prefix of the synthetic message that replaces the dropped middle.  A marker
#: (rather than a bare summary) lets a later pass recognise its own output and
#: stay idempotent.
COMPACTION_MARKER = "[compacted]"

#: How many trailing messages are always kept verbatim.
DEFAULT_KEEP_TAIL = 6

#: Characters of a dropped message that survive into the digest.
DIGEST_CHARS = 160


def _first_line(text: str, limit: int = DIGEST_CHARS) -> str:
    """First non-empty line of *text*, cut to *limit* characters."""
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:limit]
    return ""


def _digest(messages: Sequence[dict[str, Any]]) -> str:
    """One compact message standing in for the dropped middle of a trajectory."""
    lines: list[str] = [
        f"{COMPACTION_MARKER} {len(messages)} earlier messages were dropped to fit "
        "the context window. Their gist:"
    ]
    for message in messages:
        role = str(message.get("role", "") or "")
        content = str(message.get("content", "") or "")
        if not content.strip():
            continue
        head = _first_line(content)
        if not head:
            continue
        tokens = estimate_tokens(content)
        lines.append(f"- {role}: {head} (~{tokens} tok)")
    return "\n".join(lines)


def compact_trajectory(
    messages: Sequence[Any],
    *,
    keep_tail: int = DEFAULT_KEEP_TAIL,
    target_tokens: int | None = None,
) -> list[dict[str, Any]]:
    """Return a compacted copy of *messages*.

    ``target_tokens`` is advisory: when given, the tail is shortened until the
    estimate fits, down to a floor of two messages.  When the trajectory is
    already short enough to compact nothing, the original list is returned
    (as a fresh list, so the caller can mutate it safely).
    """
    normalized = normalize_messages(messages)
    if len(normalized) <= 2:
        return normalized

    # Split: [system?] + [goal] + middle + tail
    head: list[dict[str, Any]] = []
    rest = list(normalized)
    if rest and str(rest[0].get("role", "")) == "system":
        head.append(rest.pop(0))
    if rest:
        head.append(rest.pop(0))  # the goal

    keep = max(2, int(keep_tail))
    if len(rest) <= keep:
        # Nothing worth dropping: the tail already covers everything.
        return normalized

    tail = rest[-keep:]
    middle = rest[:-keep]
    if not middle:
        return normalized

    # Idempotency: a middle that is *already* nothing but compaction digests
    # has nothing left to drop.  Re-digesting it would replace a useful digest
    # with a digest of the digest — smaller, but strictly less informative, and
    # it would make a retry loop shrink the history on every pass.
    existing = [m for m in middle if COMPACTION_MARKER in str(m.get("content", ""))]
    fresh = [m for m in middle if COMPACTION_MARKER not in str(m.get("content", ""))]
    if not fresh:
        return normalized

    digest_parts = [str(m.get("content", "")) for m in existing]
    digest_parts.append(_digest(fresh))
    digest = {"role": "user", "content": "\n".join(part for part in digest_parts if part)}
    compacted = head + [digest] + tail

    # Advisory target: shrink the tail if the digest alone did not buy enough.
    if target_tokens is not None:
        while (
            len(tail) > 2
            and sum(estimate_tokens(str(m.get("content", ""))) for m in compacted) > target_tokens
        ):
            tail = tail[1:]
            compacted = head + [digest] + tail

    # Invariant 1: never return something bigger than the input.
    before = sum(estimate_tokens(str(m.get("content", ""))) for m in normalized)
    after = sum(estimate_tokens(str(m.get("content", ""))) for m in compacted)
    if after >= before:
        return normalized
    return compacted


def compaction_fits(
    messages: Sequence[Any],
    *,
    limit_tokens: int,
    keep_tail: int = DEFAULT_KEEP_TAIL,
) -> bool:
    """True when a compaction of *messages* would land under *limit_tokens*.

    Lets a caller decide *before* spending a request whether recovery is even
    possible, instead of compacting and discovering it was not enough.
    """
    compacted = compact_trajectory(messages, keep_tail=keep_tail, target_tokens=limit_tokens)
    total = sum(estimate_tokens(str(m.get("content", ""))) for m in compacted)
    return total < limit_tokens
