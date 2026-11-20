"""A code change is graded by a contract, not by "the tests passed".

**The gap.**  An artifact goal had a contract: the plan's sections, the palette,
the heading, the card count, the render.  A code goal had one check — the test
suite — and that check is both too weak and too strong:

* too weak: a diff that touches files the goal never named, deletes a public
  function, or leaves the module unparseable passes as long as some test is
  green (and a repo with no tests passes trivially);
* too strong: a correct change the existing tests do not cover is rejected for a
  reason that has nothing to do with the change.

The four facts a code change can be held to without guessing are the files it
touched, the tests, the linter, and the public symbols it kept.  Each is a fact
about the tree, not an opinion about the code.

The rule that matters most here: a check that cannot run is reported as **not
checked**, never as passed.  A repo with no linter is not a clean repo.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from gigaforge.harness.code_contract import (
    CodeContract,
    check_code_contract,
    public_symbols,
)


class TestPublicSymbols:
    def test_functions_classes_and_constants_are_public(self) -> None:
        source = (
            "def add(a, b):\n    return a + b\n"
            "class Thing:\n    pass\n"
            "LIMIT = 3\n"
            "def _private():\n    pass\n"
            "_HIDDEN = 1\n"
        )
        assert public_symbols(source) == {"add", "Thing", "LIMIT"}

    def test_an_annotated_constant_is_public(self) -> None:
        assert public_symbols("VERSION: str = '1'\n") == {"VERSION"}

    def test_broken_source_has_no_symbols(self) -> None:
        """Inventing symbols from broken source would be worse than none."""
        assert public_symbols("def add(:\n") == set()


class TestTheFileCheck:
    def test_a_file_the_goal_did_not_name_is_reported(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text("X = 1\n", encoding="utf-8")
        (tmp_path / "b.py").write_text("Y = 1\n", encoding="utf-8")
        verdict = check_code_contract(
            CodeContract(allowed_paths=("a.py",)),
            tmp_path,
            changed=["a.py", "b.py"],
        )
        assert verdict.unexpected_files == ["b.py"]
        assert not verdict.ok

    def test_a_goal_that_names_no_path_skips_the_file_check(self, tmp_path: Path) -> None:
        """'Fix the failing test' names no file and must not be rejected."""
        (tmp_path / "a.py").write_text("X = 1\n", encoding="utf-8")
        verdict = check_code_contract(CodeContract(), tmp_path, changed=["a.py"])
        assert verdict.unexpected_files == []
        assert verdict.ok

    def test_a_leading_dot_slash_is_normalised(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text("X = 1\n", encoding="utf-8")
        verdict = check_code_contract(
            CodeContract(allowed_paths=("./a.py",)), tmp_path, changed=["a.py"]
        )
        assert verdict.ok


class TestTheSyntaxCheck:
    def test_a_file_that_no_longer_parses_is_reported(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text("def add(:\n", encoding="utf-8")
        verdict = check_code_contract(CodeContract(), tmp_path, changed=["a.py"])
        assert verdict.syntax_errors
        assert not verdict.ok
        assert "does not parse" in verdict.unmet[0]

    def test_a_valid_file_passes(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text("def add(a, b):\n    return a + b\n",
                                       encoding="utf-8")
        assert check_code_contract(CodeContract(), tmp_path, changed=["a.py"]).ok


class TestTheSymbolCheck:
    def test_a_removed_public_function_is_reported(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text("def add(a, b):\n    return a + b\n",
                                       encoding="utf-8")
        verdict = check_code_contract(
            CodeContract(),
            tmp_path,
            changed=["a.py"],
            before={"a.py": "def add(a, b):\n    return a + b\n\ndef sub(a, b):\n    return a - b\n"},
        )
        assert verdict.removed_symbols == ["a.py:sub"]
        assert not verdict.ok

    def test_adding_a_symbol_is_fine(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text(
            "def add(a, b):\n    return a + b\n\ndef sub(a, b):\n    return a - b\n",
            encoding="utf-8",
        )
        verdict = check_code_contract(
            CodeContract(), tmp_path, changed=["a.py"],
            before={"a.py": "def add(a, b):\n    return a + b\n"},
        )
        assert verdict.removed_symbols == []
        assert verdict.ok

    def test_renaming_a_private_helper_is_fine(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text("def _helper2():\n    pass\n", encoding="utf-8")
        verdict = check_code_contract(
            CodeContract(), tmp_path, changed=["a.py"],
            before={"a.py": "def _helper():\n    pass\n"},
        )
        assert verdict.ok


class TestTestsAndLintAreNotAssumed:
    def test_no_test_command_is_not_checked_not_passed(self, tmp_path: Path) -> None:
        """A repo with no tests is not a repo whose tests pass."""
        verdict = check_code_contract(
            CodeContract(run_tests=True), tmp_path, changed=[]
        )
        assert verdict.tests_passed is None
        assert any("no test command" in item for item in verdict.not_checked)

    def test_a_missing_linter_is_not_checked_not_clean(self, tmp_path: Path) -> None:
        verdict = check_code_contract(
            CodeContract(run_lint=True, lint_command=("definitely-not-a-linter",)),
            tmp_path,
            changed=[],
        )
        assert verdict.lint_errors == []
        assert any("not found" in item for item in verdict.not_checked)

    def test_a_failing_test_suite_is_reported(self, tmp_path: Path) -> None:
        verdict = check_code_contract(
            CodeContract(
                run_tests=True,
                test_command=(sys.executable, "-c", "raise SystemExit(1)"),
            ),
            tmp_path,
            changed=[],
        )
        assert verdict.tests_passed is False
        assert not verdict.ok

    def test_a_passing_test_suite_is_recorded(self, tmp_path: Path) -> None:
        verdict = check_code_contract(
            CodeContract(
                run_tests=True,
                test_command=(sys.executable, "-c", "raise SystemExit(0)"),
            ),
            tmp_path,
            changed=[],
        )
        assert verdict.tests_passed is True
        assert verdict.ok


class TestTheVerdictIsSerialisable:
    def test_to_dict_has_every_field(self, tmp_path: Path) -> None:
        verdict = check_code_contract(CodeContract(), tmp_path, changed=[])
        payload = verdict.to_dict()
        for key in ("ok", "unexpected_files", "removed_symbols", "syntax_errors",
                    "lint_errors", "tests_passed", "not_checked", "unmet"):
            assert key in payload
