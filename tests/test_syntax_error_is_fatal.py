"""A file that does not parse is fatal, and it has no call graph.

Two defects, both measured on ``psf__requests-1142``:

* ``invalid-syntax: Expected an indented block after 'if' statement`` was
  **accepted**.  ``ruff_fatal_select`` was ``("E9", "F82", "WEB")`` and matches
  by prefix; ruff renamed the syntax-code from ``E999`` to ``invalid-syntax``,
  and nothing in that tuple matches the new name.  A syntactically broken diff
  stopped being fatal the day ruff was upgraded.
* The rejected diff then reported ``3 undefined symbol(s): E501: Line too long;
  W293: Blank line contains whitespace; invalid-syntax: Expected an indented
  block``.  tree-sitter recovers from a syntax error by reading whatever tokens
  it can find, and on a broken file those "calls" are garbage.  The repair loop
  was sent after function names that were lint codes.

Both are harness defects.  The model wrote a broken file; the harness called it
fine and then described it with invented names.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gigaforge.firewall.config import FirewallConfig  # noqa: E402
from gigaforge.firewall.validator import _is_fatal  # noqa: E402


def test_invalid_syntax_is_fatal() -> None:
    """The name ruff uses now, not the name it used before."""
    config = FirewallConfig()
    assert _is_fatal("invalid-syntax", config.ruff_fatal_select)


def test_the_old_syntax_code_is_still_fatal() -> None:
    """An older ruff in CI reports E999; both names must be covered."""
    config = FirewallConfig()
    assert _is_fatal("E999", config.ruff_fatal_select)


def test_a_style_warning_is_not_fatal() -> None:
    """The positive control: a long line must not fail a run."""
    config = FirewallConfig()
    for code in ("E501", "W293", "F401"):
        assert not _is_fatal(code, config.ruff_fatal_select), code


@pytest.mark.skipif(
    __import__("importlib").util.find_spec("tree_sitter") is None,
    reason="tree-sitter is not installed",
)
def test_a_broken_file_reports_no_undefined_symbols(tmp_path: Path) -> None:
    """Names must come from a real parse, not from tree-sitter's recovery."""
    from gigaforge.firewall.validator import DiffValidator
    from gigaforge.l0.config import L0Config
    from gigaforge.l0.store import L0Store

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "mod.py").write_text("def existing():\n    return 1\n", encoding="utf-8")
    config = FirewallConfig(repo_root=str(repo))
    store = L0Store(config=L0Config(db_path=str(tmp_path / "l0.db"), repo_root=str(repo)))
    try:
        validator = DiffValidator(store, config=config)
        broken = "def f(\n    if True\n    return E501\n"
        parsed = validator.parser.parse_source(broken, "mod.py")
        missing = validator._missing_symbols("mod.py", broken, parsed)
        assert missing == [], (
            "a file that does not parse must not be reported as calling "
            f"undefined names; got {missing}"
        )
    finally:
        store.close()


@pytest.mark.skipif(
    __import__("importlib").util.find_spec("tree_sitter") is None,
    reason="tree-sitter is not installed",
)
def test_a_valid_file_still_reports_a_real_undefined_name(tmp_path: Path) -> None:
    """The positive control: the check must still catch the case it exists for."""
    from gigaforge.firewall.validator import DiffValidator
    from gigaforge.l0.config import L0Config
    from gigaforge.l0.store import L0Store

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "mod.py").write_text("def existing():\n    return 1\n", encoding="utf-8")
    config = FirewallConfig(repo_root=str(repo))
    store = L0Store(config=L0Config(db_path=str(tmp_path / "l0.db"), repo_root=str(repo)))
    try:
        validator = DiffValidator(store, config=config)
        valid = "def f():\n    return never_defined_anywhere()\n"
        parsed = validator.parser.parse_source(valid, "mod.py")
        missing = validator._missing_symbols("mod.py", valid, parsed)
        assert "never_defined_anywhere" in missing
    finally:
        store.close()
