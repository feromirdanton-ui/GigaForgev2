"""A task that is not a patch must not be forced through the patch pipeline.

**The defect this pins.**  The harness had one product: a unified diff.  Every
goal had to end in one, so a goal whose correct output is a sentence or a
diagnosis was either rejected ("diff could not be parsed") or died in the
planner.  Measured live:

* ``"What is 2+2? Answer in chat, do not change any files."`` \u2192 failed at
  ``plan`` with ``steps: 0``;
* ``"Analyse calc.py and tell me what is wrong with it"`` \u2192 classified as
  ``code`` because it names an existing file, then failed at ``plan`` with
  ``steps: 0`` after the model had already produced a useful answer.

The second is the interesting one: the answer dimension existed for chat and was
never extended to code, so "read this and tell me" was indistinguishable from
"change this".

The tests assert the classification and the plan, because those are the two
places the run died.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gigaforge.harness.task_kind import classify_task  # noqa: E402


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    (tmp_path / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "design.md").write_text("# Design\n")
    (tmp_path / "index.html").write_text("<html></html>\n")
    return tmp_path


def test_analysing_existing_code_is_not_a_patch(repo: Path) -> None:
    """"Analyse calc.py and tell me what is wrong" reads, it does not write."""
    kind = classify_task("Analyse calc.py and tell me what is wrong with it", repo)
    assert kind.kind == "analysis", kind
    assert kind.is_read_only
    assert kind.is_text_product


def test_analysing_with_a_mutating_verb_is_still_code(repo: Path) -> None:
    """The distinction is the verb, not the file: "fix" means write."""
    kind = classify_task("Analyse calc.py and fix the bug in it", repo)
    assert kind.kind == "code", kind
    assert not kind.is_read_only


def test_a_document_goal_is_its_own_class(repo: Path) -> None:
    """A .md deliverable is prose in a file, not a diff to grade."""
    kind = classify_task("Write a design document in docs/design.md", repo)
    assert kind.kind == "document", kind
    assert kind.is_document


def test_a_chat_question_is_still_an_answer(repo: Path) -> None:
    kind = classify_task("What is 2+2? Answer in chat, do not change any files.", repo)
    assert kind.kind == "answer", kind
    assert kind.is_text_product


def test_an_html_edit_is_still_an_artifact_edit(repo: Path) -> None:
    """The new classes must not swallow the existing ones."""
    kind = classify_task("Update index.html to add a footer", repo)
    assert kind.kind == "artifact_edit", kind


def test_the_plan_for_a_text_product_has_one_step_and_no_target_file() -> None:
    """The plan the run died on: ``steps: 0``.

    A text product's plan is the sentence that says what to produce.  It has no
    executable step with a target file because there is no file to target, and
    the planner used to treat that as a failure.
    """
    from gigaforge.core.pipeline import TaskPipeline

    pipeline = TaskPipeline.__new__(TaskPipeline)
    pipeline._event = lambda *a, **k: None  # type: ignore[method-assign]
    pipeline.config = type("C", (), {"repo_root": ""})()  # type: ignore[attr-defined]

    class _Ctx:
        run_id = "r1"
        extras: dict = {}

        class state:  # noqa: N801
            goal = "Analyse calc.py and tell me what is wrong with it"

    ctx = _Ctx()
    ctx.extras["task_kind"] = {"kind": "analysis"}
    out = pipeline._node_plan(ctx, {})
    assert out["plan_meta"]["steps"] == 1
    assert out["plan_meta"]["read_only"] is True
    assert "report what is wrong" in out["plan"][0]
