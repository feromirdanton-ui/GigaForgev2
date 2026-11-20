"""Base protocol and shared helpers for Firewall compressors.

Every compressor transforms a raw tool-output string into a small structural
JSON dict. The dict MUST satisfy ``estimate_tokens(json.dumps(result)) <
limit_tokens`` — this is asserted in tests and enforced at the end of every
``compress`` call via :func:`enforce_token_budget`.

Contracts:
- Each string field is individually capped with :func:`truncate_for_tokens` and
  marked with a literal ``[truncated]`` suffix when cut.
- Lists are capped to a max count; when items are dropped, the compressor MUST
  emit a ``"_truncated": <dropped_count>`` sibling key so the LLM can see that
  truncation happened.
- Never raise on malformed input. On parse failure return
  ``{"error": "<summary>", "raw_truncated": truncate_for_tokens(raw, 500)}``.
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from gigaforge.firewall.tokens import estimate_tokens, truncate_for_tokens

__all__ = [
    "Compressor",
    "cap_list",
    "cap_str",
    "enforce_token_budget",
    "estimate_tokens",
    "fail_dict",
    "truncate_for_tokens",
]


@runtime_checkable
class Compressor(Protocol):
    """A Firewall compressor.

    Implementations take raw tool output and return a small JSON-serializable
    dict whose serialized token cost is below ``limit_tokens``.
    """

    def compress(self, raw: str, *, limit_tokens: int = 4000) -> dict[str, Any]:  # noqa: D401
        """Compress *raw* tool output into a structural JSON dict."""
        ...


def cap_str(s: str | None, limit_tokens: int) -> str:
    """Truncate a single string field and mark with ``[truncated]`` if cut."""
    if s is None:
        return ""
    return truncate_for_tokens(s, limit_tokens)


def cap_list(items: list[Any], max_count: int) -> tuple[list[Any], int]:
    """Slice *items* to ``max_count``; return (kept, dropped_count).

    ``dropped_count`` is 0 when nothing was dropped. Callers should emit it as
    ``"_truncated": dropped_count`` ONLY when non-zero.
    """
    if max_count < 0:
        max_count = 0
    if len(items) <= max_count:
        return items, 0
    return items[:max_count], len(items) - max_count


def enforce_token_budget(result: dict[str, Any], limit_tokens: int) -> dict[str, Any]:
    """Last-resort guard: if serialized result still exceeds budget, shrink.

    Compressors should already produce well-sized output, but if a caller passes
    a tiny ``limit_tokens`` we trim the largest string field(s) so the contract
    ``estimate_tokens(json.dumps(result)) < limit_tokens`` always holds.
    """
    import json

    while estimate_tokens(json.dumps(result, ensure_ascii=False)) >= limit_tokens:
        # Find the largest string field and halve its token allowance.
        largest_key: str | None = None
        largest_len = 0
        for k, v in result.items():
            if isinstance(v, str) and len(v) > largest_len:
                largest_len = len(v)
                largest_key = k
        if largest_key is None:
            # No string left to shrink. Drop the largest list, if any.
            list_key = None
            list_len = 0
            for k, v in result.items():
                if isinstance(v, list) and len(v) > list_len:
                    list_len = len(v)
                    list_key = k
            if list_key is None or list_len <= 1:
                # Nothing else to do — drop every optional key.
                for k in list(result.keys()):
                    if k not in {"error", "raw_truncated"}:
                        result.pop(k, None)
                        break
                else:
                    break
            else:
                kept = result[list_key][: max(1, len(result[list_key]) // 2)]
                dropped = len(result[list_key]) - len(kept)
                result[list_key] = kept
                if dropped:
                    result["_truncated"] = result.get("_truncated", 0) + dropped
            continue
        old = result[largest_key]
        new_limit = max(1, len(old) // 8 // 4)  # tokens
        result[largest_key] = truncate_for_tokens(old, new_limit)
    return result


def fail_dict(summary: str, raw: str, *, raw_limit_tokens: int = 500) -> dict[str, Any]:
    """Standard fallback for unparseable input — bounded, never raises."""
    return {
        "error": truncate_for_tokens(summary, 200),
        "raw_truncated": truncate_for_tokens(raw, raw_limit_tokens),
    }
