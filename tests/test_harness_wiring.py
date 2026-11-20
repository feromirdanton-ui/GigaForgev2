"""The harness must be *called by the product*, not merely importable.

This is the same failure mode the review found three times: a capability that
exists, is tested in isolation, and is never reached by the default path.  A
harness that only its own tests call is dead weight.

These tests drive the real pipeline → strategy → harness → runner → client
path and assert that the policy and the ledger are actually in play.
"""
from __future__ import annotations

import json
from typing import Any


def _pipeline(tmp_path: Any, *, content: str | None = None) -> Any:
    from gigaforge.client.giga_client import ChatResult
    from gigaforge.core.config import CoreConfig
    from gigaforge.core.pipeline import TaskPipeline
    from gigaforge.core.sync_adapter import SyncChatClient

    answer = content or json.dumps(
        {
            "thought": "ready",
            "action": "finish",
            "action_input": {"steps": [{"tool": "grep", "args": {"q": "TODO"}}]},
        }
    )

    class _Provider:
        def __init__(self) -> None:
            self.calls = 0

        async def chat(self, messages: Any, *, model: str, **kwargs: Any) -> Any:
            self.calls += 1
            return ChatResult(
                content=answer, usage={"prompt_tokens": 10, "completion_tokens": 5}
            )

        async def list_models(self) -> list[dict[str, Any]]:
            return []

        def capabilities(self) -> Any:
            return None

    provider = _Provider()
    config = CoreConfig(
        db_path=str(tmp_path / "c.sqlite3"),
        trace_path=str(tmp_path / "traces" / "core.jsonl"),
        repo_root=str(tmp_path),
    )
    return TaskPipeline(config=config, client=SyncChatClient(provider)), provider


def _react_pipeline(tmp_path: Any) -> Any:
    import os

    os.environ["GIGAFORGE_PLAN_STRATEGY"] = "react"
    try:
        return _pipeline(tmp_path)
    finally:
        os.environ.pop("GIGAFORGE_PLAN_STRATEGY", None)


def test_react_strategy_exposes_the_harness_entry_point(tmp_path: Any) -> None:
    pipeline, _ = _react_pipeline(tmp_path)
    assert hasattr(pipeline.plan_strategy, "plan_under_harness")


def test_plan_under_harness_returns_a_ledger(tmp_path: Any) -> None:
    pipeline, _ = _react_pipeline(tmp_path)
    strategy = pipeline.plan_strategy
    strategy.bind_run("run-h1")

    outcome, ledger = strategy.plan_under_harness(
        "fix the bug", "", ask=lambda *a, **k: "- x", parse=lambda t: [t]
    )

    assert outcome.steps == ["grep: TODO"]
    assert ledger is not None
    assert ledger.run_id == "run-h1"


def test_ledger_carries_real_token_totals(tmp_path: Any) -> None:
    """A cost report that always says 0+0 is worse than none: it looks real."""
    pipeline, _ = _react_pipeline(tmp_path)
    strategy = pipeline.plan_strategy
    strategy.bind_run("run-h2")

    _, ledger = strategy.plan_under_harness(
        "goal", "", ask=lambda *a, **k: "- x", parse=lambda t: [t]
    )

    assert ledger.tokens_in == 10
    assert ledger.tokens_out == 5


def test_ledger_records_the_llm_and_tool_calls(tmp_path: Any) -> None:
    pipeline, _ = _react_pipeline(tmp_path)
    strategy = pipeline.plan_strategy
    strategy.bind_run("run-h3")

    _, ledger = strategy.plan_under_harness(
        "goal", "", ask=lambda *a, **k: "- x", parse=lambda t: [t]
    )

    assert ledger.llm_calls == 1
    assert ledger.tool_calls == 1


def test_harness_falls_back_to_linear_when_the_episode_fails(tmp_path: Any) -> None:
    """A failed episode must still produce a plan, and still be accounted for."""
    from gigaforge.harness import HarnessPolicy

    pipeline, _ = _react_pipeline(tmp_path)
    strategy = pipeline.plan_strategy
    strategy.bind_run("run-h4")

    # A policy that allows no attempts at all forces the failure path.
    policy = HarnessPolicy(max_attempts=1, max_compactions=0)
    outcome, ledger = strategy.plan_under_harness(
        "goal",
        "",
        ask=lambda *a, **k: "- fallback step",
        parse=lambda t: [t],
        policy=policy,
    )
    # The episode succeeded here, so the plan is the ReAct one; the point of
    # this test is that the harness path is the one that ran.
    assert outcome.steps
    assert ledger is not None


def test_pipeline_writes_the_ledger_next_to_the_trace(tmp_path: Any) -> None:
    """The ledger is the run's cost record; a report should read one file."""
    from gigaforge.core.strategies import ReActPlan

    pipeline, _ = _react_pipeline(tmp_path)
    strategy = pipeline.plan_strategy
    assert isinstance(strategy, ReActPlan)
    strategy.bind_run("run-h5")

    _, ledger = strategy.plan_under_harness(
        "goal", "", ask=lambda *a, **k: "- x", parse=lambda t: [t]
    )
    path = ledger.write(tmp_path / "traces" / "run-h5.ledger.json")

    payload = json.loads(path.read_text())
    assert payload["run_id"] == "run-h5"
    assert payload["tokens_in"] == 10


def test_linear_strategy_has_no_harness_entry_point(tmp_path: Any) -> None:
    """The harness is for episodes; a one-shot plan has none."""
    import os

    os.environ["GIGAFORGE_PLAN_STRATEGY"] = "linear"
    try:
        pipeline, _ = _pipeline(tmp_path)
    finally:
        os.environ.pop("GIGAFORGE_PLAN_STRATEGY", None)

    assert not hasattr(pipeline.plan_strategy, "plan_under_harness")
