"""A rejected cycle must say what is wrong with the artifact.

The defect: ``goal.py`` called the verifier only when ``accepted`` was true, so
a rejected cycle carried the pipeline's own reason and nothing else.  Measured
live: a page with a duplicated stylesheet and a stray ``</style>`` in the body
was rejected three cycles running, each cycle re-deriving the same mistake,
because nothing ever said "the body contains CSS text".

The fix runs the *cheap* subset on rejection — syntax and the HTML audit, no
test suite and no browser — and appends its findings to the error the next cycle
reads.  The tests below pin both halves: that it finds a real structural
problem, and that it stays silent when there is nothing to say (an invented
complaint would send the next cycle chasing a phantom).
"""
from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from gigaforge.core.goal import GoalRunner, WorkspaceVerifier


class _Runner(GoalRunner):
    """A GoalRunner with only what ``_cheap_verification`` needs."""

    def __init__(self, root: Path) -> None:  # noqa: D107
        self.repo_root = str(root)


BROKEN_HTML = (
    "<!DOCTYPE html><html><head><style>body{color:#fff}</style></head><body>"
    "body{color:#fff}\n"
    '<section class="hero"><h1>Hi</h1></section>'
    "</body></html>"
)


class TestCheapVerificationOnReject:
    def test_a_broken_html_file_is_reported(self, tmp_path: Path) -> None:
        (tmp_path / "index.html").write_text(BROKEN_HTML, encoding="utf-8")
        runner = _Runner(tmp_path)
        out = runner._cheap_verification(["index.html"], "make a page")
        assert out, "a page with CSS text in the body produced no finding"
        assert "index.html" in out

    def test_a_clean_html_file_is_silent(self, tmp_path: Path) -> None:
        clean = (
            "<!DOCTYPE html><html><head><style>body{color:#fff}</style></head>"
            '<body><section class="hero"><h1>Hi</h1></section></body></html>'
        )
        (tmp_path / "index.html").write_text(clean, encoding="utf-8")
        runner = _Runner(tmp_path)
        assert runner._cheap_verification(["index.html"], "make a page") == ""

    def test_a_syntax_error_is_reported(self, tmp_path: Path) -> None:
        (tmp_path / "broken.py").write_text("def f(:\n    pass\n", encoding="utf-8")
        runner = _Runner(tmp_path)
        out = runner._cheap_verification(["broken.py"], "fix it")
        assert "broken.py" in out

    def test_a_valid_python_file_is_silent(self, tmp_path: Path) -> None:
        (tmp_path / "ok.py").write_text("def f():\n    return 1\n", encoding="utf-8")
        runner = _Runner(tmp_path)
        assert runner._cheap_verification(["ok.py"], "fix it") == ""

    def test_a_missing_file_is_not_a_finding(self, tmp_path: Path) -> None:
        runner = _Runner(tmp_path)
        assert runner._cheap_verification(["nope.html"], "x") == ""

    def test_a_path_outside_the_repo_is_ignored(self, tmp_path: Path) -> None:
        runner = _Runner(tmp_path)
        assert runner._cheap_verification(["../../etc/passwd"], "x") == ""

    def test_it_never_raises(self, tmp_path: Path) -> None:
        runner = _Runner(tmp_path)
        # A directory where a file is expected, an empty name, a weird suffix.
        (tmp_path / "adir.html").mkdir()
        out = runner._cheap_verification(["adir.html", "", "x.weird"], "x")
        assert isinstance(out, str)

    def test_it_does_not_run_the_test_suite(self, tmp_path: Path) -> None:
        """The cheap path must stay cheap: no pytest, no browser."""
        source = inspect.getsource(GoalRunner._cheap_verification)
        assert "pytest" not in source
        assert "browser" not in source.lower() or "require_browser=False" in source


class TestWiring:
    def test_the_verifier_runs_on_rejection(self) -> None:
        source = inspect.getsource(GoalRunner.run)
        assert "_cheap_verification" in source
        # It must be guarded by "not accepted", not by "accepted".
        assert "if not accepted and changed:" in source

    def test_the_finding_reaches_the_next_cycle(self) -> None:
        source = inspect.getsource(GoalRunner.run)
        assert "error = (error + " in source, (
            "the finding must be appended to the error the next cycle reads"
        )

    def test_an_event_is_emitted(self) -> None:
        source = inspect.getsource(GoalRunner.run)
        assert "goal.verify_on_reject" in source


class TestBuiltinVerifierIsReusable:
    def test_the_audit_is_callable_without_a_browser(self, tmp_path: Path) -> None:
        (tmp_path / "index.html").write_text(BROKEN_HTML, encoding="utf-8")
        verifier = WorkspaceVerifier(require_browser=False)
        check = verifier._audit_html(tmp_path, tmp_path / "index.html")
        assert check.status == "failed"

    def test_changed_paths_rejects_escapes(self, tmp_path: Path) -> None:
        verifier = WorkspaceVerifier(require_browser=False)
        assert verifier._changed_paths(tmp_path, ["../x.py"]) == []
