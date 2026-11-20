"""Core error taxonomy (M4–M5).

Every failure carries a stable ``GF-CORE-<AREA>-<NNN>`` code plus a
``retryable`` flag, matching the ``client``/``l0``/``firewall`` convention so
the DAG, the circuit breaker and the metrics layer can branch on behaviour
without ever string-matching a message.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional


class CoreError(Exception):
    """Base class for every orchestration-layer failure."""

    code: str = "GF-CORE-000"
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


# ── configuration ────────────────────────────────────────────────────────────
class CoreConfigError(CoreError):
    """Environment / config is unusable (missing secret, bad value)."""

    code = "GF-CORE-CFG-001"


# ── model routing ────────────────────────────────────────────────────────────
class RoutingError(CoreError):
    code = "GF-CORE-RT-010"


class UnknownPhaseError(RoutingError):
    """A request named a phase that is not declared in the config."""

    code = "GF-CORE-RT-011"


class NoModelForPhaseError(RoutingError):
    """The phase has no candidate model left (all excluded / unknown)."""

    code = "GF-CORE-RT-012"


class PhaseBudgetExceeded(RoutingError):
    """The phase burned its token (or call) budget.

    Non-retryable on purpose: retrying just burns more budget.  The caller
    should escalate to a cheaper phase or fail the task.
    """

    code = "GF-CORE-RT-013"
    retryable = False


# ── DAG execution ────────────────────────────────────────────────────────────
class DagError(CoreError):
    code = "GF-CORE-DAG-020"


class DuplicateNodeError(DagError):
    code = "GF-CORE-DAG-021"


class DuplicateArtifactError(DagError):
    """Multiple nodes claim to own the same artifact key."""

    code = "GF-CORE-DAG-028"


class CycleError(DagError):
    """The declared graph is not a DAG."""

    code = "GF-CORE-DAG-022"


class UnknownDependencyError(DagError):
    code = "GF-CORE-DAG-023"


class MissingInputError(DagError):
    """A node ran without an artifact it declared in ``requires``."""

    code = "GF-CORE-DAG-024"


class NodeOutputError(DagError):
    """A node's returned artifact keys disagree with its declaration."""

    code = "GF-CORE-DAG-029"


class NodeTimeoutError(DagError):
    """A node exceeded its wall-clock budget."""

    code = "GF-CORE-DAG-025"
    # A timed out worker may still be inside a blocking call. Retrying could
    # duplicate its side effects, so timeouts are deliberately terminal.
    retryable = False


class RunCancelledError(DagError):
    """A run or node was asked to stop cooperatively."""

    code = "GF-CORE-DAG-027"
    retryable = False


class NodeFailedError(DagError):
    """A node exhausted its retries."""

    code = "GF-CORE-DAG-026"


# ── circuit breaker ──────────────────────────────────────────────────────────
class BreakerOpenError(CoreError):
    """The breaker is open; the call was rejected without trying."""

    code = "GF-CORE-BRK-030"
    retryable = False


# ── cache ────────────────────────────────────────────────────────────────────
class CacheError(CoreError):
    code = "GF-CORE-CACHE-040"


class CacheMissError(CacheError):
    """Requested a cached value that is absent or expired."""

    code = "GF-CORE-CACHE-041"


# ── fix loop / sub-agents ────────────────────────────────────────────────────
class FixLoopExhausted(CoreError):
    """The task was not fixed inside the iteration budget."""

    code = "GF-CORE-FIX-050"


class SubAgentError(CoreError):
    code = "GF-CORE-AGENT-060"


class WorktreeError(SubAgentError):
    """git worktree could not be created, entered or removed."""

    code = "GF-CORE-AGENT-061"


class PipelineError(CoreError):
    code = "GF-CORE-PIPE-070"


__all__ = [
    "CoreError",
    "CoreConfigError",
    "RoutingError",
    "UnknownPhaseError",
    "NoModelForPhaseError",
    "PhaseBudgetExceeded",
    "DagError",
    "DuplicateNodeError",
    "DuplicateArtifactError",
    "CycleError",
    "UnknownDependencyError",
    "MissingInputError",
    "NodeOutputError",
    "NodeTimeoutError",
    "RunCancelledError",
    "NodeFailedError",
    "BreakerOpenError",
    "CacheError",
    "CacheMissError",
    "FixLoopExhausted",
    "SubAgentError",
    "WorktreeError",
    "PipelineError",
]
