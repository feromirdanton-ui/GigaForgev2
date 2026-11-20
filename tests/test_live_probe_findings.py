"""Two defects the live probe found in its own report.

Both are the same shape the whole review series is about — a field that exists
and that the product never populates — and both were visible only because the
probe prints what it measured instead of what it expected.
"""
from __future__ import annotations

from pathlib import Path

import pytest


def test_plan_used_is_populated_on_a_real_run(tmp_path: Path) -> None:
    """``plan_used`` must be true when the episode's plan reached the executor.

    The field was added to ``PipelineResult`` and to the ledger and then never
    written by the pipeline, so the live probe reported ``plan_used: false`` on
    a run whose plan had demonstrably been used.  A field that is always false
    is worse than no field: it reads as a measurement.
    """
    from tests.test_goal_to_diff import _pipeline, _repo, _ScriptedProvider

    _repo(tmp_path)
    provider = _ScriptedProvider(
        react_script=[
            {"thought": "locate", "action": "grep", "action_input": "def add"},
            {"thought": "plan", "action": "finish",
             "action_input": {"steps": [
                 {"tool": "edit_function", "args": {"name": "add"}},
                 {"tool": "run_tests", "args": {}},
             ]}},
        ],
        diff=(
            "```diff\n--- a/math_utils.py\n+++ b/math_utils.py\n@@ -1,2 +1,2 @@\n"
            " def add(a: int, b: int) -> int:\n-    return a - b\n+    return a + b\n```\n"
        ),
    )
    result = _pipeline(tmp_path, provider).run("the arithmetic helper is wrong")

    assert result.plan_used is True, "the plan reached the coder but was not recorded"
    assert result.plan_steps_executed >= 1


def test_a_linear_fallback_is_not_recorded_as_a_used_plan(tmp_path: Path) -> None:
    """A fallback to the linear plan must not claim the episode's plan was used."""
    from tests.test_goal_to_diff import _pipeline, _repo, _ScriptedProvider

    _repo(tmp_path)
    provider = _ScriptedProvider(
        react_script=[{"thought": "nothing", "action": "finish", "action_input": []}],
        diff="",
    )
    result = _pipeline(tmp_path, provider).run("the arithmetic helper is wrong")
    assert result.plan_used is False


@pytest.mark.parametrize(
    "thought",
    [
        "The function should be fixed",
        "This is the place to change",
        "There are several options here",
    ],
)
def test_localization_never_guesses_a_prose_word(thought: str) -> None:
    """A localisation guess must be code-shaped, not the first word of a sentence.

    The live probe reported ``target_function_guess: "The"`` — the first word of
    an English thought.  That guess is rendered into the coder's prompt as a
    target, so a wrong guess is worse than no guess.
    """
    from gigaforge.agents.react import ReActRunner

    loc = ReActRunner._extract_localization(None, [], thought)  # type: ignore[arg-type]
    assert "target_function_guess" not in loc, loc


def test_localization_accepts_a_code_shaped_mention() -> None:
    """A real symbol mention is still picked up."""
    from gigaforge.agents.react import ReActRunner

    loc = ReActRunner._extract_localization(
        None, [], "I should change def validate_email(x) to reject short TLDs"
    )  # type: ignore[arg-type]
    assert loc.get("target_function_guess") == "validate_email"


def test_the_repaired_files_are_reported_on_the_result(tmp_path: Path) -> None:
    """A repaired diff must be visible on the result, not only in a log line."""
    from tests.test_goal_to_diff import _pipeline, _repo, _ScriptedProvider

    _repo(tmp_path)
    provider = _ScriptedProvider(
        react_script=[{"thought": "done", "action": "finish", "action_input": []}],
        diff=(
            "```diff\n--- /dev/null\n+++ b/math_utils.py\n@@ -0,0 +1,2 @@\n"
            "+def add(a: int, b: int) -> int:\n+    return a + b\n```\n"
        ),
    )
    result = _pipeline(tmp_path, provider).run("the arithmetic helper is wrong")
    assert "math_utils.py" in result.diff_repaired, result.diff_repaired
    assert "/dev/null" not in (result.diff or ""), result.diff
