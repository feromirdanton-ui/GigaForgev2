"""Circuit breaker for external calls (M4–M5).

Three states, the standard semantics:

* **closed** — calls pass through; consecutive failures are counted.
* **open** — after ``failure_threshold`` consecutive failures (default **3**)
  every call is rejected immediately with :class:`BreakerOpenError`; no time is
  wasted on a dependency that is clearly down.
* **half-open** — once ``reset_timeout`` has elapsed, a limited number of probe
  calls are allowed through; ``success_threshold`` consecutive successes close
  the breaker, a single failure re-opens it.

Consecutive-ness is the important part: total failure counts would let a service
that has been healthy for hours be marked down by three unrelated blips spread
across that window.  Only *uninterrupted* failure trips the breaker.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional, TypeVar

from .config import BreakerConfig
from .errors import BreakerOpenError

T = TypeVar("T")


class BreakerState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class BreakerStats:
    calls: int = 0
    successes: int = 0
    failures: int = 0
    rejected: int = 0
    trips: int = 0
    resets: int = 0
    half_open_probes: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "successes": self.successes,
            "failures": self.failures,
            "rejected": self.rejected,
            "trips": self.trips,
            "resets": self.resets,
            "half_open_probes": self.half_open_probes,
        }


class CircuitBreaker:
    """Thread-safe circuit breaker around any failure-producing call."""

    def __init__(
        self,
        name: str = "default",
        config: Optional[BreakerConfig] = None,
        *,
        clock: Any = None,
        metrics: Any = None,
        trace: Any = None,
        is_failure: Optional[Callable[[BaseException], bool]] = None,
    ) -> None:
        self.name = name
        self.config = config or BreakerConfig()
        self._clock = clock or time.monotonic
        self.metrics = metrics
        self.trace = trace
        # By default every exception counts.  A caller that knows some errors are
        # "the caller's fault, not the dependency's" (e.g. a 400) can exclude
        # them, so a bad request cannot trip a healthy service's breaker.
        self._is_failure = is_failure or (lambda exc: True)
        self._lock = threading.RLock()
        self._state = BreakerState.CLOSED
        self._consecutive_failures = 0
        self._consecutive_successes = 0
        self._opened_at = 0.0
        self._half_open_inflight = 0
        self._stats = BreakerStats()
        self._last_error: str = ""

    # ── introspection ────────────────────────────────────────────────────────
    @property
    def state(self) -> BreakerState:
        with self._lock:
            self._maybe_half_open()
            return self._state

    @property
    def stats(self) -> BreakerStats:
        return self._stats

    @property
    def is_open(self) -> bool:
        """True when the circuit is fully open.

        Deliberately *not* true in ``HALF_OPEN``: the half-open state exists to
        let a single probe through, so treating it as closed would defeat the
        recovery path.  Callers that must not even probe should check
        :attr:`state` directly.
        """
        return self.state is BreakerState.OPEN

    def failure_count(self) -> int:
        with self._lock:
            return self._consecutive_failures

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            self._maybe_half_open()
            return {
                "name": self.name,
                "state": self._state.value,
                "consecutive_failures": self._consecutive_failures,
                "consecutive_successes": self._consecutive_successes,
                "threshold": self.config.failure_threshold,
                "reset_timeout": self.config.reset_timeout,
                "last_error": self._last_error,
                "stats": self._stats.to_dict(),
            }

    # ── state machine ────────────────────────────────────────────────────────
    def _maybe_half_open(self) -> None:
        if self._state is BreakerState.OPEN and self._open_elapsed() >= self.config.reset_timeout:
            self._state = BreakerState.HALF_OPEN
            self._consecutive_successes = 0
            self._half_open_inflight = 0
            self._emit("breaker.half_open")

    def _open_elapsed(self) -> float:
        return max(0.0, self._clock() - self._opened_at)

    def _trip(self, error: str) -> None:
        self._state = BreakerState.OPEN
        self._opened_at = self._clock()
        self._half_open_inflight = 0
        self._stats.trips += 1
        self._emit("breaker.open", error=error, failures=self._consecutive_failures)

    def _reset(self) -> None:
        self._state = BreakerState.CLOSED
        self._consecutive_failures = 0
        self._consecutive_successes = 0
        self._half_open_inflight = 0
        self._stats.resets += 1
        self._emit("breaker.closed")

    def _emit(self, kind: str, **payload: Any) -> None:
        if self.metrics is not None:
            self.metrics.incr(f"core.{kind}", 1, {"breaker": self.name})
        if self.trace is not None:
            self.trace.event(kind, breaker=self.name, **payload)

    def _allow(self) -> None:
        """Raise if the call must be rejected; otherwise account for the probe."""
        self._maybe_half_open()
        if self._state is BreakerState.CLOSED:
            return
        if self._state is BreakerState.OPEN:
            self._stats.rejected += 1
            wait = max(0.0, self.config.reset_timeout - self._open_elapsed())
            if self.metrics is not None:
                self.metrics.incr("core.breaker.rejected", 1, {"breaker": self.name})
            raise BreakerOpenError(
                f"breaker {self.name!r} is open",
                detail={"retry_in": round(wait, 3), "last_error": self._last_error,
                        "failures": self._consecutive_failures},
            )
        # HALF_OPEN: only a bounded number of probes at a time.
        if self._half_open_inflight >= max(1, self.config.half_open_max_calls):
            self._stats.rejected += 1
            raise BreakerOpenError(
                f"breaker {self.name!r} is half-open and a probe is in flight",
                detail={"inflight": self._half_open_inflight},
            )
        self._half_open_inflight += 1
        self._stats.half_open_probes += 1

    def _on_success(self) -> None:
        with self._lock:
            self._stats.successes += 1
            self._consecutive_failures = 0
            if self._state is BreakerState.HALF_OPEN:
                self._half_open_inflight = max(0, self._half_open_inflight - 1)
                self._consecutive_successes += 1
                if self._consecutive_successes >= max(1, self.config.success_threshold):
                    self._reset()
            else:
                self._consecutive_successes += 1

    def _on_failure(self, exc: BaseException) -> None:
        with self._lock:
            counted = self._is_failure(exc)
            self._stats.failures += 1
            self._last_error = f"[{getattr(exc, 'code', 'ERR')}] {exc}"[:500]
            if not counted:
                # An excluded failure is a caller-side error: re-open nothing, but
                # do not let it count as a success either.
                if self._state is BreakerState.HALF_OPEN:
                    self._half_open_inflight = max(0, self._half_open_inflight - 1)
                self._emit("breaker.ignored_failure", error=str(exc)[:200])
                return
            self._consecutive_successes = 0
            self._consecutive_failures += 1
            if self._state is BreakerState.HALF_OPEN:
                self._half_open_inflight = max(0, self._half_open_inflight - 1)
                self._trip(self._last_error)
                return
            if self._consecutive_failures >= max(1, self.config.failure_threshold):
                self._trip(self._last_error)

    # ── call wrapper ─────────────────────────────────────────────────────────
    def call(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        """Run ``fn`` under the breaker.

        State transitions happen while holding the lock, so two concurrent
        failures cannot both observe the threshold and double-trip.
        """
        with self._lock:
            self._stats.calls += 1
            self._allow()
        try:
            result = fn(*args, **kwargs)
        except BaseException as exc:  # noqa: BLE001 - re-raised
            self._on_failure(exc)
            raise
        self._on_success()
        return result

    def __call__(self, fn: Callable[..., T]) -> Callable[..., T]:
        def wrapper(*args: Any, **kwargs: Any) -> T:
            return self.call(fn, *args, **kwargs)
        return wrapper

    def reset(self) -> None:
        with self._lock:
            self._reset()
            self._stats = BreakerStats()

    def force_open(self) -> None:
        """Mark the breaker open (used by tests and by an operator override)."""
        with self._lock:
            self._consecutive_failures = max(1, self.config.failure_threshold)
            self._trip("forced")


class BreakerRegistry:
    """One breaker per named dependency, created on first use."""

    def __init__(self, config: Optional[BreakerConfig] = None, **shared: Any) -> None:
        self.config = config or BreakerConfig()
        self._shared = shared
        self._lock = threading.Lock()
        self._breakers: dict[str, CircuitBreaker] = {}

    def get(self, name: str) -> CircuitBreaker:
        with self._lock:
            breaker = self._breakers.get(name)
            if breaker is None:
                breaker = CircuitBreaker(name, self.config, **self._shared)
                self._breakers[name] = breaker
            return breaker

    def reconfigure(self, config: BreakerConfig) -> None:
        """Update the policy for this registry and its existing breakers."""
        with self._lock:
            self.config = config
            for breaker in self._breakers.values():
                breaker.config = config

    def all(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {k: v.snapshot() for k, v in self._breakers.items()}


__all__ = ["CircuitBreaker", "BreakerState", "BreakerStats", "BreakerRegistry"]
