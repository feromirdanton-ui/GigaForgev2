"""Client-side token-bucket rate limiting for RPM and TPM.

The provider counts two things we can measure locally: requests per minute and
tokens per minute. Both are classic token buckets. The subtlety is *which* token
count to reserve: the exact completion size is only known after the call, so the
limiter lets the client reserve an estimate up front and then **reconcile** with the
real ``usage`` afterwards.

Implements deficit scheduling rather than lock-and-sleep: a caller that has to wait
is still handed its slot immediately (the bucket is allowed to go negative), so
concurrent callers queue in the right order without a thundering herd waking up
together.
"""
from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional


class TokenBucket:
    """A refilling bucket measured in abstract *units*.

    ``rate`` is units per second; ``capacity`` is how much may burst at once. The
    balance is allowed to go negative so callers can be *scheduled* rather than
    blocked while holding a lock.
    """

    __slots__ = ("rate", "capacity", "_balance", "_updated", "_clock")

    def __init__(self, rate: float, capacity: float, *, clock: Callable[[], float] = time.monotonic,
                 initial: Optional[float] = None) -> None:
        self.rate = max(0.0, float(rate))
        self.capacity = max(0.0, float(capacity))
        self._clock = clock
        self._balance = self.capacity if initial is None else float(initial)
        self._updated = clock()

    @property
    def balance(self) -> float:
        self._refill()
        return self._balance

    def _refill(self) -> None:
        now = self._clock()
        elapsed = now - self._updated
        if elapsed <= 0:
            return
        self._updated = now
        if self.rate > 0:
            self._balance = min(self.capacity, self._balance + elapsed * self.rate)
        elif self._balance < 0:  # no refill configured but a debt exists
            self._balance = 0.0

    def wait_time(self, units: float) -> float:
        """Seconds until ``units`` would be available at the current rate."""
        self._refill()
        if units <= 0 or self.rate <= 0:
            return 0.0
        deficit = units - self._balance
        if deficit <= 0:
            return 0.0
        return deficit / self.rate

    def consume(self, units: float) -> None:
        """Deduct ``units`` now (the balance may go negative by design)."""
        self._refill()
        if units > 0:
            self._balance -= units

    def refund(self, units: float) -> None:
        """Return reserved-but-unused units, capped at capacity."""
        self._refill()
        if units > 0:
            self._balance = min(self.capacity, self._balance + units)

    def drain(self) -> None:
        self._balance = min(self._balance, 0.0)

    def reset(self) -> None:
        self._updated = self._clock()
        self._balance = self.capacity


@dataclass
class Reservation:
    """The ticket returned by :meth:`RateLimiter.reserve`."""

    wait_seconds: float = 0.0
    estimated_tokens: int = 0
    request_id: Optional[str] = None

    @property
    def waited(self) -> bool:
        return self.wait_seconds > 0


@dataclass
class RateLimiter:
    """Combined request + token bucket limiter with 429 penalty support."""

    rpm: int = 0
    tpm: int = 0
    burst: float = 1.0
    enforce: bool = True
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    _requests: Optional[TokenBucket] = field(default=None, init=False, repr=False)
    _tokens: Optional[TokenBucket] = field(default=None, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _blocked_until: float = field(default=0.0, init=False, repr=False)

    def __post_init__(self) -> None:
        burst = max(0.1, float(self.burst))
        if self.rpm and self.rpm > 0:
            per_second = self.rpm / 60.0
            self._requests = TokenBucket(per_second, max(1.0, per_second * burst), clock=self.clock)
        if self.tpm and self.tpm > 0:
            per_second = self.tpm / 60.0
            self._tokens = TokenBucket(per_second, max(1.0, per_second * burst), clock=self.clock)

    @property
    def enabled(self) -> bool:
        return self._requests is not None or self._tokens is not None

    # -- reservation -----------------------------------------------------------
    def reserve(self, estimated_tokens: int = 0, *, block: bool = True) -> Reservation:
        """Claim a slot. Returns the wait the caller must observe before sending."""
        units = max(0, int(estimated_tokens))
        with self._lock:
            # A 429 penalty must be honoured even when no bucket is configured, so
            # the blocked window is checked before the (optional) buckets.
            now = self.clock()
            wait = 0.0
            if now < self._blocked_until:
                wait = self._blocked_until - now
            if self._requests is not None:
                wait = max(wait, self._requests.wait_time(1))
                self._requests.consume(1)
            if self._tokens is not None and units > 0:
                wait = max(wait, self._tokens.wait_time(units))
                self._tokens.consume(units)
        if wait > 0 and block:
            self.sleep(wait)
        return Reservation(wait_seconds=wait, estimated_tokens=units)

    # -- async facade for the async client -------------------------------------
    async def acquire(self, estimated_tokens: int = 0, *, block: bool = True) -> Reservation:
        """Async face of :meth:`reserve`.

        ``reserve`` sleeps on the calling thread, which must not be the event loop
        of the async client.  The client called ``await ratelimiter.acquire(...)``
        — a method that did not exist — so every authenticated call died on
        ``AttributeError`` before reaching the network.  Off-loading to a worker
        thread keeps one implementation and a responsive loop.
        """
        if not self.enabled:
            return Reservation(estimated_tokens=max(0, int(estimated_tokens)))
        return await asyncio.to_thread(self.reserve, estimated_tokens, block=block)

    def update_retry_after(self, retry_after: Optional[float]) -> float:
        """Async-face name for :meth:`penalize` (the client's 429 hook)."""
        return self.penalize(retry_after)

    # -- post-call accounting --------------------------------------------------
    def reconcile(self, reservation: Reservation, actual_tokens: Optional[int]) -> None:
        """Correct the token bucket once the real usage is known.

        An underestimate pushes the bucket further negative (we owe the difference);
        an overestimate refunds it. Request counts are never refunded - the provider
        counted the call the moment it arrived.
        """
        if self._tokens is None or actual_tokens is None:
            return
        delta = int(actual_tokens) - int(reservation.estimated_tokens)
        if delta == 0:
            return
        with self._lock:
            if delta > 0:
                self._tokens.consume(delta)
            else:
                self._tokens.refund(-delta)

    def penalize(self, retry_after: Optional[float]) -> float:
        """Apply a 429: block every caller for ``retry_after`` seconds."""
        delay = max(0.0, float(retry_after or 0.0))
        with self._lock:
            self._blocked_until = max(self._blocked_until, self.clock() + delay)
            if self._requests is not None:
                self._requests.drain()
            if self._tokens is not None:
                self._tokens.drain()
        return delay

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            now = self.clock()
            return {
                "rpm": self.rpm,
                "tpm": self.tpm,
                "enabled": self.enabled,
                "requests_available": (
                    round(self._requests.balance, 3) if self._requests is not None else None
                ),
                "tokens_available": (
                    round(self._tokens.balance, 1) if self._tokens is not None else None
                ),
                "blocked_for": round(max(0.0, self._blocked_until - now), 3),
            }


__all__ = ["RateLimiter", "Reservation", "TokenBucket"]
