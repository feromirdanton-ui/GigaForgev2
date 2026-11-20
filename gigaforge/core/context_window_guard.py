"""ContextWindowGuard — keep a GigaChat request inside the model's real window.

Ported from ``ai-forever/deepagents-gigachat``'s
``ContextWindowGuardMiddleware``, re-expressed against GigaForge's own types
(plain message dicts, no LangChain).  The behaviour that made it worth porting:

* **The measured window table**, not a guess.  Resolution goes through
  :mod:`gigaforge.providers.context_windows` (261 120 tokens for every public
  chat model, env-overridable).
* **Proactive compaction at a safe fraction** (85 % by default).  Waiting for
  the provider to reject the request wastes a full round trip and, on a long
  agent loop, a whole step of the budget.
* **Translation of the provider's overflow error** into one typed exception, so
  the caller has a single thing to catch regardless of whether the API answered
  ``422``, ``400`` or a ``RequestEntityTooLargeError``.
* **Retry-loop protection.**  After a compaction the summariser retries the
  model with a synthetic summary; raising a second overflow from that retry
  would escape the caller's catch block.  The guard therefore refuses to fire
  twice on the same compaction.

The guard is deliberately *advisory*: it raises
:class:`ContextOverflowError` and lets the caller decide (compact, drop
history, or fail).  It never mutates the message list itself — a guard that
silently rewrote the prompt would make a run unreproducible.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional, Sequence

from gigaforge.providers.context_windows import (
    DEFAULT_CONTEXT_WINDOW,
    context_window_for,
    normalize_model_id,
)

__all__ = [
    "ContextOverflowError",
    "ContextWindowGuard",
    "DEFAULT_TRIGGER_FRACTION",
    "estimate_message_tokens",
    "is_context_overflow_error",
]

#: Compact once the request reaches this share of the window.
DEFAULT_TRIGGER_FRACTION = 0.85

#: Below this many messages a "conversation" is too short to be worth
#: compacting — the overflow is then a single oversized payload, not history.
DEFAULT_MINIMUM_MESSAGES = 7

#: Substrings the provider uses when it rejects an oversized request.
_CONTEXT_ERROR_TEXT = (
    "context_too_long",
    "context too long",
    "context length",
    "context limit",
    "context window",
    "maximum allowed context",
    "maximum context",
    "payload too large",
    "too many tokens",
    "request entity too large",
)

_CONTEXT_ERROR_CLASSES = {
    "RequestEntityTooLargeError",
    "ContextOverflowError",
}


class ContextOverflowError(RuntimeError):
    """The request does not fit the model's context window.

    Raised either proactively (the guard's own estimate crossed the trigger) or
    reactively (the provider rejected the request and the guard translated the
    error).  ``estimated_tokens`` / ``context_window`` are filled in when known.
    """

    def __init__(
        self,
        message: str,
        *,
        estimated_tokens: Optional[int] = None,
        context_window: Optional[int] = None,
        proactive: bool = False,
    ) -> None:
        super().__init__(message)
        self.estimated_tokens = estimated_tokens
        self.context_window = context_window
        self.proactive = proactive


def estimate_message_tokens(messages: Iterable[Any], *, chars_per_token: float = 4.0) -> int:
    """Cheap, deterministic token estimate for a message list.

    Deliberately not a tokenizer: the guard only needs to know whether the
    request is *near* a 261 120-token ceiling, and a 4-chars-per-token estimate
    is accurate enough for that while costing nothing.  A per-message overhead
    of 4 tokens accounts for the role/separator framing.
    """
    total = 0
    per_token = chars_per_token if chars_per_token > 0 else 4.0
    for message in messages:
        if isinstance(message, Mapping):
            content = message.get("content", "")
        else:
            content = getattr(message, "content", "")
        text = "" if content is None else str(content)
        total += int(len(text) / per_token) + 4
    return total


def is_context_overflow_error(exc: BaseException) -> bool:
    """Does *exc* mean "the request was too large for the model"?"""
    if type(exc).__name__ in _CONTEXT_ERROR_CLASSES:
        return True
    # Only the provider's 4xx shapes are candidates; a 500 is not an overflow.
    if type(exc).__name__ not in {
        "BadRequestError",
        "UnprocessableEntityError",
        "UpstreamError",
        "ResponseError",
    }:
        return False
    text = str(exc).lower()
    return any(marker in text for marker in _CONTEXT_ERROR_TEXT)


@dataclass
class ContextWindowGuard:
    """Decide when a request must be compacted before it is sent.

    ``context_window`` pins one window for every model (used by tests and by a
    deployment that knows its own limit); when ``None`` the window is resolved
    per model from the measured table.
    """

    context_window: Optional[int] = None
    trigger_fraction: float = DEFAULT_TRIGGER_FRACTION
    minimum_messages: int = DEFAULT_MINIMUM_MESSAGES
    chars_per_token: float = 4.0

    def __post_init__(self) -> None:
        if self.context_window is not None and self.context_window <= 0:
            raise ValueError("context_window must be a positive integer")
        if not 0 < self.trigger_fraction < 1:
            raise ValueError("trigger_fraction must be between 0 and 1")
        if self.minimum_messages < 2:
            raise ValueError("minimum_messages must be at least 2")

    # -- window resolution ---------------------------------------------------
    def window_for(self, model: Any = None) -> int:
        if self.context_window is not None:
            return self.context_window
        return context_window_for(model)

    def trigger_tokens_for(self, model: Any = None) -> int:
        return int(self.window_for(model) * self.trigger_fraction)

    # -- the check -----------------------------------------------------------
    def check(
        self,
        messages: Sequence[Any],
        *,
        model: Any = None,
        already_compacted: bool = False,
    ) -> int:
        """Return the estimated token count, raising if compaction is due.

        ``already_compacted=True`` suppresses the proactive raise: the caller
        has just summarised and is retrying, and a second overflow from that
        retry would escape its own handler.
        """
        estimated = estimate_message_tokens(messages, chars_per_token=self.chars_per_token)
        if already_compacted or len(messages) < self.minimum_messages:
            return estimated
        window = self.window_for(model)
        trigger = int(window * self.trigger_fraction)
        if estimated < trigger:
            return estimated
        raise ContextOverflowError(
            "GigaChat context guard requested proactive compaction at "
            f"{estimated} estimated tokens "
            f"({self.trigger_fraction:.0%} of {window}).",
            estimated_tokens=estimated,
            context_window=window,
            proactive=True,
        )

    def translate(self, exc: BaseException, *, model: Any = None) -> BaseException:
        """Map a provider overflow error to :class:`ContextOverflowError`.

        Returns the original exception unchanged when it is not an overflow, so
        a caller can do ``raise guard.translate(exc)`` in one line.
        """
        if isinstance(exc, ContextOverflowError):
            return exc
        if not is_context_overflow_error(exc):
            return exc
        return ContextOverflowError(
            "GigaChat rejected the request because its context window was exceeded.",
            context_window=self.window_for(model),
            proactive=False,
        )

    # -- introspection -------------------------------------------------------
    def describe(self, model: Any = None) -> dict[str, Any]:
        window = self.window_for(model)
        return {
            "model": normalize_model_id(model),
            "context_window": window,
            "trigger_fraction": self.trigger_fraction,
            "trigger_tokens": int(window * self.trigger_fraction),
            "default_window": DEFAULT_CONTEXT_WINDOW,
        }
