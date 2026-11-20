"""The finish payload must survive the shapes a real model actually emits.

Two defects are pinned here, both found by a live run against GigaChat and
neither visible to the offline suite:

1. ``json_repair`` extracts from the first ``{`` to the last ``}``, so a
   *list* payload that needed repair came back as a bare object — the wrapping
   ``[ ]`` was gone.  ``_parse_finish_plan`` then saw a dict, found no
   ``steps`` key, and returned ``[]``.  Measured live: the model returned a
   one-step plan for "build a cyberpunk site", the plan was discarded, and the
   run reported ``finish_action`` with zero steps while the trace still said
   the episode had succeeded.
2. The plan guard only recognised ``name``/``symbol``/``path``/``file``/
   ``target``/``function`` as an edit target.  The model wrote ``location``,
   the guard dropped the only step of the plan as "untargeted", and the run
   fell back to a linear plan.

Both are asserted through the real functions, not a reimplementation.
"""
from __future__ import annotations

import json

from gigaforge.agents.react import ReActRunner
from gigaforge.core.task_state import PlanStep
from gigaforge.harness.plan_guard import validate_plan


def _parser() -> ReActRunner:
    """A runner with no collaborators — only the pure parsers are exercised."""
    return ReActRunner.__new__(ReActRunner)


# ── the repair-collapses-a-list defect ───────────────────────────────────────
def test_a_clean_list_payload_parses() -> None:
    payload = json.dumps([{"tool": "edit_file", "args": {"path": "index.html"}}])
    steps = _parser()._parse_finish_plan(payload)
    assert [s.tool for s in steps] == ["edit_file"]


def test_a_list_payload_that_needs_repair_still_parses() -> None:
    """The regression: a trailing comma made ``json_repair`` drop the brackets.

    Without the fix this returns ``[]`` — the plan is silently thrown away.
    """
    payload = '[{"tool": "edit_file", "args": {"path": "index.html"}},'
    steps = _parser()._parse_finish_plan(payload)
    assert [s.tool for s in steps] == ["edit_file"], (
        "a repairable list payload lost its plan; json_repair collapsed the "
        "list to a bare object and the parser rejected it"
    )


def test_a_bare_object_payload_is_a_one_step_plan() -> None:
    """A dict that names a tool is a plan of one, not a parse failure."""
    payload = '{"tool": "edit_file", "args": {"path": "index.html"}}'
    steps = _parser()._parse_finish_plan(payload)
    assert [s.tool for s in steps] == ["edit_file"]


def test_a_double_encoded_payload_parses() -> None:
    inner = json.dumps([{"tool": "edit_file", "args": {"path": "index.html"}}])
    steps = _parser()._parse_finish_plan(json.dumps(inner))
    assert [s.tool for s in steps] == ["edit_file"]


def test_the_steps_envelope_still_parses() -> None:
    payload = json.dumps({"steps": [{"tool": "grep", "args": {"pattern": "x"}}]})
    steps = _parser()._parse_finish_plan(payload)
    assert [s.tool for s in steps] == ["grep"]


def test_prose_is_still_a_parse_failure() -> None:
    """The recovery must not turn every string into a plan."""
    assert _parser()._parse_finish_plan("I will create the file now.") == []


# ── the guard-drops-the-step defect ──────────────────────────────────────────
def test_the_guard_accepts_location_as_an_edit_target() -> None:
    """The regression: ``location`` was not a recognised target key.

    The model's own vocabulary is not the guard's business — the guard exists
    to reject an edit that names *nothing*.
    """
    steps = [PlanStep(tool="edit_file", args={"location": "index.html", "content": "<html>"})]
    verdict = validate_plan(steps, known_tools={"edit_file", "run_tests"})
    assert verdict.steps, (
        "the guard dropped an edit that names its target as 'location'"
    )
    assert not verdict.dropped_untargeted


def test_the_guard_still_rejects_an_edit_with_no_target() -> None:
    """The rule the guard exists for must survive the widened vocabulary."""
    steps = [PlanStep(tool="edit_file", args={"content": "<html>"})]
    verdict = validate_plan(steps, known_tools={"edit_file", "run_tests"})
    assert not verdict.steps
    assert verdict.dropped_untargeted == ["edit_file"]
