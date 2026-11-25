"""A no-op budget claim for tests that only need the accounting to be inert.

The product now claims budget atomically (``BudgetTracker.reserve``) instead of
validating and recording in two steps, because check-then-commit let concurrent
callers pass the same remaining balance.  Test doubles that stand in for the
tracker therefore need a ``reserve`` too \u2014 and they need it to behave like the
real one: a claim that records nothing, and that can be settled or cancelled.

Keeping the stub in one place means a future change to the claim protocol is
one edit, not eight copies that drift.
"""
from __future__ import annotations

from typing import Any


class NullClaim:
    """A reservation that records nothing and never refuses."""

    def __init__(self) -> None:
        self.settled = False
        self.cancelled = False
        self.usage: tuple[int, int] | None = None

    def settle(self, tokens_in: int | None = None, tokens_out: int | None = None) -> None:
        self.settled = True
        self.usage = (int(tokens_in or 0), int(tokens_out or 0))

    def cancel(self) -> None:
        self.cancelled = True

    def __enter__(self) -> "NullClaim":
        return self

    def __exit__(self, *exc: Any) -> None:
        if not self.settled:
            self.cancel()


class NullBudgets:
    """A ``BudgetTracker`` stand-in: every call is allowed, nothing is recorded."""

    def __init__(self) -> None:
        self.claims: list[NullClaim] = []

    def check(self, *a: Any, **k: Any) -> None:
        return None

    def commit(self, *a: Any, **k: Any) -> None:
        return None

    def commit_task(self, *a: Any, **k: Any) -> None:
        return None

    def check_overshoot(self, *a: Any, **k: Any) -> None:
        return None

    def reserve(self, *a: Any, **k: Any) -> NullClaim:
        claim = NullClaim()
        self.claims.append(claim)
        return claim

    def remaining(self, phase: str) -> int:
        return 10**9

    def task_remaining_for(self, task_id: str) -> int:
        return 10**9

    def task_total(self) -> int:
        return 0
