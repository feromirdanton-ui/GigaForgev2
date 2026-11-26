"""A path copied from a traceback must resolve to the repository file.

Measured on ``django__django-10554``: the issue's traceback named
``/home/bbt/.virtualenvs/ispnext/lib/python3.6/site-packages/django/db/utils.py``,
the model copied that path into the diff header, and the applier tried to create
a directory tree named ``home/bbt/...`` inside the repository.  The run burned
its whole code budget on a path that could never exist.

The resolution is deterministic and refuses to guess: the longest tail that
exists wins, and a basename search only answers when exactly one file matches.
"""
from __future__ import annotations

from pathlib import Path

from gigaforge.firewall.diff import _resolve_by_suffix


def _repo(tmp_path: Path) -> Path:
    (tmp_path / "django" / "db").mkdir(parents=True)
    (tmp_path / "django" / "db" / "utils.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "django" / "db" / "models.py").write_text("y = 2\n", encoding="utf-8")
    return tmp_path


def test_installed_path_resolves_to_the_repo_file(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    got = _resolve_by_suffix(
        "home/bbt/.virtualenvs/ispnext/lib/python3.6/site-packages/django/db/utils.py",
        root,
    )
    assert got == "django/db/utils.py"


def test_a_real_repo_path_is_left_alone(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    assert _resolve_by_suffix("django/db/utils.py", root) == "django/db/utils.py"


def test_an_unknown_file_resolves_to_nothing(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    assert _resolve_by_suffix("home/x/site-packages/django/db/nope.py", root) == ""


def test_an_ambiguous_basename_is_not_guessed(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    (tmp_path / "other" / "db").mkdir(parents=True)
    (tmp_path / "other" / "db" / "utils.py").write_text("z = 3\n", encoding="utf-8")
    # Two files named db/utils.py: the exact-tail rule still resolves the real
    # path, but a path that only shares the basename must not be guessed.
    assert _resolve_by_suffix("django/db/utils.py", root) == "django/db/utils.py"
    assert _resolve_by_suffix("site-packages/db/utils.py", root) == ""
    assert _resolve_by_suffix("site-packages/zzz/utils.py", root) == ""
