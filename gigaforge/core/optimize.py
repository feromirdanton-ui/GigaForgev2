"""Context and prompt optimisation (V2).

Two cheap, deterministic wins that V1 left on the table:

* **Deduplication.**  A context slice assembled from several sources routinely
  repeats the same symbol or the same block.  Sending it twice costs tokens and
  — worse — biases the model toward whatever is duplicated.  V2 collapses
  exact duplicates while preserving first-seen order.
* **Budget trimming.**  A slice that exceeds its token budget is trimmed by
  *dropping whole blocks from the tail*, never by cutting a block in half: a
  half-block is worse than no block, because it looks complete and is not.

Both operations are pure functions over strings, so they are trivially testable
and cannot change a run's outcome except by making the prompt smaller.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Optional, Sequence


def estimate_tokens(text: str, chars_per_token: float = 4.0) -> int:
    if not text:
        return 0
    ratio = chars_per_token if chars_per_token > 0 else 4.0
    return max(1, int(round(len(text) / ratio)))


def dedupe_blocks(blocks: Iterable[str], *, keep_order: bool = True) -> list[str]:
    """Drop exact duplicate blocks, preserving first-seen order."""
    seen: set[str] = set()
    out: list[str] = []
    for block in blocks:
        key = (block or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(block)
    if not keep_order:
        out.sort()
    return out


@dataclass
class TrimResult:
    """Outcome of a budget trim."""

    text: str
    kept: int
    dropped: int
    tokens: int
    truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "kept": self.kept,
            "dropped": self.dropped,
            "tokens": self.tokens,
            "truncated": self.truncated,
            "chars": len(self.text),
        }


class ContextOptimizer:
    """Deduplicate and budget-trim a context slice before it reaches a model."""

    def __init__(self, *, chars_per_token: float = 4.0, separator: str = "\n\n") -> None:
        self.chars_per_token = chars_per_token if chars_per_token > 0 else 4.0
        self.separator = separator

    def optimize(
        self,
        blocks: Sequence[str],
        *,
        budget_tokens: Optional[int] = None,
    ) -> TrimResult:
        """Dedupe, then trim whole blocks from the tail to fit ``budget_tokens``."""
        unique = dedupe_blocks(blocks)
        dropped = len([b for b in blocks if (b or "").strip()]) - len(unique)
        if not budget_tokens or budget_tokens <= 0:
            text = self.separator.join(unique)
            return TrimResult(
                text=text, kept=len(unique), dropped=dropped,
                tokens=estimate_tokens(text, self.chars_per_token),
            )

        kept: list[str] = []
        used = 0
        truncated = False
        for block in unique:
            cost = estimate_tokens(block, self.chars_per_token) + 1
            if used + cost > budget_tokens:
                truncated = True
                break
            kept.append(block)
            used += cost
        text = self.separator.join(kept)
        return TrimResult(
            text=text,
            kept=len(kept),
            dropped=dropped + (len(unique) - len(kept)),
            tokens=estimate_tokens(text, self.chars_per_token),
            truncated=truncated,
        )

    def optimize_text(self, text: str, *, budget_tokens: Optional[int] = None) -> TrimResult:
        """Optimise a pre-joined text by splitting it on blank lines."""
        blocks = [b for b in (text or "").split(self.separator)]
        return self.optimize(blocks, budget_tokens=budget_tokens)


def optimize_context(
    blocks: Sequence[str],
    *,
    budget_tokens: Optional[int] = None,
    chars_per_token: float = 4.0,
) -> TrimResult:
    """Convenience wrapper around :class:`ContextOptimizer`."""
    return ContextOptimizer(chars_per_token=chars_per_token).optimize(
        blocks, budget_tokens=budget_tokens
    )


__all__ = [
    "ContextOptimizer",
    "TrimResult",
    "dedupe_blocks",
    "optimize_context",
    "estimate_tokens",
]
