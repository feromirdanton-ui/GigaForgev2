"""The brief must see the tree the run writes to.

**The defect this file exists for.**  ``_build_prefix`` built the task brief
with::

    repo_root=getattr(ctx, "worktree", None) or getattr(self, "repo_root", None)

``RunContext`` has no ``worktree`` attribute — it never did — and the pipeline
has no ``repo_root`` attribute either (it has ``_repo_root()``).  So the brief
was always built against ``None``, which means "no tree": no named-path
resolution, no file count, no test command, and no grounding.

Measured live on a document goal about the decision ladder:

* the trace showed a **2 742-character** prefix where the brief and the skills
  should have been (the same goal builds a 9 514-character prefix when the tree
  is passed);
* the model invented a completely different ladder, because the source it was
  supposed to be describing was never quoted.

The failure was silent in the worst way: every ``except`` in ``_build_prefix``
is a ``noqa: BLE001`` that records a degradation, and there was no exception —
``build_brief`` happily returned a brief about nothing.

These tests pin the wiring, not the brief's internals: the prefix builder must
pass a real root, and the resulting prefix must contain the measured facts.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from gigaforge.core.pipeline import TaskPipeline
from gigaforge.core.state import TaskState


class _Ctx:
    """The minimum a prefix build touches."""

    def __init__(self, task: str, worktree: str = "") -> None:
        self.task = task
        self.state = TaskState(goal=task, task_id="t")
        self.run_id = "t"
        self.extras: dict = {}
        self.worktree = worktree


def _pipeline(root: Path) -> TaskPipeline:
    pipeline = TaskPipeline.__new__(TaskPipeline)
    pipeline.metrics = None
    pipeline.indexer = None
    pipeline.firewall = None
    pipeline.config = type("C", (), {"repo_root": str(root)})()
    pipeline._event = lambda *a, **k: None  # type: ignore[method-assign]
    return pipeline


@pytest.fixture()
def tree(tmp_path):
    (tmp_path / "gigaforge" / "core").mkdir(parents=True)
    (tmp_path / "gigaforge" / "core" / "ladder.py").write_text(
        '"""The decision ladder: L0 deterministic, L1 cheap, L2 strong, L3 flagship."""\n'
        "L0_DETERMINISTIC = 0\n"
    )
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    return tmp_path


def test_the_prefix_contains_the_measured_brief(tree) -> None:
    """A brief about nothing is the defect; a brief with facts is the fix."""
    ctx = _Ctx("Write a design note to docs/design.md explaining the decision ladder")
    _pipeline(tree)._build_prefix(ctx)
    text = ctx.state.prefix.text
    assert "## Task brief" in text
    assert "Repository:" in text


def test_the_prefix_quotes_the_module_the_goal_names(tree) -> None:
    """The live case: the ladder was invented because it was never quoted."""
    ctx = _Ctx("Write a design note to docs/design.md explaining the decision ladder")
    _pipeline(tree)._build_prefix(ctx)
    assert "L0_DETERMINISTIC" in ctx.state.prefix.text


def test_the_prefix_contains_the_selected_skills(tree) -> None:
    ctx = _Ctx("Write a design note to docs/design.md explaining the decision ladder")
    _pipeline(tree)._build_prefix(ctx)
    assert "## Skill:" in ctx.state.prefix.text


def test_the_prefix_is_substantially_larger_than_the_bare_persona(tree) -> None:
    """The measured symptom: 2 742 chars where ~9 500 were expected."""
    ctx = _Ctx("Write a design note to docs/design.md explaining the decision ladder")
    _pipeline(tree)._build_prefix(ctx)
    assert len(ctx.state.prefix.text) > 3_000


def test_a_ctx_without_a_worktree_attribute_still_works(tree) -> None:
    """``RunContext`` has no ``worktree``; the builder must not depend on one."""

    class _Bare:
        def __init__(self) -> None:
            self.task = "Write a note to docs/design.md about the decision ladder"
            self.state = TaskState(goal=self.task, task_id="t")
            self.run_id = "t"
            self.extras: dict = {}

    ctx = _Bare()
    _pipeline(tree)._build_prefix(ctx)
    assert "## Task brief" in ctx.state.prefix.text
