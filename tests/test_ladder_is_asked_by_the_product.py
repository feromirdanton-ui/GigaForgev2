"""The ladder must be consulted by the product, not only by its own tests.

``decide_level`` was built, exported and covered — and called from nowhere in
``gigaforge/``.  Every phase therefore went to its configured model on the
first attempt, and a step that had already failed twice was retried on the
model that had just failed twice.  A ladder nobody climbs is a label.
"""
from __future__ import annotations

from gigaforge.core.ladder import Level


class _RecordingRouter:
    """A router that records the level it was asked to serve."""

    def __init__(self) -> None:
        self.levels: list[object] = []
        self.decided: list[str] = []

    def decide_level(self, phase, **kwargs):
        self.decided.append(phase)

        class _D:
            level = Level.L1_CHEAP
            reason = "test"

        return _D()

    def complete(self, phase, messages, *, level=None, **kwargs):
        self.levels.append(level)

        class _R:
            content = "ok"

        return _R()


def test_ask_passes_the_ladder_verdict_to_the_router() -> None:
    from gigaforge.core.pipeline import TaskPipeline
    from gigaforge.core.contracts import RunContext
    from gigaforge.core.state import TaskState

    pipeline = TaskPipeline.__new__(TaskPipeline)
    pipeline.router = _RecordingRouter()
    pipeline._prefixes = {}
    pipeline._event = lambda *a, **k: None
    pipeline._degraded = lambda *a, **k: None

    ctx = RunContext(run_id="r1", state=TaskState(goal="g"))
    pipeline._ask("code", "do it", ctx)

    assert pipeline.router.decided == ["code"]
    assert pipeline.router.levels == [Level.L1_CHEAP]
