"""Stable error taxonomy for the IGAFORGE client layer.

Every failure raised by :mod:`gigaforge.client` is a :class:`GigaForgeError`
carrying a stable ``code`` (``GF-*``). That code is what lands in structured logs,
in the ``client.error.<code>`` metric and in the SQLite ``errors`` table, so an
operator can join all three without ever parsing a message string.

``retryable`` is the contract the retry loop reads: an error that sets it to
``True`` is a candidate for another attempt, bounded by ``max_retries`` and - for
429s - by the provider's ``Retry-After`` hint.
"""
from __future__ import annotations

from typing import Any, Dict, Optional


class GigaForgeError(Exception):
    """Base class for every client failure."""

    code: str = "GF-UNKNOWN"
    retryable: bool = False

    def __init__(
        self,
        message: str,
        *,
        status: Optional[int] = None,
        payload: Any = None,
        retryable: Optional[bool] = None,
        retry_after: Optional[float] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.payload = payload
        self.retry_after = retry_after
        if retryable is not None:
            self.retryable = retryable

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "retryable": bool(self.retryable),
        }
        if self.status is not None:
            out["status"] = self.status
        if self.retry_after is not None:
            out["retry_after"] = self.retry_after
        if self.payload is not None:
            out["payload"] = self.payload
        return out

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"{type(self).__name__}(code={self.code!r}, message={self.message!r})"


class ConfigurationError(GigaForgeError):
    """The client was constructed with an unusable configuration."""

    code = "GF-CONFIG"


class AuthError(GigaForgeError):
    """OAuth exchange rejected, or an authenticated call answered 401/403."""

    code = "GF-AUTH-401"
    retryable = True


class RateLimitError(GigaForgeError):
    """Provider throttling (429). ``retry_after`` mirrors the ``Retry-After`` header."""

    code = "GF-RATE-429"
    retryable = True


class RequestError(GigaForgeError):
    """Caller-side / validation failure (4xx other than 401 and 429)."""

    code = "GF-REQUEST-4XX"


class ServerError(GigaForgeError):
    """Provider-side failure (5xx) - always worth another attempt."""

    code = "GF-SERVER-5XX"
    retryable = True


class NetworkError(GigaForgeError):
    """Transport-level failure before or while reading a response."""

    code = "GF-NETWORK"
    retryable = True


class RequestTimeout(GigaForgeError):
    """Connect / read / stream timeout."""

    code = "GF-TIMEOUT"
    retryable = True


class StreamProtocolError(GigaForgeError):
    """The SSE stream did not follow the chat-completions protocol."""

    code = "GF-STREAM-PROTO"


class ToolCallError(GigaForgeError):
    """A function call could not be built, parsed or recovered."""

    code = "GF-FUNC-CALL"


_TIMEOUT_NAMES = frozenset(
    {"Timeout", "ReadTimeout", "ConnectTimeout", "TimeoutError", "RequestTimeout"}
)
_NETWORK_NAMES = frozenset(
    {
        "ConnectionError",
        "ConnectTimeoutError",
        "SSLError",
        "ProxyError",
        "ChunkedEncodingError",
        "ProtocolError",
        "RemoteDisconnected",
        "NewConnectionError",
        "MaxRetryError",
        "IncompleteRead",
        "ContentDecodingError",
    }
)


def error_for_status(
    status: int,
    message: str,
    *,
    payload: Any = None,
    retry_after: Optional[float] = None,
) -> GigaForgeError:
    """Map an HTTP status code onto the taxonomy."""
    if status == 401:
        return AuthError(message, status=status, payload=payload)
    if status == 403:
        return AuthError(message, status=status, payload=payload, retryable=False)
    if status == 429:
        return RateLimitError(message, status=status, payload=payload, retry_after=retry_after)
    if 500 <= status <= 599:
        return ServerError(message, status=status, payload=payload)
    return RequestError(message, status=status, payload=payload)


def error_for_exception(exc: BaseException) -> GigaForgeError:
    """Map an arbitrary transport exception onto the taxonomy.

    Type names are used instead of an ``isinstance`` check against ``requests``
    so the mapping also works with any duck-typed transport (and in tests).
    """
    if isinstance(exc, GigaForgeError):
        return exc
    name = type(exc).__name__
    if name in _TIMEOUT_NAMES:
        return RequestTimeout(str(exc) or name, retryable=True)
    if name in _NETWORK_NAMES:
        return NetworkError(str(exc) or name, retryable=True)
    return GigaForgeError(str(exc) or name, retryable=False)


# ── Backwards-compatible aliases for agents & pipeline ───────────────────────
class UpstreamError(GigaForgeError):
    """5xx / network / non-retryable status from upstream."""
    code = "GF-UPSTREAM-5XX"

    def __init__(self, status: Optional[int] = None, message: str = "") -> None:
        super().__init__(message or f"Upstream error: {status}", status=status)


class FunctionCallMissing(GigaForgeError):
    """Model returned text instead of the required function_call."""
    code = "GF-FC-MISSING"


class BudgetExceeded(GigaForgeError):
    """A phase/task token budget was exceeded — hard stop."""
    code = "GF-BUDGET-EXCEEDED"


class CircuitBreakerOpen(GigaForgeError):
    """Too many consecutive failed steps; escalate to human."""
    code = "GF-BREAKER-OPEN"


class DiffRejected(GigaForgeError):
    """Diff was rejected by the validator."""
    code = "GF-DIFF-REJECTED"

    def __init__(self, reason: str = "", raw: str = "") -> None:
        super().__init__(reason or "Diff rejected")
        self.reason = reason
        self.raw = raw
