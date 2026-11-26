"""A task that is not a patch must not be forced through the patch pipeline.

**The defect this pins.**  Every run had to end in a unified diff.  A goal whose
correct output is a sentence or a diagnosis was rejected ("diff could not be
parsed"), or died in the planner demanding "an executable plan" — because the
planner asked for a step naming an existing file, and a diagnosis has no such
step.  Measured live: ``"What is 2+2? Answer in chat, do not change any files."``
failed at ``plan`` with ``steps: 0``.

The fix is a task-kind dimension, not a special case: ``answer`` (a reply),
``analysis`` (a read-only diagnosis of named code) and ``document`` (prose in a
file) are products in their own right, and the plan for each is a sentence
rather than a list of edits.

These tests assert the *routing* — which pipeline branch a goal reaches — because
the bug was that every goal reached the same branch.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gigaforge.harness.task_kind import classify_task  # noqa: E402


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    (tmp_path / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n",
        encoding="utf-8",
    )
    (tmp_path / "index.html").write_text(
        "<!doctype html><html><body><h1>Hi</h1></body></html>", encoding="utf-8"
    )
    return tmp_path


def test_a_chat_question_is_an_answer_not_a_patch() -> None:
    kind = classify_task("What is 2+2? Answer in chat, do not change any files.", None)
    assert kind.kind == "answer"
    assert kind.is_text_product
    assert kind.is_read_only


def test_a_diagnosis_of_named_code_is_analysis(repo: Path) -> None:
    """Naming an existing file used to mean "code", and code means "patch".

    "Analyse calc.py and tell me what is wrong with it" names a file, so it
    classified as code; the planner then demanded an executable step with a
    target file, and there is none — the correct output is a diagnosis.

    The root matters: without it the classifier cannot see that the file exists
    and the goal falls through to the generic answer branch.  A test that omits
    it would prove the wrong thing.
    """
    kind = classify_task(
        "Analyse calc.py and tell me what is wrong with it.", repo
    )
    assert kind.kind == "analysis", kind.evidence
    assert kind.is_analysis
    assert kind.is_read_only
    assert kind.is_text_product


def test_a_fix_of_the_same_file_is_still_code(repo: Path) -> None:
    """The distinction is the verb, not the file: "fix" still means "patch"."""
    kind = classify_task("Fix the bug in calc.py so mul returns the product.", repo)
    assert kind.kind == "code"
    assert not kind.is_read_only


def test_a_document_goal_is_its_own_class() -> None:
    """A document's deliverable is its text, not a patch to it."""
    kind = classify_task("Write a design document in docs/design.md", None)
    assert kind.kind == "document", kind.evidence
    assert kind.is_document


def test_a_page_goal_is_still_an_artifact(repo: Path) -> None:
    kind = classify_task("Add a footer to index.html", repo)
    assert kind.is_artifact
    assert not kind.is_text_product


def test_every_class_is_reachable(repo: Path) -> None:
    """No class is dead: each is the best answer to some real goal."""
    reachable = {
        classify_task("What is 2+2? Answer in chat.", repo).kind,
        classify_task("Write a design document in docs/x.md", repo).kind,
        classify_task("Explain what is wrong in calc.py", repo).kind,
    }
    assert {"answer", "document", "analysis"} <= reachable
