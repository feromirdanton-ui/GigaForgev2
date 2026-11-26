"""A URL in a problem statement is not a file the run must create.

Measured on SWE-bench ``pytest-dev__pytest-10051``: the issue text links to
``https://github.com/pytest-dev/pytest/blob/28e8c85.../src/_pytest/logging.py``.
The path regex matched the whole URL, the run treated
``github.com/pytest-dev/pytest/blob/.../logging.py`` as a required file, spent a
model call on it, hit a 429, and reported ``could not generate required file
github.com/...`` — a failure the harness invented out of a citation.

Every issue that links to its own source would have hit this, so the rule is
pinned here rather than left to the regex.
"""
from __future__ import annotations

from gigaforge.harness.task_kind import _named_paths


def test_a_github_blob_url_is_not_a_path():
    goal = (
        "The handler is at "
        "https://github.com/pytest-dev/pytest/blob/28e8c8582ea947704655a3c3f2d57184831336fd/"
        "src/_pytest/logging.py and it should not reset the level."
    )
    assert _named_paths(goal) == []


def test_a_plain_repo_path_is_still_found():
    goal = "Fix the bug in src/_pytest/logging.py so the level is restored."
    assert _named_paths(goal) == ["src/_pytest/logging.py"]


def test_a_url_and_a_real_path_together_keep_only_the_path():
    goal = (
        "See https://github.com/psf/requests/blob/main/requests/models.py for context; "
        "the change belongs in requests/sessions.py."
    )
    assert _named_paths(goal) == ["requests/sessions.py"]


def test_a_bare_domain_is_not_a_path():
    assert _named_paths("Reported at github.com/psf/requests/issues/1234") == []


def test_prose_abbreviations_are_not_paths():
    assert _named_paths("Use a helper, e.g. one that wraps the call.") == []
