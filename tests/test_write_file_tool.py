"""write_file is the harness's own way to produce a file.

The defect it closes: without a write tool the only way to create a file was to
embed its body in a plan step inside the ``finish`` payload — a JSON string
inside a JSON string inside a JSON string.  A live run asking for a cyberpunk
page produced 1.4 KB of HTML that had to survive three levels of escaping; it
did not, the payload failed to parse four times, the episode hit ``max_steps``
with an empty plan, and the run degraded to a one-shot linear answer.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from gigaforge.agents.react import ReActContext, WriteFileTool


def _ctx(root: Path) -> ReActContext:
    return ReActContext(repo_root=root, worktree=root, index=None, sandbox=None)


def _run(tool: WriteFileTool, payload: str, root: Path) -> str:
    return asyncio.run(tool.run(payload, ctx=_ctx(root)))


def test_writes_a_file_with_one_level_of_escaping(tmp_path: Path) -> None:
    body = "<!DOCTYPE html>\n<html>\n<body class=\"neon\">hi</body>\n</html>"
    payload = json.dumps({"path": "index.html", "content": body})
    out = _run(WriteFileTool(), payload, tmp_path)
    assert "created index.html" in out
    assert (tmp_path / "index.html").read_text() == body


def test_creates_parent_directories(tmp_path: Path) -> None:
    payload = json.dumps({"path": "site/css/main.css", "content": "body{}"})
    _run(WriteFileTool(), payload, tmp_path)
    assert (tmp_path / "site/css/main.css").read_text() == "body{}"


def test_reports_an_update_not_a_create(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("old")
    out = _run(WriteFileTool(), json.dumps({"path": "a.txt", "content": "new"}), tmp_path)
    assert "updated a.txt" in out
    assert (tmp_path / "a.txt").read_text() == "new"


def test_accepts_location_as_the_path_key(tmp_path: Path) -> None:
    """The model's own vocabulary — the same key the plan guard had to learn."""
    _run(WriteFileTool(), json.dumps({"location": "x.html", "content": "x"}), tmp_path)
    assert (tmp_path / "x.html").read_text() == "x"


def test_refuses_to_escape_the_worktree(tmp_path: Path) -> None:
    out = _run(WriteFileTool(), json.dumps({"path": "../escape.txt", "content": "x"}), tmp_path)
    assert "refusing" in out
    assert not (tmp_path.parent / "escape.txt").exists()


def test_a_missing_path_is_an_error_not_a_guess(tmp_path: Path) -> None:
    out = _run(WriteFileTool(), json.dumps({"content": "x"}), tmp_path)
    assert "needs a 'path'" in out


def test_a_missing_content_is_an_error(tmp_path: Path) -> None:
    out = _run(WriteFileTool(), json.dumps({"path": "a.txt"}), tmp_path)
    assert "needs 'content'" in out


def test_unparseable_input_explains_the_shape(tmp_path: Path) -> None:
    out = _run(WriteFileTool(), "not json at all", tmp_path)
    assert "ERROR" in out and "path" in out


def test_the_tool_is_registered_by_default() -> None:
    """A tool the model is told about but the registry lacks is worse than none."""
    from gigaforge.agents.react import ToolRegistry

    reg = ToolRegistry()
    reg.install_defaults(sandbox=None)
    assert "write_file" in reg.names()
