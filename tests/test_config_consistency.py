"""The two configuration modules must not disagree about a phase.

GigaForge has two: ``gigaforge/config.py`` (``Settings``, the flat ``GF_CLIENT__``
/ ``GIGAFORGE_`` knobs) and ``gigaforge/core/config.py`` (``CoreConfig``, the
nested ``GF_CORE__`` knobs).  ``BudgetTracker`` reads the first; ``ModelRouter``
reads the second.  A phase that exists in one and not the other is a phase that
is budgeted but not routed, or routed but not budgeted — and the failure is
silent, because both sides have a default.

That is exactly what happened to ``react``: the episode billed ``plan`` in
``Settings`` while ``CoreConfig`` had no ``react`` route at all, so the loop ran
on the planning model with the planning call budget and died on its second step.
"""
from __future__ import annotations

import pytest


def _settings_phases() -> set[str]:
    from gigaforge.core.budgets import PHASE_CAP_ATTRS

    return set(PHASE_CAP_ATTRS)


def _core_phases() -> set[str]:
    from gigaforge.core.config import CoreConfig

    cfg = CoreConfig()
    return set(cfg.budgets) | set(cfg.routes)


def test_every_budgeted_phase_is_also_routed() -> None:
    """A phase with a budget but no route falls through to the legacy table."""
    from gigaforge.core.config import CoreConfig

    cfg = CoreConfig()
    budgeted = set(cfg.budgets)
    routed = set(cfg.routes)
    assert budgeted <= routed, f"budgeted but not routed: {sorted(budgeted - routed)}"


def test_the_react_phase_exists_on_both_sides() -> None:
    """The loop's phase must be known to the tracker AND to the router."""
    assert "react" in _settings_phases()
    assert "react" in _core_phases()


def test_the_react_phase_has_a_loop_sized_call_budget() -> None:
    """A loop needs more than the four calls a single planning phase gets."""
    from gigaforge.core.config import CoreConfig

    cfg = CoreConfig()
    react = cfg.budget_for("react")
    plan = cfg.budget_for("plan")
    assert react.max_calls > plan.max_calls, (
        "a ReAct episode is a loop; giving it the single-call planning budget is "
        "what killed the live episode on its second step"
    )
    assert react.max_tokens >= plan.max_tokens


def test_the_react_phase_routes_to_a_real_model() -> None:
    from gigaforge.core.config import CoreConfig

    cfg = CoreConfig()
    route = cfg.routes["react"]
    assert route.model
    assert route.temperature <= 0.2, "exploration should stay near-deterministic"


def test_react_episode_timeout_is_independently_configurable() -> None:
    from gigaforge.core.config import CoreConfig

    default = CoreConfig()
    configured = CoreConfig.from_env({"GF_CORE__REACT_TIMEOUT": "333"})

    assert default.react_timeout == 480.0
    assert configured.react_timeout == 333.0
    assert configured.to_dict()["react_timeout"] == 333.0


def test_react_plan_node_uses_its_episode_timeout(tmp_path) -> None:
    from gigaforge.core.config import CoreConfig
    from gigaforge.core.pipeline import TaskPipeline

    pipeline = TaskPipeline(config=CoreConfig(
        repo_root=str(tmp_path),
        db_path=str(tmp_path / "core.sqlite3"),
        trace_path=str(tmp_path / "traces" / "core.jsonl"),
        react_timeout=333.0,
    ))
    try:
        assert pipeline.build().nodes["plan"].timeout == 333.0
    finally:
        pipeline.close()


@pytest.mark.parametrize("phase", ["plan", "code", "review", "react"])
def test_a_phase_resolves_to_a_non_empty_budget(phase: str) -> None:
    from gigaforge.core.config import CoreConfig

    budget = CoreConfig().budget_for(phase)
    assert budget.max_tokens > 0
    assert budget.max_calls > 0
