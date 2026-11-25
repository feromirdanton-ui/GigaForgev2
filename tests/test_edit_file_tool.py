from __future__ import annotations

import asyncio
import json
from pathlib import Path

from gigaforge.agents.react import EditFileTool, ReActContext, ToolRegistry


def _edit(tool: EditFileTool, root: Path, payload: dict[str, object]) -> str:
    ctx = ReActContext(repo_root=root, worktree=root, index=None, sandbox=None)
    return asyncio.run(tool.run(json.dumps(payload), ctx=ctx))


def test_replaces_a_unique_fragment_and_preserves_surrounding_content(
    tmp_path: Path,
) -> None:
    path = tmp_path / "index.html"
    path.write_text("<header>Old</header>\n<main>Keep me</main>", encoding="utf-8")

    result = _edit(
        EditFileTool(),
        tmp_path,
        {
            "path": "index.html",
            "old_string": "<header>Old</header>",
            "new_string": "<header>New</header>",
        },
    )

    assert result.startswith("updated index.html")
    assert path.read_text(encoding="utf-8") == "<header>New</header>\n<main>Keep me</main>"


def test_rejects_missing_or_ambiguous_fragments(tmp_path: Path) -> None:
    path = tmp_path / "notes.txt"
    path.write_text("same\nsame", encoding="utf-8")
    tool = EditFileTool()

    ambiguous = _edit(
        tool,
        tmp_path,
        {"path": "notes.txt", "old_string": "same", "new_string": "changed"},
    )
    missing = _edit(
        tool,
        tmp_path,
        {"path": "notes.txt", "old_string": "absent", "new_string": "changed"},
    )

    assert "occurs 2 times" in ambiguous
    assert "old_string not found" in missing
    assert path.read_text(encoding="utf-8") == "same\nsame"


def test_refuses_paths_outside_the_worktree(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside.txt"
    outside.write_text("private", encoding="utf-8")
    try:
        result = _edit(
            EditFileTool(),
            tmp_path,
            {
                "path": f"..\\{outside.name}",
                "old_string": "private",
                "new_string": "changed",
            },
        )
        assert "refusing to edit outside the worktree" in result
        assert outside.read_text(encoding="utf-8") == "private"
    finally:
        outside.unlink(missing_ok=True)


def test_edit_file_is_registered_by_default() -> None:
    registry = ToolRegistry()
    registry.install_defaults(sandbox=object())

    assert isinstance(registry.get("edit_file"), EditFileTool)
