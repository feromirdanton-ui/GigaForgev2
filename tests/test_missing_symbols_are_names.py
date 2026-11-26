"""An "undefined symbol" must be a name, never a lint code.

Measured on ``psf__requests-1142`` the rejection read:

    2 undefined symbol(s): E501: Line too long (108 > 100); E501: Line too long
    (102 > 100); E713: Test for membership should be `not in`; F632: Use `==` to
    compare constant literals

Those are ruff codes.  ``parsed.calls`` comes from a tree-sitter walk, and the
tokens its recovery pass salvages from a broken file look like calls.  The
repair loop was then sent after function names that are lint diagnostics.

Python has an exact answer, so the check now uses ``ast``.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gigaforge.firewall.validator import DiffValidator  # noqa: E402


def test_a_free_call_is_reported() -> None:
    calls = DiffValidator._python_free_calls("def f():\n    return helper()\n")
    assert calls is not None
    assert ("helper", False) in calls


def test_a_method_call_is_marked_as_such() -> None:
    """``obj.run()`` is never judged: the receiver's type is unknowable."""
    calls = DiffValidator._python_free_calls("def f(o):\n    return o.run()\n")
    assert calls is not None
    assert ("run", True) in calls
    assert ("run", False) not in calls


def test_a_broken_file_yields_no_calls_at_all() -> None:
    """``ast`` either parses or it does not; it cannot invent a call."""
    assert DiffValidator._python_free_calls("def f(:\n    return E501\n") is None


def test_an_unbound_name_in_a_function_body_is_found() -> None:
    """The case the check exists for: a free function called but never defined."""
    calls = DiffValidator._python_free_calls(
        "def f():\n    return never_defined_anywhere(1)\n"
    )
    assert calls is not None
    assert ("never_defined_anywhere", False) in calls


def test_lint_codes_never_appear_as_calls() -> None:
    """The instruction not to reason about this one: the shape is impossible."""
    calls = DiffValidator._python_free_calls(
        "import os\n\n\ndef f():\n    x = 1\n    return x\n"
    ) or []
    names = {name for name, _ in calls}
    assert not any(n.startswith(("E5", "E7", "F6", "W2")) for n in names), names


def test_a_non_python_file_falls_back_to_the_other_walk() -> None:
    """``None`` means "not my language", not "no calls"."""
    assert DiffValidator._python_free_calls("function f() { return 1; }") is None


def test_empty_text_falls_back_rather_than_crashing() -> None:
    assert DiffValidator._python_free_calls("") is None


def test_a_python_file_that_does_not_parse_reports_no_symbols() -> None:
    """The fallback is for other languages, not for broken Python.

    Measured on psf__requests-1142: the file did not parse, ``ast`` returned
    None, the code fell back to the tree-sitter walk, and the rejection read
    "2 undefined symbol(s): E501: Line too long; invalid-syntax: Unexpected
    indentation".  A Python file that does not parse has no call graph.
    """
    from gigaforge.firewall.config import FirewallConfig
    from gigaforge.l0.config import L0Config
    from gigaforge.l0.store import L0Store

    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "mod.py").write_text("def f():\n    return 1\n", encoding="utf-8")
        store = L0Store(config=L0Config(db_path=str(root / "l0.db"), repo_root=str(root)))
        try:
            validator = DiffValidator(store, config=FirewallConfig(repo_root=str(root)))
            broken = "def f():\n    x = 1\n        return E501\n"
            parsed = validator.parser.parse_source(broken, "mod.py")
            assert validator._missing_symbols("mod.py", broken, parsed) == []
        finally:
            store.close()
