from __future__ import annotations

from pathlib import Path

from gigaforge.desktop.qt_app import _search_project_files


def test_project_search_returns_only_indexed_safe_markdown_links(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "topic.md").write_text(
        "Orchid overview\nSee [related](related.md), [secret](../.env), "
        "and [unindexed](unindexed.md).\n",
        encoding="utf-8",
    )
    (docs / "related.md").write_text("Related notes\n", encoding="utf-8")
    (docs / "unindexed.md").write_text("Not in the project index\n", encoding="utf-8")
    (tmp_path / ".env").write_text("do not expose\n", encoding="utf-8")

    results = _search_project_files(
        tmp_path,
        [
            {"path": "docs/topic.md"},
            {"path": "docs/related.md"},
            {"path": ".env"},
        ],
        "orchid",
        limit=32,
        byte_limit=1024 * 1024,
    )

    assert [row["path"] for row in results] == ["docs/topic.md"]
    assert results[0]["related"] == ["docs/related.md"]


def test_project_search_skips_non_utf8_files(tmp_path: Path) -> None:
    invalid_file = tmp_path / "invalid.md"
    invalid_file.write_bytes(b"\xff\xfeorchid")

    results = _search_project_files(
        tmp_path,
        [{"path": "invalid.md"}],
        "orchid",
        limit=32,
        byte_limit=1024,
    )

    assert results == []
