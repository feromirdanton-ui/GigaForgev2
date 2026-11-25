from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from gigaforge.core.strategies import ReActPlan


def test_guarded_plan_salvages_successfully_written_files() -> None:
    strategy = ReActPlan()
    result = SimpleNamespace(
        plan=[],
        trajectory=[],
        written_files=["./index.html", "../outside.html", "C:/outside.html"],
    )

    steps, _ = strategy._guarded_steps(result)

    assert len(steps) == 1
    assert "index.html" in steps[0]
    assert "../outside.html" not in steps[0]
    assert "C:/outside.html" not in steps[0]


def test_unexecutable_plan_still_records_harness_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _State:
        task_id = "failed-plan"

        def __init__(self) -> None:
            self.phase_usages = {}

        def record_phase_usage(self, phase, usage) -> None:
            self.phase_usages[phase] = dict(usage)

    state = _State()
    runner = SimpleNamespace(state=SimpleNamespace(), ctx=SimpleNamespace(), budgets=None)
    ledger = SimpleNamespace(
        tokens_in=123,
        tokens_out=45,
        llm_calls=4,
        cost_micros=67,
    )

    class _Harness:
        def __init__(self, **kwargs) -> None:
            pass

        def run(self, *args, **kwargs):
            return SimpleNamespace(
                ok=True,
                result=SimpleNamespace(plan=[], trajectory=[], written_files=[]),
                ledger=ledger,
                finish_reason="max_steps",
                error="",
            )

    monkeypatch.setattr("gigaforge.harness.Harness", _Harness)
    strategy = ReActPlan(runner_factory=lambda goal, **kwargs: runner)

    with pytest.raises(RuntimeError, match="did not produce an executable plan"):
        strategy.plan_under_harness(
            "goal",
            "",
            ask=lambda *args, **kwargs: "",
            parse=lambda text: [],
            pipeline_state=state,
        )

    assert state.phase_usages["react"] == {
        "prompt_tokens": 123,
        "completion_tokens": 45,
        "total_tokens": 168,
        "calls": 4,
        "cost_micros": 67,
    }


def test_plan_with_written_file_is_marked_as_salvaged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = SimpleNamespace(
        plan=[],
        trajectory=[],
        written_files=["index.html"],
        localization={},
        finish_reason="max_steps",
        llm_calls=4,
    )
    ledger = SimpleNamespace(
        tokens_in=100,
        tokens_out=50,
        llm_calls=4,
        cost_micros=0,
        compactions=0,
        retries=0,
        note_plan=lambda **kwargs: None,
    )
    runner = SimpleNamespace(
        state=SimpleNamespace(),
        ctx=SimpleNamespace(),
        budgets=None,
    )

    class _Harness:
        def __init__(self, **kwargs) -> None:
            pass

        def run(self, *args, **kwargs):
            return SimpleNamespace(ok=True, result=result, ledger=ledger)

    monkeypatch.setattr("gigaforge.harness.Harness", _Harness)
    strategy = ReActPlan(runner_factory=lambda goal, **kwargs: runner)

    outcome, returned_ledger = strategy.plan_under_harness(
        "goal",
        "",
        ask=lambda *args, **kwargs: "",
        parse=lambda text: [],
    )

    assert returned_ledger is ledger
    assert outcome.salvaged is True
    assert outcome.written_files == ["index.html"]
    assert outcome.localization == {"target_file": "index.html"}
    assert outcome.steps and "index.html" in outcome.steps[0]


@pytest.mark.parametrize("file_exists", [True, False])
def test_plan_with_designated_file_is_salvaged(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    file_exists: bool,
) -> None:
    result = SimpleNamespace(plan=[], trajectory=[], written_files=[], localization={})
    ledger = SimpleNamespace(
        tokens_in=10,
        tokens_out=5,
        llm_calls=1,
        cost_micros=0,
        compactions=0,
        retries=0,
        note_plan=lambda **kwargs: None,
    )
    runner = SimpleNamespace(
        state=SimpleNamespace(),
        ctx=SimpleNamespace(worktree=tmp_path),
        budgets=None,
    )
    if file_exists:
        (tmp_path / "index.html").write_text("<html>existing</html>", encoding="utf-8")

    class _Harness:
        def __init__(self, **kwargs) -> None:
            pass

        def run(self, *args, **kwargs):
            return SimpleNamespace(ok=True, result=result, ledger=ledger)

    monkeypatch.setattr("gigaforge.harness.Harness", _Harness)
    strategy = ReActPlan(runner_factory=lambda goal, **kwargs: runner)

    outcome, _ = strategy.plan_under_harness(
        "Improve index.html",
        "",
        ask=lambda *args, **kwargs: "",
        parse=lambda text: [],
        fallback_paths=["index.html", "../outside.html"],
    )

    assert outcome.salvaged is True
    assert outcome.localization == {"target_file": "index.html"}
    assert outcome.written_files == (["index.html"] if file_exists else [])
    assert outcome.steps == [
        "Apply the requested changes to index.html and verify the result."
    ]
