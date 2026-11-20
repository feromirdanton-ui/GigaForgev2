"""Message-shape rules for GigaChat — the provider's hard constraints.

GigaChat enforces two things that a generic chat harness gets wrong:

1. **A system message must be the FIRST message.**  A ``SystemMessage`` that
   appears anywhere else in the conversation is rejected with a hard HTTP 400.
   So a harness that appends "nudges" (loop-breaker hints, tool contracts,
   specification audits) as system messages breaks the run.
2. **Nudges therefore travel as ``HumanMessage``.**  The model still follows
   them — they are instructions in the user turn — and the request shape stays
   legal.

This module is the single place that enforces both.  Every provider call goes
through :func:`normalize_messages`, so no caller can accidentally produce an
illegal shape, and :func:`nudge` is the only sanctioned way to build a
mid-conversation instruction.

The functions are pure and dependency-free (plain dicts, the wire shape), which
is what makes them testable without a network or a model.
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "SYSTEM_ROLE",
    "HUMAN_ROLE",
    "ASSISTANT_ROLE",
    "TOOL_ROLE",
    "normalize_messages",
    "nudge",
    "is_nudge",
    "NUDGE_MARKER",
]

SYSTEM_ROLE = "system"
HUMAN_ROLE = "user"
ASSISTANT_ROLE = "assistant"
TOOL_ROLE = "tool"

#: Prefix every mid-conversation instruction carries, so a nudge is
#: recognisable in a transcript and can be counted (bounded re-injection).
NUDGE_MARKER = "[NUDGE]"

_ROLE_ALIASES = {
    "system": SYSTEM_ROLE,
    "sys": SYSTEM_ROLE,
    "developer": SYSTEM_ROLE,
    "user": HUMAN_ROLE,
    "human": HUMAN_ROLE,
    "assistant": ASSISTANT_ROLE,
    "ai": ASSISTANT_ROLE,
    "model": ASSISTANT_ROLE,
    "tool": TOOL_ROLE,
    "function": TOOL_ROLE,
}


def _role_of(message: Any) -> str:
    if isinstance(message, Mapping):
        raw = message.get("role", HUMAN_ROLE)
    else:
        raw = getattr(message, "role", HUMAN_ROLE)
    return _ROLE_ALIASES.get(str(raw).strip().lower(), HUMAN_ROLE)


def _content_of(message: Any) -> str:
    if isinstance(message, Mapping):
        content = message.get("content", "")
    else:
        content = getattr(message, "content", "")
    if content is None:
        return ""
    return content if isinstance(content, str) else str(content)


def _as_dict(message: Any) -> dict[str, Any]:
    if isinstance(message, Mapping):
        out = dict(message)
        out["role"] = _role_of(message)
        out["content"] = _content_of(message)
        return out
    return {"role": _role_of(message), "content": _content_of(message)}


def normalize_messages(messages: Iterable[Any]) -> list[dict[str, Any]]:
    """Return a wire-legal message list for GigaChat.

    * every message is coerced to ``{"role", "content", ...}``;
    * **all** system messages are hoisted to the front and merged into one
      (GigaChat accepts exactly one leading system message; a later one is a
      hard 400);
    * the relative order of the remaining messages is preserved.

    An empty input returns an empty list (the caller decides whether that is an
    error); a list with no system message is returned unchanged in shape.
    """
    coerced = [_as_dict(m) for m in messages]
    if not coerced:
        return []

    system_parts: list[str] = []
    rest: list[dict[str, Any]] = []

    for message in coerced:
        if message["role"] == SYSTEM_ROLE:
            # GigaChat accepts exactly ONE system message and only in first
            # position. Hoist every system message to the front and merge them,
            # so no instruction is lost and the request stays legal.
            if message["content"].strip():
                system_parts.append(message["content"])
            continue
        rest.append(message)

    out: list[dict[str, Any]] = []
    if system_parts:
        out.append({"role": SYSTEM_ROLE, "content": "\n\n".join(system_parts)})
    out.extend(rest)
    return out


def nudge(text: str, *, marker: str = "") -> dict[str, Any]:
    """Build a mid-conversation instruction as a **human** message.

    ``marker`` is an optional signature tag (``[LOOP-BREAKER-ERROR]`` and
    friends) placed on its own line so the caller can count how many times a
    given nudge was already injected.
    """
    body = (text or "").strip()
    head = NUDGE_MARKER
    if marker:
        head = f"{head} {marker.strip()}"
    return {"role": HUMAN_ROLE, "content": f"{head}\n{body}".strip()}


def is_nudge(message: Any, marker: str = "") -> bool:
    """True when *message* is a nudge (optionally of a specific *marker*)."""
    if _role_of(message) != HUMAN_ROLE:
        return False
    content = _content_of(message)
    if NUDGE_MARKER not in content:
        return False
    return marker in content if marker else True


def count_nudges(messages: Sequence[Any], marker: str = "") -> int:
    """How many nudges (of *marker*) the history already contains."""
    return sum(1 for message in messages if is_nudge(message, marker))
