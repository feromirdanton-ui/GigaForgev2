"""Context Firewall error taxonomy (M3).

Every failure raised by the firewall carries a stable ``code`` so callers,
metrics and the audit trail can branch on it without string matching.  Codes
follow the ``GF-FW-<AREA>-<NNN>`` convention used across IGAFORGE.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional


class FirewallError(Exception):
    """Base class for every firewall failure."""

    code: str = "GF-FW-000"
    retryable: bool = False

    def __init__(
        self,
        message: str,
        *,
        code: Optional[str] = None,
        retryable: Optional[bool] = None,
        detail: Optional[Mapping[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if retryable is not None:
            self.retryable = retryable
        self.detail: dict[str, Any] = dict(detail or {})

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "detail": self.detail,
        }

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"[{self.code}] {self.message}"


# ── compression ──────────────────────────────────────────────────────────
class CompressionError(FirewallError):
    code = "GF-FW-CMP-010"


class UnsupportedSourceError(CompressionError):
    """The payload does not look like any compressible source kind."""

    code = "GF-FW-CMP-011"


# ── context slice ────────────────────────────────────────────────────────
class SliceError(FirewallError):
    code = "GF-FW-SLICE-020"


class EmptySliceError(SliceError):
    """Nothing in the index matched the task; the slice would be empty."""

    code = "GF-FW-SLICE-021"


# ── diff parsing ─────────────────────────────────────────────────────────
class DiffParseError(FirewallError):
    """The text is not a usable unified diff."""

    code = "GF-FW-DIFF-030"


class DiffApplyError(FirewallError):
    """A hunk could not be located or applied."""

    code = "GF-FW-DIFF-031"


class PathNotAllowedError(DiffApplyError):
    """The diff touches a path outside the allow-list / inside a deny-glob."""

    code = "GF-FW-DIFF-032"


class ContextMismatchError(DiffApplyError):
    """No match strategy could anchor a hunk in the target file."""

    code = "GF-FW-DIFF-033"


class ApplyRollbackError(DiffApplyError):
    """The atomic apply failed AND the restore of a touched file failed."""

    code = "GF-FW-DIFF-034"


# ── validation ───────────────────────────────────────────────────────────
class ValidationError(FirewallError):
    code = "GF-FW-VAL-040"


class RetryBudgetExhausted(ValidationError):
    """The validator loop burned its retries and the diff is still rejected."""

    code = "GF-FW-VAL-041"


__all__ = [
    "FirewallError",
    "CompressionError",
    "UnsupportedSourceError",
    "SliceError",
    "EmptySliceError",
    "DiffParseError",
    "DiffApplyError",
    "PathNotAllowedError",
    "ContextMismatchError",
    "ApplyRollbackError",
    "ValidationError",
    "RetryBudgetExhausted",
]
