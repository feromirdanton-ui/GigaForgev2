"""The brief: facts the harness measures so the model does not guess.

**The defect this file exists for.**  Every live run paid tokens for the model
to work out things the harness already knew or could measure in microseconds:
whether the file the goal names exists, what language the repository is in,
which test command it uses, whether the goal forbids changes.  The model
guessed, and a guess about the tree is the most expensive kind — a wrong "the
file exists" produces an edit to a file that is not there.

The rule these tests pin: **every line of a brief is a measurement**.  A fact
the harness could not observe is absent, not guessed.  A brief that says
"probably a Python project" is a guess wearing a fact's clothes, and it is
worse than no brief because the model trusts it.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from gigaforge.harness.brief import TaskBrief, build_brief, render_brief


# ── named paths are resolved, not assumed ───────────────────────────────────
def test_an_existing_file_is_measured(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("def add(a, b):\n    return a + b\n")
    brief = build_brief("fix app.py", repo_root=tmp_path)
    named = {p.path: p for p in brief.named_paths}
    assert named["app.py"].exists
    assert named["app.py"].lines == 2
    assert named["app.py"].language == "Python"
    assert named["app.py"].size_bytes > 0


def test_a_missing_file_is_reported_as_missing(tmp_path: Path) -> None:
    """The model must be told to create it, not to edit it."""
    brief = build_brief("fix missing.py", repo_root=tmp_path)
    named = {p.path: p for p in brief.named_paths}
    assert not named["missing.py"].exists
    assert "must create" in named["missing.py"].describe()


def test_a_directory_is_not_reported_as_a_file(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    brief = build_brief("look at src", repo_root=tmp_path)
    named = {p.path: p for p in brief.named_paths}
    assert named["src"].is_dir
    assert "directory" in named["src"].describe()


def test_named_paths_are_deduplicated(tmp_path: Path) -> None:
    brief = build_brief("fix app.py and also app.py again", repo_root=tmp_path)
    assert [p.path for p in brief.named_paths].count("app.py") == 1


def test_named_paths_are_bounded(tmp_path: Path) -> None:
    """A brief that grows with the goal is a second repo map."""
    goal = " ".join(f"f{i}.py" for i in range(50))
    brief = build_brief(goal, repo_root=tmp_path, max_paths=5)
    assert len(brief.named_paths) <= 5


# ── the repository shape ────────────────────────────────────────────────────
def test_the_language_mix_is_measured(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x = 1\n")
    (tmp_path / "b.py").write_text("y = 2\n")
    (tmp_path / "c.ts").write_text("let z = 3\n")
    brief = build_brief("do something", repo_root=tmp_path)
    languages = dict(brief.languages)
    assert languages.get("Python") == 2
    assert languages.get("TypeScript") == 1


def test_vendored_directories_are_not_counted(tmp_path: Path) -> None:
    """node_modules and .venv are not the project's shape."""
    (tmp_path / "app.py").write_text("x = 1\n")
    for junk in ("node_modules", ".venv", "__pycache__"):
        (tmp_path / junk).mkdir()
        (tmp_path / junk / "dep.py").write_text("y = 2\n")
    brief = build_brief("do something", repo_root=tmp_path)
    assert brief.file_count == 1


# ── the test command ────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "marker,expected",
    [
        ("pyproject.toml", "pytest"),
        ("package.json", "npm test"),
        ("Cargo.toml", "cargo test"),
        ("go.mod", "go test ./..."),
    ],
)
def test_the_test_command_is_detected(tmp_path: Path, marker: str, expected: str) -> None:
    (tmp_path / marker).write_text("{}\n")
    assert build_brief("do something", repo_root=tmp_path).test_command == expected


def test_no_marker_means_no_command(tmp_path: Path) -> None:
    """An absent fact is absent, not guessed."""
    assert build_brief("do something", repo_root=tmp_path).test_command == ""


# ── the constraints the goal states ─────────────────────────────────────────
@pytest.mark.parametrize(
    "goal",
    [
        "What is 2+2? Answer in chat, do not change any files",
        "Explain the parser without changing anything",
        "Read-only review of app.py",
        "Just answer: how many tests are there?",
    ],
)
def test_a_read_only_goal_is_flagged(goal: str) -> None:
    assert build_brief(goal).read_only


def test_a_mutating_goal_is_not_flagged_read_only() -> None:
    assert not build_brief("Add a footer to index.html").read_only


def test_an_answer_goal_is_flagged() -> None:
    brief = build_brief("what is 2+2", task_kind="answer")
    assert brief.wants_answer
    assert any("reply" in note for note in brief.notes)


# ── rendering ───────────────────────────────────────────────────────────────
def test_an_empty_brief_renders_to_nothing() -> None:
    """An empty section in a system prompt is tokens spent to say nothing."""
    assert render_brief(TaskBrief()) == ""


def test_the_rendered_brief_states_the_class_and_the_paths(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("x = 1\n")
    brief = build_brief(
        "fix app.py", repo_root=tmp_path, task_kind="code", task_evidence="path exists"
    )
    text = render_brief(brief)
    assert "**code**" in text
    assert "path exists" in text
    assert "`app.py`" in text
    assert "pytest" not in text  # no marker in this tmp tree


def test_the_rendered_brief_does_not_invent_a_test_command(tmp_path: Path) -> None:
    text = render_brief(build_brief("fix app.py", repo_root=tmp_path))
    assert "Test command" not in text


def test_the_brief_is_serialisable(tmp_path: Path) -> None:
    """The ledger and the report read the dict form."""
    (tmp_path / "app.py").write_text("x = 1\n")
    payload = build_brief("fix app.py", repo_root=tmp_path, task_kind="code").to_dict()
    assert payload["task_kind"] == "code"
    assert payload["named_paths"][0]["path"] == "app.py"
    assert payload["named_paths"][0]["exists"] is True


# ── no tree, no guesses ─────────────────────────────────────────────────────
def test_without_a_tree_paths_are_reported_as_absent() -> None:
    """No tree to check means the harness does not claim the file exists."""
    brief = build_brief("fix app.py", repo_root=None)
    assert brief.named_paths
    assert all(not p.exists for p in brief.named_paths)
    assert brief.file_count == 0
