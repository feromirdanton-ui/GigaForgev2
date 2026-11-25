"""The approval gate on the graph path: all four decisions, not just yes/no.

``langgraph_goal`` has had an ``interrupt()`` approval node for several
iterations, and it was described as "the ``interrupt()`` replacement for
``ApprovalGate``".  It was not: it accepted a ``Callable[[str], bool]``, so the
graph could express exactly two of the gate's four decisions.  ``edit`` — the
one that carries a human's diff back into the run — and ``abort`` were
unreachable, and ``reject`` was indistinguishable from "the gate said no".

These tests drive the real graph with a real ``ApprovalGate`` and assert each
decision's *effect*, not just that a callback was called:

* ``apply``  — the verified files are published.
* ``reject`` — nothing is published, and the record says why.
* ``abort``  — nothing is published, and the publish node never runs.
* ``edit``   — the human's diff is applied to the workspace copy and *that* is
  what gets published; a diff that does not apply is refused rather than
  published unapplied.

The last one is the reason this module exists.  "Publish the diff the human
approved" and "publish the diff the pipeline produced" are different claims, and
only a test that inspects what was published can tell them apart.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from gigaforge.core.approval import (
    ApprovalDecision,
    ApprovalGate,
    ApprovalResult,
)

class _Report:
    def __init__(self, passed: bool | None) -> None:
        self.passed = passed
        self.details: dict[str, Any] = {}

    def feedback(self) -> str:
        return "" if self.passed else "verification failed"

    def to_dict(self) -> dict[str, Any]:
        return {"passed": self.passed, "details": self.details}


class _Pipeline:
    def __init__(self) -> None:
        self.calls = 0

    def run(self, prompt: str, **kwargs: Any) -> Any:
        self.calls += 1

        class _Result:
            accepted = True
            status = "ok"
            changed_files = ["calc.py"]
            summary = "added add()"
            tokens = 10
            calls = 1
            duration_ms = 1.0
            error = ""
            final_diff = (
                "--- a/calc.py\n"
                "+++ b/calc.py\n"
                "@@ -1,1 +1,2 @@\n"
                " def add(a, b):\n"
                "+    return a + b\n"
            )

        return _Result()


class _Verifier:
    def verify(self, root: Path, changed: list[str], goal: str) -> _Report:
        return _Report(True)


class _Workspace:
    """A workspace that records what it was asked to publish."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.destination = root
        self.published: list[list[str]] = []

    def publish(self, changed: list[str]) -> list[str]:
        self.published.append(list(changed))
        return list(changed)

    def cleanup_project_copy(self) -> None:
        return None


class _Gate(ApprovalGate):
    """A gate that returns a fixed decision, recording what it was shown."""

    def __init__(self, decision: ApprovalDecision, edited_diff: str = "") -> None:
        self.decision = decision
        self.edited_diff = edited_diff
        self.seen: list[str] = []

    async def review(self, diff, test_results, *, task_id):
        self.seen.append(diff)
        return ApprovalResult(
            decision=self.decision,
            edited_diff=self.edited_diff,
            reason=f"test gate: {self.decision}",
        )


def _run(tmp_path: Path, gate: ApprovalGate, workspace: _Workspace | None = None):
    from gigaforge.core.langgraph_goal import run_goal_graph

    return run_goal_graph(
        _Pipeline(),
        repo_root=tmp_path,
        goal="make add() work",
        verifier=_Verifier(),
        workspace=workspace or _Workspace(tmp_path),
        checkpoint_path=tmp_path / "cp.sqlite3",
        gate=gate,
    )


def test_gate_apply_publishes(tmp_path: Path) -> None:
    workspace = _Workspace(tmp_path)
    gate = _Gate(ApprovalDecision.APPLY)
    result = _run(tmp_path, gate, workspace)
    assert result.status == "completed"
    assert workspace.published == [["calc.py"]]
    assert gate.seen, "the gate was never shown the diff"


def test_gate_reject_does_not_publish(tmp_path: Path) -> None:
    workspace = _Workspace(tmp_path)
    result = _run(tmp_path, _Gate(ApprovalDecision.REJECT), workspace)
    assert workspace.published == []
    assert result.status == "needs_attention"
    assert "declined" in result.error


def test_gate_abort_does_not_publish_and_skips_publish(tmp_path: Path) -> None:
    """``abort`` means stop, not "record a refusal and end".

    Asserting only that nothing was published is not enough: routing ``abort``
    to the publish node also publishes nothing, because publish refuses a
    non-apply decision.  The two are distinguished by whether the publish node
    *ran*, which the stream events record.  Verified mutationally — routing
    ``abort`` to publish fails this test and only this test.
    """
    from gigaforge.core.langgraph_goal import run_goal_graph

    workspace = _Workspace(tmp_path)
    seen: list[str] = []
    result = run_goal_graph(
        _Pipeline(),
        repo_root=tmp_path,
        goal="make add() work",
        verifier=_Verifier(),
        workspace=workspace,
        checkpoint_path=tmp_path / "cp.sqlite3",
        gate=_Gate(ApprovalDecision.ABORT),
        emit=lambda event, payload: seen.append(event),
    )
    assert workspace.published == []
    assert result.status != "completed"
    assert "goal.publish" not in seen, (
        "the publish node ran for an aborted run; abort must stop the graph"
    )
    # And the record must say the user stopped it, not that the cycle budget ran
    # out — the two are different facts and only one of them is true here.
    assert "cycle limit" not in result.error, (
        f"an aborted run reported a cycle-budget reason: {result.error!r}"
    )
    assert "abort" in result.error.lower() or "test gate" in result.error.lower(), (
        f"the abort reason was not recorded: {result.error!r}"
    )


def test_gate_edit_publishes_the_edited_diff(tmp_path: Path) -> None:
    """The published diff must be the human's, not the pipeline's.

    This is the assertion that separates "we accepted an edit" from "we
    published the edit".  The edited diff touches a *different* file, so a run
    that published the pipeline's ``changed_files`` fails it.
    """
    workspace = _Workspace(tmp_path)
    # The workspace copy must contain the file the edited diff targets.
    (tmp_path / "calc.py").write_text("def add(a, b):\n    return 0\n")
    (tmp_path / "other.py").write_text("x = 1\n")

    edited = (
        "--- a/other.py\n"
        "+++ b/other.py\n"
        "@@ -1,1 +1,2 @@\n"
        " x = 1\n"
        "+y = 2\n"
    )
    result = _run(tmp_path, _Gate(ApprovalDecision.EDIT, edited_diff=edited), workspace)

    assert workspace.published, "nothing was published after an edit"
    published = workspace.published[0]
    assert published == ["other.py"], (
        f"the pipeline's changed_files were published instead of the edit: {published}"
    )
    assert result.status == "completed"


def test_gate_edit_with_an_unappliable_diff_is_refused(tmp_path: Path) -> None:
    """A diff that does not apply must not be published as if it had.

    Publishing a diff that was never applied is the failure this gate exists to
    prevent, so the run must record the refusal rather than proceed.
    """
    workspace = _Workspace(tmp_path)
    (tmp_path / "calc.py").write_text("def add(a, b):\n    return 0\n")

    edited = (
        "--- a/calc.py\n"
        "+++ b/calc.py\n"
        "@@ -1,1 +1,2 @@\n"
        " this context line does not exist in the file\n"
        "+    return a + b\n"
    )
    result = _run(tmp_path, _Gate(ApprovalDecision.EDIT, edited_diff=edited), workspace)

    assert workspace.published == [], "an unappliable edit was published anyway"
    assert result.status != "completed"


def test_gate_edit_with_no_headers_is_refused(tmp_path: Path) -> None:
    workspace = _Workspace(tmp_path)
    result = _run(tmp_path, _Gate(ApprovalDecision.EDIT, edited_diff="not a diff"), workspace)
    assert workspace.published == []
    assert result.status != "completed"


def test_a_raising_gate_aborts_rather_than_publishing(tmp_path: Path) -> None:
    """A broken gate must not publish."""

    class _Broken(ApprovalGate):
        async def review(self, diff, test_results, *, task_id):
            raise RuntimeError("gate exploded")

    workspace = _Workspace(tmp_path)
    result = _run(tmp_path, _Broken(), workspace)
    assert workspace.published == []
    assert result.status != "completed"


def test_the_boolean_approve_callable_still_works(tmp_path: Path) -> None:
    """The documented ``approve`` signature is not broken by the richer gate."""
    from gigaforge.core.langgraph_goal import run_goal_graph

    asked: list[str] = []

    def approve(diff: str) -> bool:
        asked.append(diff)
        return True

    result = run_goal_graph(
        _Pipeline(),
        repo_root=tmp_path,
        goal="make add() work",
        verifier=_Verifier(),
        workspace=_Workspace(tmp_path),
        checkpoint_path=tmp_path / "cp.sqlite3",
        approve=approve,
    )
    assert result.status == "completed"
    assert len(asked) == 1
