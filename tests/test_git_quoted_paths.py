"""A path git quotes must be read back as the path git meant.

**The defect this pins.**  ``git diff`` quotes any path with a non-ASCII byte
and writes each byte as an octal escape::

    diff --git "a/\\321\\201.html" "b/\\321\\201.html"

``split_diff`` took that token literally, so the path came out as
``"b//321/201.html"`` — quotes kept, backslashes turned into slashes.  The file
was then unrecognised, ``_drop_hunks_for`` did not drop its hunk, and
``final_diff`` described a file that was not in the tree.

The test builds a real repository and takes a real ``git diff``, because the
quoting is git's behaviour and a hand-written fixture would only prove that the
parser agrees with the fixture.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gigaforge.harness.diff_parse import (  # noqa: E402
    normalise_path,
    paths_in,
    split_diff,
    unquote_git_path,
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q", ".")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    return tmp_path


def test_a_cyrillic_path_survives_a_real_git_diff(repo: Path) -> None:
    target = repo / "страница.html"
    target.write_text("old\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "init")
    target.write_text("new\n", encoding="utf-8")

    diff = _git(repo, "diff")
    # The premise: git really did quote it.  Without this the test would pass
    # on a git that stopped quoting and prove nothing.
    assert '"a/\\' in diff, diff

    assert paths_in(diff) == ["страница.html"]
    blocks = split_diff(diff)
    assert len(blocks) == 1
    assert blocks[0].path == "страница.html"
    assert "+new" in blocks[0].text


def test_a_path_with_a_space_survives_a_real_git_diff(repo: Path) -> None:
    target = repo / "my page.html"
    target.write_text("old\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "init")
    target.write_text("new\n", encoding="utf-8")

    diff = _git(repo, "diff")
    assert paths_in(diff) == ["my page.html"]


def test_two_quoted_files_are_two_blocks(repo: Path) -> None:
    for name in ("страница.html", "другая.html"):
        (repo / name).write_text("old\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "init")
    for name in ("страница.html", "другая.html"):
        (repo / name).write_text("new\n", encoding="utf-8")

    diff = _git(repo, "diff")
    assert sorted(paths_in(diff)) == ["другая.html", "страница.html"]
    assert len(split_diff(diff)) == 2


def test_unquote_is_byte_exact_for_multibyte_characters() -> None:
    # "я" is two bytes, so it is two octal escapes; decoding them one at a time
    # would produce two replacement characters.
    assert unquote_git_path('"a/\\321\\217.html"') == "a/я.html"


def test_unquote_leaves_an_unquoted_path_alone() -> None:
    assert unquote_git_path("a/index.html") == "a/index.html"
    assert unquote_git_path("") == ""


def test_unquote_handles_the_escapes_git_actually_emits() -> None:
    assert unquote_git_path('"a/tab\\there"') == "a/tab\there"
    assert unquote_git_path('"a/quote\\"here"') == 'a/quote"here'
    assert unquote_git_path('"a/back\\\\slash"') == "a/back\\slash"


def test_normalise_strips_the_prefix_after_unquoting() -> None:
    assert normalise_path('"b/\\321\\201.html"') == "с.html"
