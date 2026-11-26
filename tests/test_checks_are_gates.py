"""Checks that were computed and never read are now gates.

The review's summary was one sentence: "each check must be a gate, not an event
in the log".  Three checks in this project were computed and did not affect the
verdict -- ``document_issues``, ``overflow_375`` after ``overflow-x: hidden``,
and ``timeout_s``.  While that is true, a green report is indistinguishable from
green code.

These tests pin the four mechanisms that close that gap:

* D4 -- the attempt counter counts *failures at this question*, not calls;
* D5 -- the document pipeline's refinement verdict fails acceptance;
* D6 -- a phase's call allowance is a function of the work;
* D7 -- a called name is "defined" only if this file can see it.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gigaforge.core.config import CoreConfig, PhaseBudget  # noqa: E402
from gigaforge.core.contracts import RunContext  # noqa: E402
from gigaforge.core.router import ModelRouter  # noqa: E402
from gigaforge.core.state import TaskState  # noqa: E402


class _StubFixLoop:
    """A validator that accepts everything, so the gate under test is the only
    thing that can reject the run."""

    validator = object()

    def validate_once(self, diff: str, *, task: str = "", dry_run: bool = False) -> Any:
        from gigaforge.core.contracts import ValidateResult

        return ValidateResult(accepted=True, applied=True, changed_files=["docs/design.md"])


def _ctx(goal: str = "do the thing") -> RunContext:
    return RunContext(run_id="r1", state=TaskState(run_id="r1", goal=goal))


# ── D4: the counter counts failures, not calls ──────────────────────────────

def test_ten_different_prompts_are_ten_first_attempts():
    """A ten-section document must not escalate sections 3..10 to flagship.

    The counter was ``attempts[phase] += 1``.  For ``code`` in a fix loop that
    is roughly right; for ``document`` it is false -- ten sections are ten
    independent units of work, and from the third call on the ladder read
    "attempt 2 at this level" and raised every remaining section a level,
    although not one had failed.
    """
    from gigaforge.core.pipeline import TaskPipeline

    pipeline = TaskPipeline.__new__(TaskPipeline)
    ctx = _ctx()
    attempts = [
        pipeline._note_phase_attempt(ctx, "document", f"write section {i}")[0]
        for i in range(10)
    ]
    assert attempts == [0] * 10


def test_the_same_question_asked_again_is_an_attempt():
    """The control: a genuine repeat must still climb."""
    from gigaforge.core.pipeline import TaskPipeline

    pipeline = TaskPipeline.__new__(TaskPipeline)
    ctx = _ctx()
    seen = [pipeline._note_phase_attempt(ctx, "code", "same prompt")[0] for _ in range(3)]
    assert seen == [0, 1, 2]


def test_a_loop_that_knows_its_iteration_overrides_the_count():
    """The fix loop's iteration is the only honest source of "how many failed"."""
    from gigaforge.core.pipeline import TaskPipeline

    pipeline = TaskPipeline.__new__(TaskPipeline)
    ctx = _ctx()
    assert pipeline._note_phase_attempt(ctx, "fix", "p", attempt=4)[0] == 4


# ── D5: the document pipeline's verdict is a gate ───────────────────────────

def test_unresolved_document_issues_fail_acceptance():
    """``document_issues`` was written and read by nobody.

    A document that still had placeholders after refinement was accepted, so
    the pipeline was a log line rather than a gate.  This asserts the verdict,
    not the event.
    """
    from gigaforge.core.pipeline import TaskPipeline

    pipeline = TaskPipeline.__new__(TaskPipeline)
    pipeline.config = CoreConfig()
    pipeline.metrics = None
    pipeline._event = lambda *a, **k: None
    pipeline.test_runner = None
    pipeline.fixloop = _StubFixLoop()
    ctx = _ctx("Write a design document in docs/design.md with three sections")
    ctx.extras["document_issues"] = ["Section 2: Design Details is a placeholder"]

    class _Proposal:
        diff = "+## Section 1\n+Real prose about the subject.\n"
        changed_files: list[str] = ["docs/design.md"]

    result = pipeline._node_validate(ctx, {"proposal": _Proposal()})
    validation = result["validation"]
    assert validation.accepted is False
    assert "unresolved section" in validation.reason


def test_a_clean_document_passes_the_same_gate():
    """The control: no issues must not be turned into a failure."""
    from gigaforge.core.pipeline import TaskPipeline

    pipeline = TaskPipeline.__new__(TaskPipeline)
    pipeline.config = CoreConfig()
    pipeline.metrics = None
    pipeline._event = lambda *a, **k: None
    pipeline.test_runner = None
    pipeline.fixloop = _StubFixLoop()
    ctx = _ctx("Write a design document in docs/design.md with three sections")
    ctx.extras["document_issues"] = []

    class _Proposal:
        diff = "+## Section 1\n+Real prose about the subject.\n"
        changed_files: list[str] = ["docs/design.md"]

    result = pipeline._node_validate(ctx, {"proposal": _Proposal()})
    assert result["validation"].accepted is True


# ── D6: the allowance is a function of the work ─────────────────────────────

def test_a_ten_section_document_gets_thirty_calls():
    """``max_calls=16`` against 30 calls is a predictable failure, not a limit.

    The 17th call raised ``PhaseBudgetExceeded``, the section loop caught it as
    a degraded step, and ``assemble`` wrote "_No content was produced for this
    section._" into the document -- which then reported success.
    """
    router = ModelRouter(client=None, config=CoreConfig())
    assert router.config.budget_for("document").max_calls == 16
    router.scale_phase_budget("document", calls=3 * 10 + 4)
    assert router.config.budget_for("document").max_calls == 16  # config untouched
    assert router.remaining("document")["calls"] == 34


def test_scaling_only_raises():
    """A caller cannot lower a budget it does not own."""
    router = ModelRouter(client=None, config=CoreConfig())
    router.scale_phase_budget("document", calls=100)
    router.scale_phase_budget("document", calls=2)
    assert router.remaining("document")["calls"] == 100


# ── D7: reachability, not a global set of names ─────────────────────────────

def _validator(root: Path) -> Any:
    from gigaforge.firewall.validator import DiffValidator
    from gigaforge.firewall.config import FirewallConfig

    return DiffValidator(store=None, config=FirewallConfig(repo_root=str(root)))


def test_a_name_defined_in_an_unimported_module_is_not_reachable(tmp_path):
    """The check exists for "called but never defined or imported".

    The previous version answered "defined" for any name present anywhere in
    the checkout, which is a tautology for exactly that case: ``run``, ``main``,
    ``get``, ``parse`` are defined in *some* module of every repository.
    """
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "other.py").write_text(
        "def helper():\n    return 1\n", encoding="utf-8"
    )
    (tmp_path / "pkg" / "target.py").write_text(
        "def main():\n    return helper()\n", encoding="utf-8"
    )

    validator = _validator(tmp_path)
    from gigaforge.l0.parser import SymbolParser

    parsed = SymbolParser().parse_source(
        (tmp_path / "pkg" / "target.py").read_text(encoding="utf-8"), "pkg/target.py"
    )
    # ``helper`` exists in the checkout but this file neither defines nor
    # imports it -- Python would raise NameError, and so must the check.
    assert validator._name_is_reachable("helper", "pkg/target.py", parsed) is False


def test_a_name_imported_by_the_file_is_reachable(tmp_path):
    """The control: a real import must satisfy the check."""
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "other.py").write_text(
        "def helper():\n    return 1\n", encoding="utf-8"
    )
    (tmp_path / "pkg" / "target.py").write_text(
        "from .other import helper\n\ndef main():\n    return helper()\n",
        encoding="utf-8",
    )

    validator = _validator(tmp_path)
    from gigaforge.l0.parser import SymbolParser

    parsed = SymbolParser().parse_source(
        (tmp_path / "pkg" / "target.py").read_text(encoding="utf-8"), "pkg/target.py"
    )
    assert validator._name_is_reachable("helper", "pkg/target.py", parsed) is True


# ── D10: a host is structural, not a TLD list ───────────────────────────────

@pytest.mark.parametrize(
    "goal, expected",
    [
        ("Fix docs.example.ai/guide.md and src/app.py", ["src/app.py"]),
        ("See https://github.com/a/b/blob/x/src/_pytest/logging.py", []),
        ("Update site.co/page.html", []),
        ("Edit index.html", ["index.html"]),
        ("Edit docs/design.md", ["docs/design.md"]),
    ],
)
def test_a_host_is_not_a_path(goal, expected):
    """The TLD blocklist missed ``.ai``, ``.co``, ``.ru``, ``.app``, ``.sh``.

    A blocklist of TLDs cannot be complete, so the rule is structural: a dotted
    *directory* segment followed by a path is a host.  A bare filename with a
    dot (``index.html``) is not -- the first version of this check dropped every
    filename and broke artifact routing outright.
    """
    from gigaforge.harness.task_kind import _named_paths

    assert _named_paths(goal) == expected
