"""Shared token-estimation helpers for the Firewall.

The Firewall sits between every tool output and every LLM call. We never let
raw tool output above a token threshold reach the model, so we need a cheap,
deterministic estimate. The GigaChat tokenizer is server-side and not exposed;
the chars/4 heuristic matches the OpenAI/GigaChat empirical average for code
and prose well enough for budget enforcement. Real usage is recorded in metrics.
"""
from __future__ import annotations


def estimate_tokens(text: str) -> int:
    """Cheap token estimate: chars//4, never zero for non-empty input.

    >>> estimate_tokens("")
    0
    >>> estimate_tokens("hello world")  # 11 chars -> 2 tokens
    2
    """
    if not text:
        return 0
    return max(1, len(text) // 4)


def truncate_for_tokens(text: str, limit_tokens: int) -> str:
    """Cut *text* so its estimated token count is at most ``limit_tokens``.

    If a cut happens, append the literal marker ``\\n[truncated]`` so downstream
    LLM consumers see an explicit truncation signal. ``limit_tokens <= 0`` returns
    an empty string.
    """
    if limit_tokens <= 0:
        return ""
    if not text:
        return ""
    char_limit = limit_tokens * 4
    if len(text) <= char_limit:
        return text
    marker = "\n[truncated]"
    keep = max(0, char_limit - len(marker))
    return text[:keep] + marker
