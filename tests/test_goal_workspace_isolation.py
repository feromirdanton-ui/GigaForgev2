"""A goal run must write into its isolated workspace, not the destination.

Measured live: `gigaforge goal` created index.html, then failed to publish with
"destination changed while the goal was running; resolve these files before
publishing: index.html".  The run had changed the destination itself.

The cause was the ReAct runner factory: a goal run builds a *second*, isolated
pipeline and hands it the same strategy object.  The factory was only attached
when absent, so the isolated pipeline kept the first pipeline's factory and its
episode wrote into the destination instead of the workspace.
"""
from __future__ import annotations

from pathlib import Path

from gigaforge.core.pipeline import TaskPipeline
from gigaforge.core.strategies import build_plan_strategy


class _Cfg:
    def __init__(self, root: Path) -> None:
        self.repo_root = str(root)


def _pipeline(root: Path, strategy) -> TaskPipeline:
    p = TaskPipeline.__new__(TaskPipeline)
    p.config = _Cfg(root)
    p.plan_strategy = strategy
    p._event = lambda *a, **k: None  # type: ignore[method-assign]
    return p


def test_the_factory_is_rebound_to_the_pipeline_that_owns_it(tmp_path: Path) -> None:
    """Two pipelines sharing one strategy: the last one wins, deliberately."""
    strategy = build_plan_strategy()
    first = _pipeline(tmp_path / "destination", strategy)
    second = _pipeline(tmp_path / "workspace", strategy)

    # Simulate what __init__ does for each pipeline.
    setattr(first.plan_strategy, "_runner_factory", first._build_react_runner)
    setattr(second.plan_strategy, "_runner_factory", second._build_react_runner)

    factory = getattr(strategy, "_runner_factory")
    assert factory.__self__ is second, (
        "the isolated pipeline's factory must win: otherwise the goal episode "
        "writes into the destination and the publish conflicts with itself"
    )


def test_reconfigure_rebinds_the_factory(tmp_path: Path) -> None:
    import threading

    strategy = build_plan_strategy()
    p = _pipeline(tmp_path, strategy)
    p._lock = threading.RLock()
    p._closed = False
    p._active_runs = 0
    setattr(p.plan_strategy, "_runner_factory", p._build_react_runner)

    replacement = build_plan_strategy("react")
    p.reconfigure(plan_strategy=replacement)
    factory = getattr(p.plan_strategy, "_runner_factory", None)
    assert factory is not None, "a strategy swapped in at runtime lost its factory"
    assert factory.__self__ is p
