"""Orchestration-layer contracts (M4–M5).

These are ``typing.Protocol`` declarations, not implementations: the DAG
executor depends on the shape, and every concrete piece (``GigaClient``,
``L0Store``, ``ContextFirewall``) satisfies it structurally.  That is what lets
the whole core layer be tested with scripted doubles and zero network.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence, runtime_checkable

from .cancellation import CancellationToken


# ── clock ────────────────────────────────────────────────────────────────────
@dataclass
class Clock:
    """Injectable time source.

    Tests substitute a deterministic clock so backoff, TTL expiry, timeouts and
    the breaker's reset window can be exercised without ever sleeping.
    """

    monotonic: Callable[[], float] = time.monotonic
    wall: Callable[[], float] = time.time

    def now(self) -> float:
        return self.monotonic()

    def timestamp(self) -> float:
        return self.wall()


@dataclass
class SystemClock(Clock):
    """Real clock (the default)."""

    def sleep(self, seconds: float) -> None:  # pragma: no cover - real sleep
        if seconds > 0:
            time.sleep(seconds)


class Sleeper(Protocol):
    def sleep(self, seconds: float) -> None: ...


# ── model client ─────────────────────────────────────────────────────────────
@runtime_checkable
class ChatClient(Protocol):
    """What ``ModelRouter`` needs from a model client.

    Deliberately narrower than ``gigaforge.client.GigaClient``: routing only
    needs one completion call, so the router stays usable with a fake.
    """

    def complete(
        self,
        messages: Sequence[Any],
        *,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        **kwargs: Any,
    ) -> Any: ...


# ── node / pipeline ──────────────────────────────────────────────────────────
@dataclass
class NodeSpec:
    """Declarative description of one DAG node.

    ``requires``/``provides`` are the artifact keys, which is how the executor
    both topo-sorts the graph and validates that a node actually received its
    declared inputs instead of silently running on ``None``. Retries are honored
    only for nodes explicitly marked ``idempotent``. The ``parallel_safe`` flag
    is an explicit concurrency promise: shared services must be thread-safe,
    state writes must be disjoint, and the node must not mutate shared input or
    nested extra values or perform order-dependent external side effects.
    """

    name: str
    fn: Callable[["RunContext", Mapping[str, Any]], Mapping[str, Any]]
    requires: tuple[str, ...] = ()
    provides: tuple[str, ...] = ()
    retries: int = 0
    timeout: Optional[float] = None
    idempotent: bool = False
    critical: bool = True          # non-critical failure → warn, continue
    parallel_safe: bool = False    # explicit promise of isolation inside a DAG wave
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "requires": list(self.requires),
            "provides": list(self.provides),
            "retries": self.retries,
            "timeout": self.timeout,
            "idempotent": self.idempotent,
            "critical": self.critical,
            "parallel_safe": self.parallel_safe,
        }


# ── run context ──────────────────────────────────────────────────────────────
@dataclass
class RunContext:
    """Everything a node is allowed to reach, passed explicitly.

    No globals: the context carries the clock, the budget guard, the trace
    writer and the free-form bag nodes use to share state.  A node that needs
    something must find it here, which keeps tests honest.
    """

    run_id: str
    phase: str = ""
    node: str = ""
    attempt: int = 1
    clock: Clock = field(default_factory=SystemClock)
    deadline: Optional[float] = None
    metrics: Any = None
    trace: Any = None
    router: Any = None
    cache: Any = None
    store: Any = None
    firewall: Any = None
    client: Any = None
    state: Any = None
    artifacts: dict[str, Any] = field(default_factory=dict)
    extras: dict[str, Any] = field(default_factory=dict)
    cancellation: CancellationToken = field(default_factory=CancellationToken)
    correlation_id: Optional[str] = None

    def remaining(self) -> Optional[float]:
        if self.deadline is None:
            return None
        return max(0.0, self.deadline - self.clock.now())

    def expired(self) -> bool:
        return self.deadline is not None and self.clock.now() >= self.deadline

    def raise_if_cancelled(self) -> None:
        """Stop at a cooperative boundary when this node or run was cancelled."""
        self.cancellation.raise_if_cancelled()

    def child(self, **kwargs: Any) -> "RunContext":
        """A shallow copy for a node / sub-agent, with overrides applied."""
        data = {
            "run_id": self.run_id,
            "phase": self.phase,
            "node": self.node,
            "attempt": self.attempt,
            "clock": self.clock,
            "deadline": self.deadline,
            "metrics": self.metrics,
            "trace": self.trace,
            "router": self.router,
            "cache": self.cache,
            "store": self.store,
            "firewall": self.firewall,
            "client": self.client,
            "state": self.state,
            "artifacts": dict(self.artifacts),
            "extras": dict(self.extras),
            "cancellation": self.cancellation,
            "correlation_id": self.correlation_id,
        }
        data.update(kwargs)
        return RunContext(**data)


# ── proposal / validation ────────────────────────────────────────────────────
@dataclass
class ProposeResult:
    """What a model proposes when asked to satisfy a task."""

    text: str = ""
    diff: str = ""
    usage: Any = None
    model: str = ""
    phase: str = ""
    cached: bool = False
    attempts: int = 1
    latency_ms: float = 0.0
    raw: Any = None

    def to_dict(self) -> dict[str, Any]:
        usage = self.usage
        return {
            "text_chars": len(self.text or ""),
            "diff_chars": len(self.diff or ""),
            "model": self.model,
            "phase": self.phase,
            "cached": self.cached,
            "attempts": self.attempts,
            "latency_ms": round(self.latency_ms, 3),
            "usage": (
                usage.to_dict() if hasattr(usage, "to_dict") else
                (usage if isinstance(usage, dict) else None)
            ),
        }


@dataclass
class ValidateResult:
    """Normalised view over the firewall's ``ValidationOutcome``."""

    accepted: bool = False
    applied: bool = False
    retries_used: int = 0
    changed_files: list[str] = field(default_factory=list)
    reason: str = ""
    repair_hint: str = ""
    raw: Any = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "applied": self.applied,
            "retries_used": self.retries_used,
            "changed_files": list(self.changed_files),
            "reason": self.reason,
        }


@runtime_checkable
class LLMRouter(Protocol):
    def complete(
        self,
        phase: str,
        messages: Sequence[Any],
        *,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        ctx: Optional[RunContext] = None,
        **kwargs: Any,
    ) -> Any: ...


# ── sub-agents ───────────────────────────────────────────────────────────────
@dataclass
class SubAgentSpec:
    """One sub-agent's assignment."""

    name: str
    goal: str
    phase: str = "code"
    focus_paths: tuple[str, ...] = ()
    symbols: tuple[str, ...] = ()
    budget_tokens: int = 0
    # Execution semantics travel with the task spec. Context extras are isolated
    # deliberately, so run controls must not depend on inheriting that bag.
    use_cache: bool = True
    dry_run: bool = False
    max_fix_iterations: Optional[int] = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class SubAgentResult:
    """What a sub-agent returns, plus the isolation evidence."""

    name: str
    spawn_index: int
    ok: bool
    diff: str = ""
    summary: str = ""
    usage: Any = None
    worktree: str = ""
    branch: str = ""
    validate: Optional[ValidateResult] = None
    error: str = ""
    metrics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "spawn_index": self.spawn_index,
            "ok": self.ok,
            "diff_chars": len(self.diff or ""),
            "summary": self.summary,
            "worktree": self.worktree,
            "branch": self.branch,
            "validate": self.validate.to_dict() if self.validate else None,
            "error": self.error,
        }


__all__ = [
    "Clock",
    "SystemClock",
    "Sleeper",
    "ChatClient",
    "LLMRouter",
    "NodeSpec",
    "RunContext",
    "CancellationToken",
    "ProposeResult",
    "ValidateResult",
    "SubAgentSpec",
    "SubAgentResult",
]
