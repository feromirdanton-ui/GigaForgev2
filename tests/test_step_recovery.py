"""A step whose outer JSON is broken by its own payload must still be recovered.

Measured live, four times in a row: a ``write_file`` step carrying 1.4 KB of
HTML with quotes and newlines.  The outer object failed to parse, the episode
counted a parse failure, and after three of them the fallback force-finished
with nothing written — the run then reported success on an empty artifact.

The shape is recoverable because the action name and the path are short and
unambiguous, and the body is "everything after the content key".  Recovering it
is a computation, so the harness does it instead of asking the model to escape
better.
"""
from __future__ import annotations

import json

from gigaforge.agents.react import ReActRunner


def _parser() -> ReActRunner:
    return ReActRunner.__new__(ReActRunner)


def test_a_clean_write_step_parses() -> None:
    payload = json.dumps({"path": "a.html", "content": "<html>x</html>"})
    text = json.dumps({"thought": "t", "action": "write_file", "action_input": payload})
    parsed = _parser()._parse_step(text)
    assert parsed is not None
    thought, action, ai = parsed
    assert action == "write_file"
    assert json.loads(ai)["path"] == "a.html"


def test_a_write_step_broken_by_its_own_html_is_recovered() -> None:
    """The live failure: unescaped quotes inside the body break the outer JSON."""
    body = '<!DOCTYPE html>\n<html lang="en">\n<body class="neon">hi</body>\n</html>'
    # Deliberately malformed: the body's quotes are not escaped for the outer
    # object, which is exactly what the model emitted.
    text = (
        '{"thought": "write the page", "action": "write_file", "action_input": '
        '{"path": "index.html", "content": "' + body + '"}}'
    )
    parsed = _parser()._parse_step(text)
    assert parsed is not None, "a write step broken by its own HTML was not recovered"
    _, action, ai = parsed
    assert action == "write_file"
    payload = json.loads(ai)
    assert payload["path"] == "index.html"
    assert "<html" in payload["content"]
    assert "neon" in payload["content"]


def test_prose_is_still_a_parse_failure() -> None:
    """The recovery must not turn arbitrary text into a tool call."""
    assert _parser()._parse_step("I think the answer is in parser.py.") is None


def test_a_broken_non_write_step_is_not_recovered() -> None:
    """Only the write shape is recovered; a broken grep stays a failure."""
    text = '{"thought": "t", "action": "grep", "action_input": {"pattern": "a"b"}}'
    assert _parser()._parse_step(text) is None


def test_a_write_step_without_a_path_is_not_recovered() -> None:
    text = '{"thought": "t", "action": "write_file", "action_input": {"content": "a"b"}}'
    assert _parser()._parse_step(text) is None
