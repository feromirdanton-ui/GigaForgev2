"""The model emits two objects in one answer; the first one is the step.

Measured live on every run of the cyberpunk-site goal: the answer was

    {"thought": "...", "action": "write_file", "action_input": "{...}"}
    {"thought": "Now that I've created the HTML file...", "action": "finish", ...}

Parsing the whole text fails with "Extra data", so the step was counted as a
parse failure, the fallback force-finished after three of them, and the file was
never written — while the run reported success on a 3 KB stub.

The first balanced object is the step; the rest is the model thinking ahead.
Brace counting must be string-aware, or a step carrying a stylesheet is cut at
the first ``}`` inside its own CSS.
"""
from __future__ import annotations

import json

from gigaforge.agents.react import ReActRunner


def _parser() -> ReActRunner:
    return ReActRunner.__new__(ReActRunner)


def test_two_objects_in_one_answer_take_the_first() -> None:
    step = json.dumps(
        {
            "thought": "write it",
            "action": "write_file",
            "action_input": json.dumps({"path": "index.html", "content": "<html>x</html>"}),
        }
    )
    finish = json.dumps({"thought": "done", "action": "finish", "action_input": "[]"})
    parsed = _parser()._parse_step(step + "\n" + finish)
    assert parsed is not None, "a two-object answer was not parsed"
    _, action, ai = parsed
    assert action == "write_file"
    assert json.loads(ai)["path"] == "index.html"


def test_a_stylesheet_does_not_truncate_the_step() -> None:
    """A ``}`` inside the CSS must not close the outer object."""
    body = "<style>body{color:#0ff}.grid{display:grid}</style>"
    step = json.dumps(
        {
            "thought": "t",
            "action": "write_file",
            "action_input": json.dumps({"path": "a.html", "content": body}),
        }
    )
    parsed = _parser()._parse_step(step)
    assert parsed is not None
    assert json.loads(parsed[2])["content"] == body


def test_a_brace_inside_a_string_does_not_open_a_block() -> None:
    step = json.dumps(
        {
            "thought": "t",
            "action": "write_file",
            "action_input": json.dumps({"path": "a.html", "content": "a { b"}),
        }
    )
    assert _parser()._parse_step(step) is not None


def test_prose_without_an_object_is_still_a_failure() -> None:
    assert _parser()._parse_step("no json here at all") is None


def test_an_unbalanced_object_is_still_a_failure() -> None:
    assert _parser()._parse_step('{"thought": "t", "action": "write_file"') is None
