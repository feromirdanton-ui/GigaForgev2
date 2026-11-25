from __future__ import annotations

from types import SimpleNamespace

from gigaforge.core.contracts import ValidateResult
from gigaforge.core.pipeline import TaskPipeline


def test_cached_fixloop_validation_still_runs_project_test_gate() -> None:
    cached = ValidateResult(
        accepted=True,
        applied=True,
        changed_files=["src/score.py"],
    )
    pipeline = SimpleNamespace(
        fixloop=SimpleNamespace(
            validator=object(),
            validate_once=lambda *args, **kwargs: None,
        ),
        test_runner=lambda: SimpleNamespace(
            passed=False,
            summary="IndentationError in src/score.py",
        ),
    )
    ctx = SimpleNamespace(
        extras={
            "fix_result": SimpleNamespace(
                iterations=[SimpleNamespace(validate=cached)]
            ),
            "dry_run": False,
            "test_gate": True,
        },
        state=SimpleNamespace(goal="Fix src/score.py"),
    )

    outcome = TaskPipeline._node_validate(
        pipeline,
        ctx,
        {"proposal": SimpleNamespace(diff="diff --git a/src/score.py")},
    )

    validation = outcome["validation"]
    assert validation.accepted is False
    assert validation.applied is True
    assert validation.changed_files == ["src/score.py"]
    assert "test suite failed" in validation.reason
    assert "IndentationError" in validation.reason
