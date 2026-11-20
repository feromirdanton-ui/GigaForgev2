"""Exponential backoff with jitter, plus honest ``Retry-After`` handling.

``app.py`` had no retry loop at all: a transient 500 or a dropped connection was a
hard failure for the caller. This module supplies the policy and the loop, and
treats three inputs as authoritative:

* ``retryable`` on the error class (5xx / 429 / network / timeout are retryable);
* the provider's ``Retry-After`` header, which *overrides* our own backoff rather
  than merely adding to it;
* a hard cap, so a malicious or buggy ``Retry-After: 86400`` cannot pin a worker.
"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Optional

from .errors import GigaForgeError, error_for_exception


def parse_retry_after(value: Any, *, now: Optional[float] = None) -> Optional[float]:
    """Parse a ``Retry-After`` header value (delta-seconds or HTTP-date)."""
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        seconds = float(value)
        return seconds if seconds >= 0 else None
    text = str(value).strip()
    if not text:
        return None
    try:
        seconds = float(text)
        return seconds if seconds >= 0 else None
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    reference = time.time() if now is None else now
    return max(0.0, when.timestamp() - reference)


@dataclass
class RetryPolicy:
    max_retries: int = 4
    base: float = 0.5
    cap: float = 15.0
    jitter: bool = True
    jitter_ratio: float = 0.5
    max_retry_after: float = 30.0
    respect_retry_after: bool = True
    #: Floor for any retry delay.  A retry is never instantaneous: a zero delay
    #: is a busy loop against a provider that has just asked us to slow down.
    min_delay: float = 0.5

    @classmethod
    def from_config(cls, config) -> "RetryPolicy":
        return cls(
            max_retries=int(getattr(config, "max_retries", 4)),
            base=float(getattr(config, "backoff_base", 0.5)),
            cap=float(getattr(config, "backoff_max", 15.0)),
            jitter=bool(getattr(config, "backoff_jitter", True)),
            max_retry_after=float(getattr(config, "max_retry_after", 30.0)),
        )

    def backoff(self, attempt: int) -> float:
        """Pure exponential term for a 1-based attempt number."""
        return min(self.cap, self.base * (2 ** max(0, attempt - 1)))

    def compute_delay(
        self,
        attempt: int,
        retry_after: Optional[float] = None,
        *,
        rng: Callable[[], float] = random.random,
    ) -> float:
        """Delay before attempt ``attempt + 1``.

        A provider ``Retry-After`` is a floor *and* a ceiling for us: waiting less
        would just be throttled again, waiting more is pointless. Our own backoff is
        jittered so a fleet of clients does not resynchronise after an outage.

        A **non-positive** ``Retry-After`` is treated as no hint at all.  Measured
        live: GigaChat answers 429 with ``Retry-After: 0``, and honouring it
        literally made every retry instantaneous —

            provider call failed (GF-RATE-429, attempt 1/4); retrying in 0.00s
            provider call failed (GF-RATE-429, attempt 2/4); retrying in 0.00s
            provider call failed (GF-RATE-429, attempt 3/4); retrying in 0.00s
            provider call failed (GF-RATE-429, attempt 4/4); retrying in 0.00s

        — so the whole retry budget was spent inside the same throttled instant
        and the run failed on a limit that a one-second wait would have cleared.
        A server saying "retry now" while refusing the request is not giving a
        hint; it is declining to give one.
        """
        server_hint: Optional[float] = None
        if self.respect_retry_after and retry_after is not None:
            hint = float(retry_after)
            if hint > 0:
                server_hint = min(hint, self.max_retry_after)
        if server_hint is not None:
            return server_hint
        delay = self.backoff(attempt)
        if self.jitter and delay > 0:
            spread = delay * max(0.0, min(1.0, self.jitter_ratio))
            delay = delay - spread + (rng() * spread)
        # A retry is never instantaneous: a zero delay is a busy loop against a
        # provider that has just told us to slow down.
        return max(self.min_delay, min(delay, self.cap))


def retry_call(
    fn: Callable[[int], Any],
    *,
    policy: RetryPolicy,
    retryable: Callable[[GigaForgeError], bool] = lambda exc: bool(exc.retryable),
    sleep: Callable[[float], None] = time.sleep,
    rng: Callable[[], float] = random.random,
    on_retry: Optional[Callable[[GigaForgeError, int, float], None]] = None,
) -> Any:
    """Run ``fn(attempt)`` with bounded retries. Returns the first success.

    ``fn`` receives the 1-based attempt number so a caller can build retry-aware
    payloads (for example, adding a stricter instruction on the second try).
    """
    attempt = 0
    while True:
        attempt += 1
        try:
            return fn(attempt)
        except BaseException as exc:  # noqa: BLE001 - re-raised as a taxonomy error
            error = error_for_exception(exc)
            if attempt > policy.max_retries or not retryable(error):
                raise error
            delay = policy.compute_delay(attempt, error.retry_after, rng=rng)
            if on_retry is not None:
                on_retry(error, attempt, delay)
            sleep(delay)


__all__ = ["RetryPolicy", "parse_retry_after", "retry_call"]
