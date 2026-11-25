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

from gigaforge.agents.react import (
    EditFileTool,
    ReActContext,
    ReadFileTool,
    WriteFileTool,
    read_only_tool_error,
)


def _ctx(root: Path) -> ReActContext:
    return ReActContext(repo_root=root, worktree=root, index=None, sandbox=None)


def _run(
    tool: WriteFileTool | ReadFileTool,
    payload: str,
    root: Path,
    *,
    ctx: ReActContext | None = None,
) -> str:
    return asyncio.run(tool.run(payload, ctx=ctx or _ctx(root)))


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


def test_refuses_to_overwrite_an_existing_file_until_read(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    target.write_text("old")
    out = _run(WriteFileTool(), json.dumps({"path": "a.txt", "content": "new"}), tmp_path)

    assert "refusing to overwrite an existing file" in out
    assert target.read_text() == "old"


def test_truncated_read_does_not_authorize_full_replacement(tmp_path: Path) -> None:
    target = tmp_path / "large.txt"
    target.write_text("original content")
    ctx = _ctx(tmp_path)
    reader = ReadFileTool()
    reader.max_chars = 3

    observation = _run(reader, "large.txt", tmp_path, ctx=ctx)
    result = _run(
        WriteFileTool(),
        json.dumps({"path": "large.txt", "content": "replacement"}),
        tmp_path,
        ctx=ctx,
    )

    assert "[truncated:" in observation
    assert "refusing to overwrite an existing file" in result
    assert target.read_text() == "original content"


def test_rewrite_requires_reading_the_current_version(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    tool = WriteFileTool()
    _run(tool, json.dumps({"path": "a.txt", "content": "first"}), tmp_path, ctx=ctx)

    out = _run(
        tool,
        json.dumps({"path": "a.txt", "content": "second"}),
        tmp_path,
        ctx=ctx,
    )

    assert "refusing to overwrite an existing file" in out
    assert (tmp_path / "a.txt").read_text() == "first"


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


def test_read_only_planning_rejects_file_mutations(tmp_path: Path) -> None:
    target = tmp_path / "existing.py"
    target.write_text("value = 1\n", encoding="utf-8")
    ctx = _ctx(tmp_path)
    ctx.allow_file_writes = False

    write_result = asyncio.run(
        WriteFileTool().run(
            json.dumps({"path": "new.py", "content": "value = 2\n"}), ctx=ctx
        )
    )
    edit_result = asyncio.run(
        EditFileTool().run(
            json.dumps({
                "path": "existing.py",
                "old_string": "value = 1",
                "new_string": "value = 2",
            }),
            ctx=ctx,
        )
    )

    assert "disabled during read-only planning" in write_result
    assert "disabled during read-only planning" in edit_result
    assert not (tmp_path / "new.py").exists()
    assert target.read_text(encoding="utf-8") == "value = 1\n"


def test_read_only_policy_blocks_execution_tools_but_allows_observation_tools(
    tmp_path: Path,
) -> None:
    ctx = _ctx(tmp_path)
    ctx.allow_file_writes = False

    assert read_only_tool_error("read_file", ctx) is None
    assert read_only_tool_error("web_search", ctx) is None
    assert "disabled during read-only planning" in read_only_tool_error("repl", ctx)
    assert "disabled during read-only planning" in read_only_tool_error("run_tests", ctx)


def test_read_only_flag_is_included_in_the_langgraph_opening_prompt(
    tmp_path: Path,
) -> None:
    from types import SimpleNamespace

    from gigaforge.agents.react import ReActRunner, ToolRegistry

    ctx = _ctx(tmp_path)
    ctx.allow_file_writes = False
    runner = object.__new__(ReActRunner)
    runner.ctx = ctx
    runner._registry = ToolRegistry()
    runner._max_steps = 5

    prompt = runner.compose_initial_prompt(
        "Plan a change", SimpleNamespace(serialize_prefix=lambda: {})
    )

    assert "only grep, read_file, read_structure, and web_search are allowed" in prompt
    assert "repl, and run_tests are rejected" in prompt


def test_repl_does_not_execute_during_read_only_planning(tmp_path: Path) -> None:
    from gigaforge.agents.repl_tool import ReplTool

    ctx = _ctx(tmp_path)
    ctx.allow_file_writes = False

    result = asyncio.run(ReplTool().run("print('should not run')", ctx=ctx))

    assert "disabled during read-only planning" in result
    assert not (tmp_path / ".gigaforge_repl").exists()
