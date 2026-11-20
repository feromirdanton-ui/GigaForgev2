"""The budget must hold under concurrency, not only in a single thread.

**The defect this file exists for.**  ``BudgetTracker.check`` validates and
``BudgetTracker.commit`` records, and they take the lock separately.  That is
correct for a retry (an estimate must not inflate the budget) and wrong for
concurrency: the sectional path runs four section children in a
``ThreadPoolExecutor``, all four call ``check`` against the same remaining
balance, all four are told there is room, and the phase overshoots by up to 4x.

The review found this by reading the code.  These tests reproduce it: the
first one fails on the old ``check``/``commit`` pair and passes on
``reserve``/``release``, so the fix is pinned by a test that would have caught
the bug rather than by a test that merely exercises the new API.
"""
from __future__ import annotations

import threading
import time

import pytest

from gigaforge.client.errors import BudgetExceeded
from gigaforge.core.budgets import BudgetTracker
from gigaforge.config import Settings


def _tracker(**overrides) -> BudgetTracker:
    """A tracker with small, explicit caps so the arithmetic is checkable."""
    from dataclasses import replace

    from gigaforge.config import get_settings

    base = replace(
        get_settings(),
        budget_react_in=1000,
        budget_react_out=1000,
        budget_task_cap=100_000,
    )
    if overrides:
        base = replace(base, **overrides)
    return BudgetTracker(settings=base)


def test_reserve_is_atomic_under_four_concurrent_claims():
    """Four threads claiming 400 of a 1000 cap: exactly two may win.

    With ``check`` + ``commit`` all four pass the check (each sees 0 used) and
    the phase ends at 1600 \u2014 a 1.6x overshoot.  With ``reserve`` the claim is
    recorded under the same lock that validates it, so the third and fourth
    claims are refused.
    """
    tracker = _tracker()
    granted: list[int] = []
    refused: list[int] = []
    barrier = threading.Barrier(4)
    lock = threading.Lock()

    def claim(index: int) -> None:
        barrier.wait()  # all four reach the claim at the same instant
        try:
            reservation = tracker.reserve("react", 400, 0, task_id="t")
        except BudgetExceeded:
            with lock:
                refused.append(index)
            return
        with lock:
            granted.append(index)
        # Hold the claim briefly, as a real in-flight call would.
        time.sleep(0.02)
        reservation.settle(400, 0)

    threads = [threading.Thread(target=claim, args=(i,)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(granted) == 2, f"granted={granted} refused={refused}"
    assert len(refused) == 2
    used = tracker.snapshot()["phase_used"]["react"]["in"]
    assert used == 800, f"phase spent {used} of a 1000 cap"
    assert used <= 1000


def test_check_then_commit_would_have_overshot():
    """The old shape, kept as a regression witness.

    This is not testing the product \u2014 it is testing that the *reason* for
    ``reserve`` is real.  If someone "simplifies" ``reserve`` back into
    ``check`` + ``commit``, the test above fails and this one explains why.
    """
    tracker = _tracker()
    barrier = threading.Barrier(4)
    passed: list[int] = []
    lock = threading.Lock()

    def old_shape(index: int) -> None:
        barrier.wait()
        try:
            tracker.check("react", 400, 0, task_id="t")
        except BudgetExceeded:
            return
        with lock:
            passed.append(index)
        time.sleep(0.02)
        tracker.commit("react", 400, 0, task_id="t")

    threads = [threading.Thread(target=old_shape, args=(i,)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    # All four were told there was room: this is the defect, reproduced.
    assert len(passed) == 4
    assert tracker.snapshot()["phase_used"]["react"]["in"] == 1600


def test_release_records_actual_usage_not_the_estimate():
    """A call that spent less than it claimed must not be charged the claim."""
    tracker = _tracker()
    reservation = tracker.reserve("react", 500, 500, task_id="t")
    reservation.settle(120, 80)
    snapshot = tracker.snapshot()
    assert snapshot["phase_used"]["react"] == {"in": 120, "out": 80}
    assert snapshot["reserved"] == {}


def test_cancel_returns_the_claim():
    """A failed call must not leak its claim and starve the next one."""
    tracker = _tracker()
    reservation = tracker.reserve("react", 900, 0, task_id="t")
    reservation.cancel()
    assert tracker.snapshot()["reserved"] == {}
    # The full cap is available again.
    again = tracker.reserve("react", 900, 0, task_id="t")
    again.settle(900, 0)


def test_context_manager_releases_on_exception():
    """Leaving the block by raising must drop the claim, not keep it."""
    tracker = _tracker()
    with pytest.raises(RuntimeError):
        with tracker.reserve("react", 900, 0, task_id="t"):
            raise RuntimeError("provider died")
    assert tracker.snapshot()["reserved"] == {}
    assert tracker.snapshot()["phase_used"]["react"]["in"] == 0


def test_settle_is_idempotent():
    """A double settle must not double-charge."""
    tracker = _tracker()
    reservation = tracker.reserve("react", 100, 100, task_id="t")
    reservation.settle(100, 100)
    reservation.settle(100, 100)
    assert tracker.snapshot()["phase_used"]["react"] == {"in": 100, "out": 100}


def test_per_task_cap_counts_outstanding_claims():
    """The per-task cap must see claims, not only committed usage."""
    tracker = _tracker(budget_task_cap=1000)
    first = tracker.reserve("react", 600, 0, task_id="t")
    with pytest.raises(BudgetExceeded):
        tracker.reserve("react", 600, 0, task_id="t")
    first.settle(600, 0)
    # After settling, the committed 600 still blocks a second 600.
    with pytest.raises(BudgetExceeded):
        tracker.reserve("react", 600, 0, task_id="t")


def test_reserve_refuses_when_phase_is_already_full():
    tracker = _tracker()
    first = tracker.reserve("react", 1000, 0, task_id="t")
    with pytest.raises(BudgetExceeded):
        tracker.reserve("react", 1, 0, task_id="t")
    first.settle(1000, 0)
