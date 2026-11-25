"""An empty plan has two causes, and they need different words.

**Measured live** (``goal --subagents`` on the cyberpunk site goal): the provider
answered 429 fifty-five times, the retries inside the episode were exhausted, the
episode ended with ``finish_reason: "error"`` and no plan — and the run reported::

    node 'plan' failed: LangGraph ReAct did not produce an executable plan:
    {'steps': 0, 'dropped_unknown': [], ...}

That sentence blames the model's competence for a quota.  It also broke the
recovery path: ``GoalRunner`` decides whether to wait and retry the same cycle by
looking for ``429``/``rate limit`` in the error text, so a mislabelled error was
never recognised as transient and a cycle was burned instead of waited out.

The distinction the fix makes:

* ``finish_reason`` in ``{"finish_action", "max_steps"}`` with no steps — the
  model really did not produce an executable plan.  The old message is right.
* any other ``finish_reason`` (``error``, ``budget_exceeded``,
  ``context_overflow``) — the episode was cut short.  The message must name the
  reason and carry the underlying error, so the caller can classify it.
"""
from __future__ import annotations

import pytest

from gigaforge.agents.react import ReActResult
from gigaforge.core.strategies import ReActPlan


class _StubStrategy(ReActPlan):
    """A strategy whose episode returns a canned result."""

    def __init__(self, result: ReActResult) -> None:
        super().__init__(runner_factory=lambda goal, **_: object())
        self._canned = result

    def _build_runner(self, goal: str):  # type: ignore[override]
        class _Stub:
            state = {}
            ctx = None

        return _Stub()

    def _guarded_steps(self, result):  # type: ignore[override]
        from gigaforge.harness.plan_guard import validate_plan

        return [], validate_plan([], known_tools=set())


def _plan(strategy: _StubStrategy, monkeypatch: pytest.MonkeyPatch) -> str:
    import gigaforge.agents.engine as engine

    monkeypatch.setattr(engine, "run_react", lambda *a, **k: strategy._canned)
    with pytest.raises(RuntimeError) as caught:
        strategy.plan("do the thing", "", ask=lambda *a, **k: "", parse=lambda t: [])
    return str(caught.value)


class TestTheMessageNamesTheRealCause:
    def test_a_provider_error_is_not_reported_as_a_planning_defect(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result = ReActResult(
            finish_reason="error",
            error="ProviderError: GF-RATE-429 too many requests",
        )
        message = _plan(_StubStrategy(result), monkeypatch)
        assert "did not produce an executable plan" not in message
        assert "finish_reason=error" in message
        assert "429" in message, (
            "the underlying error must survive, or the goal cannot classify it "
            "as transient and will burn a cycle instead of waiting"
        )

    def test_a_budget_stop_is_named_as_such(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result = ReActResult(finish_reason="budget_exceeded")
        message = _plan(_StubStrategy(result), monkeypatch)
        assert "finish_reason=budget_exceeded" in message
        assert "did not produce an executable plan" not in message

    def test_a_genuinely_empty_plan_keeps_the_old_message(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The guard rejecting every step IS a planning defect."""
        result = ReActResult(finish_reason="finish_action")
        message = _plan(_StubStrategy(result), monkeypatch)
        assert "did not produce an executable plan" in message


class TestTheEpisodeRecordsWhyItStopped:
    def test_the_result_carries_an_error_field(self) -> None:
        assert ReActResult().error == ""

    def test_the_graph_maps_the_state_error_onto_the_result(self) -> None:
        from gigaforge.agents.langgraph_react import _to_result

        result = _to_result({
            "finish_reason": "error",
            "error": "ProviderError: GF-RATE-429",
        })
        assert result.finish_reason == "error"
        assert "429" in result.error
