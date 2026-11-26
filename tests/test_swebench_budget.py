"""The SWE-bench budget must be enforced, and the venv must be private.

Two defects, one shape: a parameter that was accepted and never used.

``run_instance(timeout_s=900)`` never read ``timeout_s``.  The real limits were
hard-coded per step -- clone 600, venv 300, install 900 (called twice on the
env-error path), tests 600 x 2 -- summing to ~3900s against a stated budget of
900s.  A run therefore could not "not fit in the budget": the budget did not
exist.  That is why the symbol-index fix was never re-verified; the report could
only say "did not finish", which reads like an external circumstance and was in
fact a missing line of code.

``_make_venv`` passed ``--system-site-packages``, which returned part of the
leak the venv exists to close: pip treats a system package as satisfying a
declared requirement, so a repo asking for ``pytest>=6.0`` against a system
pytest 8.x got "already satisfied" and ran on a version it never declared.

These tests pin the mechanism, not a live run: they need no network and no
credentials, so they run in CI where the real bench cannot.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from gigaforge.bench import swebench


# ── the deadline ────────────────────────────────────────────────────────────

def test_a_deadline_caps_a_step_at_what_is_left():
    """A 900s step inside a 10s budget must not be allowed to run for 900s."""
    deadline = swebench._Deadline(10)
    assert deadline.timeout(900) <= 10
    assert deadline.timeout(900) >= 1


def test_a_deadline_never_returns_zero():
    """Zero would make every subprocess fail instantly and read as a broken
    command rather than an exhausted budget -- the wrong diagnosis."""
    deadline = swebench._Deadline(1)
    time.sleep(1.05)
    assert deadline.expired
    assert deadline.timeout(600) == 1


def test_an_expired_deadline_refuses_to_start_a_step():
    """The step must not even be attempted: starting it and killing it wastes
    the budget the caller is trying to protect."""
    deadline = swebench._Deadline(1)
    time.sleep(1.05)
    code, out = swebench._run(["echo", "should-not-run"], Path("."), deadline=deadline)
    assert code == 124
    assert "deadline exhausted" in out


def test_a_live_deadline_lets_a_cheap_command_through():
    """The control: a deadline that has budget left must not block work."""
    deadline = swebench._Deadline(60)
    code, out = swebench._run(["echo", "ok"], Path("."), deadline=deadline)
    assert code == 0
    assert "ok" in out


def test_the_worst_case_is_bounded_by_the_budget():
    """The per-step defaults used to sum to ~3900s for a 900s budget.

    The first version of this test summed ``deadline.timeout(d)`` over the
    defaults and expected <= 900 -- which is wrong, and wrong in an instructive
    way: each call sees the *same* remaining budget, so the sum is not the
    worst case.  The real property is that the budget is spent, not that the
    numbers add up: once the deadline has passed, every further step is
    refused, so the total wall clock cannot exceed the budget by more than the
    one step that was already running.
    """
    deadline = swebench._Deadline(2)
    defaults = [600, 300, 900, 900, 600, 600]
    started = time.monotonic()
    for default in defaults:
        code, _ = swebench._run(["sleep", "0.4"], Path("."), timeout=default,
                                deadline=deadline)
        if code == 124:
            break
    elapsed = time.monotonic() - started
    # Two seconds of budget plus at most one in-flight step (0.4s), never 3900.
    assert elapsed < 4.0
    assert deadline.expired


# ── the venv is private ─────────────────────────────────────────────────────

def test_the_venv_does_not_see_system_packages():
    """``--system-site-packages`` silently undid the isolation.

    Asserted on the command the code builds, not on the module's source text:
    the first version scanned ``_make_venv``'s source and would have passed on
    a docstring that merely mentioned the flag.  A test that cannot tell a flag
    from a note about the flag measures the wrong thing.
    """
    command = swebench._venv_command(Path("/tmp/x.venv"))
    assert "--system-site-packages" not in command
    assert command[:3] == [swebench.sys.executable, "-m", "venv"]


def test_declared_dependencies_win_over_system_ones():
    """``--ignore-installed`` is what makes the private venv mean something.

    Without it pip answers "already satisfied" for a system package and the
    repo runs on a version it never declared -- the same class of defect as the
    leak, only silent.
    """
    import inspect

    source = inspect.getsource(swebench._install_test_requirements)
    assert "--ignore-installed" in source


def test_the_result_records_the_pytest_that_graded_it():
    """"Gradable" is a claim about an environment; without the version it is
    unprovable."""
    result = swebench.SweResult(instance_id="x", repo="y", status="passed")
    result.pytest_version = "pytest 7.1.2"
    assert result.to_dict()["pytest_version"] == "pytest 7.1.2"


def test_timeout_is_a_status_of_its_own():
    """An exhausted budget is not a broken environment and not a wrong answer.

    Blending it into ``environment_error`` is how "the run did not fit" became
    "the model failed" in an earlier report.
    """
    result = swebench.SweResult(instance_id="x", repo="y", status="timeout")
    summary = swebench.summarise([result])
    assert summary["by_status"]["timeout"] == 1
    assert summary["environment_errors"] == 0
