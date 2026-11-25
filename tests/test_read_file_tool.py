from __future__ import annotations

import asyncio
from pathlib import Path

from gigaforge.agents.observation_cache import CACHEABLE_TOOLS, READ_ONLY_TOOLS
from gigaforge.agents.react import ReadFileTool, ReActContext, ToolRegistry


def _read(tool: ReadFileTool, root: Path, path: str) -> str:
    ctx = ReActContext(repo_root=root.resolve(), worktree=root.resolve(), index=None, sandbox=None)
    return asyncio.run(tool.run(path, ctx=ctx))


def test_reads_existing_text_file_without_reformatting(tmp_path: Path) -> None:
    content = "<main><h1>Existing page</h1></main>\n"
    (tmp_path / "index.html").write_text(content, encoding="utf-8")

    assert _read(ReadFileTool(), tmp_path, "index.html") == content


def test_read_is_bounded_and_reports_truncation(tmp_path: Path) -> None:
    tool = ReadFileTool()
    tool.max_chars = 8
    (tmp_path / "notes.txt").write_text("0123456789", encoding="utf-8")

    result = _read(tool, tmp_path, "notes.txt")

    assert result.startswith("01234567")
    assert "showing 8 of 10 characters" in result


def test_refuses_paths_outside_the_worktree(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside.txt"
    outside.write_text("private", encoding="utf-8")
    try:
        result = _read(ReadFileTool(), tmp_path, f"..\\{outside.name}")
        assert "refusing to read outside the worktree" in result
    finally:
        outside.unlink(missing_ok=True)


def test_read_file_is_registered_and_read_only() -> None:
    registry = ToolRegistry()
    registry.install_defaults(sandbox=object())

    assert registry.get("read_file") is not None
    assert "read_file" in READ_ONLY_TOOLS
    assert "read_file" in CACHEABLE_TOOLS
