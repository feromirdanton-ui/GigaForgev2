"""A code change is graded by a contract, not by "the tests passed".

**The gap.**  An artifact goal had a contract: the plan's sections, the palette,
the heading, the card count, the render.  A code goal had one check — the test
suite — and that check is both too weak and too strong:

* too weak: a diff that touches files the goal never named, deletes a public
  function, or leaves the module unparseable passes as long as some test is
  green (and a repo with no tests passes trivially);
* too strong: a correct change that the existing tests do not cover is rejected
  for a reason that has nothing to do with the change.

The review asked for the four things a code change can be held to without
guessing: **the files it touched, the tests, the linter, and the public symbols
it kept**.  Each is a fact about the tree, not an opinion about the code.

This module computes those facts.  It is deliberately conservative: a check that
cannot run (no linter installed, no test suite) is reported as *not checked*,
never as *passed* — the same rule the render check follows.
"""
from __future__ import annotations

import ast
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

__all__ = [
    "CodeContract",
    "CodeVerdict",
    "public_symbols",
    "check_code_contract",
]


def public_symbols(source: str) -> set[str]:
    """Top-level names a module exposes: functions, classes, constants.

    A name is public when it does not start with an underscore.  This is the
    Python convention and it is what ``from module import name`` can reach, so
    it is the set a change must not silently delete.

    A module that does not parse returns an empty set — the caller reports the
    syntax error separately, and inventing symbols from broken source would be
    worse than reporting none.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if not node.name.startswith("_"):
                names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and not target.id.startswith("_"):
                    names.add(target.id)
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and not node.target.id.startswith("_"):
                names.add(node.target.id)
    return names


@dataclass
class CodeVerdict:
    """What the code contract found.  ``ok`` is the gate."""

    ok: bool = True
    #: Files the diff touched that the goal never named.
    unexpected_files: list[str] = field(default_factory=list)
    #: Public symbols that existed before and are gone now.
    removed_symbols: list[str] = field(default_factory=list)
    #: Files that no longer parse.
    syntax_errors: list[str] = field(default_factory=list)
    #: Linter findings (only when a linter actually ran).
    lint_errors: list[str] = field(default_factory=list)
    #: Test outcome: True/False, or None when there was nothing to run.
    tests_passed: bool | None = None
    #: Checks that could not run, with the reason.  Never reported as passed.
    not_checked: list[str] = field(default_factory=list)
    #: Human-readable reasons the verdict is not ok.
    unmet: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "unexpected_files": list(self.unexpected_files),
            "removed_symbols": list(self.removed_symbols),
            "syntax_errors": list(self.syntax_errors),
            "lint_errors": list(self.lint_errors),
            "tests_passed": self.tests_passed,
            "not_checked": list(self.not_checked),
            "unmet": list(self.unmet),
        }


@dataclass
class CodeContract:
    """The facts a code change is held to.

    ``allowed_paths`` is the set the goal named.  An empty set means "the goal
    named no path", in which case the file check is skipped rather than failing
    every change — a goal like "fix the failing test" names no file and must not
    be rejected for touching one.
    """

    allowed_paths: tuple[str, ...] = ()
    #: Run the test suite.  Off by default: the pipeline already runs it, and
    #: running it twice doubles the cost of every code task.
    run_tests: bool = False
    #: Run the linter.  Off by default for the same reason; the pipeline's
    #: format step already does it.
    run_lint: bool = False
    #: Test command, when ``run_tests`` is on.
    test_command: tuple[str, ...] = ()
    #: Lint command, when ``run_lint`` is on.
    lint_command: tuple[str, ...] = ()


def _run(cmd: Sequence[str], cwd: Path, timeout: int = 120) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            list(cmd), cwd=cwd, capture_output=True, text=True, timeout=timeout
        )
    except FileNotFoundError:
        return 127, f"command not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s"
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def check_code_contract(
    contract: CodeContract,
    root: Path | str,
    *,
    changed: Iterable[str],
    before: dict[str, str] | None = None,
) -> CodeVerdict:
    """Grade a code change against *contract*.

    ``changed`` is the list of paths the diff touched.  ``before`` maps a path to
    its content **before** the change, which is what makes the public-symbol
    check possible: a symbol that was there and is gone is a removal, and a
    removal is the one thing a code change must not do silently.
    """
    root_path = Path(root)
    verdict = CodeVerdict()
    changed_list = [str(p) for p in changed if str(p).strip()]

    # 1. The files the change touched.
    if contract.allowed_paths:
        allowed = {p.replace("\\", "/").lstrip("./") for p in contract.allowed_paths}
        for path in changed_list:
            normalised = path.replace("\\", "/").lstrip("./")
            if normalised not in allowed:
                verdict.unexpected_files.append(path)
        if verdict.unexpected_files:
            verdict.unmet.append(
                "the change touched files the goal did not name: "
                + ", ".join(verdict.unexpected_files[:5])
            )

    # 2. Every changed Python file must still parse, and must keep its public
    #    symbols.  A syntax error is the one defect that makes every other check
    #    meaningless, so it is checked first and reported on its own.
    for path in changed_list:
        if not path.endswith(".py"):
            continue
        target = root_path / path
        if not target.is_file():
            continue
        try:
            after = target.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            verdict.not_checked.append(f"{path}: unreadable ({exc})")
            continue
        try:
            ast.parse(after)
        except SyntaxError as exc:
            verdict.syntax_errors.append(f"{path}:{exc.lineno}: {exc.msg}")
            continue
        if before and path in before:
            was = public_symbols(before[path])
            now = public_symbols(after)
            for name in sorted(was - now):
                verdict.removed_symbols.append(f"{path}:{name}")
    if verdict.syntax_errors:
        verdict.unmet.append(
            "the change does not parse: " + "; ".join(verdict.syntax_errors[:3])
        )
    if verdict.removed_symbols:
        verdict.unmet.append(
            "the change removed public symbols: "
            + ", ".join(verdict.removed_symbols[:5])
        )

    # 3. Tests.  A suite that cannot run is NOT a pass.
    if contract.run_tests:
        if not contract.test_command:
            verdict.not_checked.append("tests: no test command configured")
        else:
            code, output = _run(contract.test_command, root_path)
            if code == 127:
                verdict.not_checked.append(f"tests: {output.strip()}")
            else:
                verdict.tests_passed = code == 0
                if code != 0:
                    verdict.unmet.append(
                        "the test suite failed: " + output.strip()[-400:]
                    )

    # 4. Linter.  Same rule: absent is not clean.
    if contract.run_lint:
        if not contract.lint_command:
            verdict.not_checked.append("lint: no lint command configured")
        else:
            code, output = _run(contract.lint_command, root_path)
            if code == 127:
                verdict.not_checked.append(f"lint: {output.strip()}")
            else:
                findings = [
                    line for line in output.splitlines()
                    if re.match(r"^\S+:\d+:\d+:", line) or line.startswith("E")
                ]
                verdict.lint_errors = findings[:20]
                if code != 0 and findings:
                    verdict.unmet.append(
                        f"the linter reported {len(findings)} finding(s)"
                    )

    verdict.ok = not verdict.unmet
    return verdict
