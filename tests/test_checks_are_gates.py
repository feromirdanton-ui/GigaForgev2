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


# ── the indent repair must cover the case the delta cannot see ──────────────

def test_over_indented_added_lines_are_repaired_when_context_matches(tmp_path):
    """The live bench's ``toy-validate-email`` failure, 1 of 3 repeats.

    The repair shifted lines by the *context* delta.  When the context matches
    the file exactly every delta is 0, so a body indented four spaces too deep
    survived untouched and the file failed to parse with ``unexpected indent``.
    The earlier fix was correct for mismatched context and blind to this case.
    """
    from gigaforge.harness.diff_repair import normalize_hunk_indentation

    (tmp_path / "math_utils.py").write_text(
        "def add(a, b):\n    return a + b\n\n\n"
        "def validate_email(value):\n    return '@' in value\n",
        encoding="utf-8",
    )
    # The added block is internally inconsistent: the ``if`` sits at 8 spaces
    # and its body at 4, so the file does not parse.  (A uniformly over-indented
    # block *is* valid Python -- the first version of this test used one and
    # asserted a repair that no parser would ask for.)
    diff = (
        "--- a/math_utils.py\n+++ b/math_utils.py\n@@ -5,2 +5,4 @@\n"
        " def validate_email(value):\n"
        "-    return '@' in value\n"
        "+        if not value:\n"
        "+    return False\n"
        "+        return '@' in value\n"
    )
    out, report = normalize_hunk_indentation(diff, tmp_path)
    assert report.changed is True
    assert "+    if not value:" in out
    assert "+        return False" in out


def test_a_repair_that_does_not_parse_is_refused(tmp_path):
    """The control: a shift that leaves the file invalid is not a repair."""
    from gigaforge.harness.diff_repair import normalize_hunk_indentation

    (tmp_path / "m.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    diff = (
        "--- a/m.py\n+++ b/m.py\n@@ -1,2 +1,3 @@\n"
        " def f():\n"
        "-    return 1\n"
        "+    return 1\n"
        "+def broken(:\n"
    )
    out, report = normalize_hunk_indentation(diff, tmp_path)
    assert report.changed is False
    assert out == diff


# ── evolve validates values, not only names ─────────────────────────────────

def test_a_negative_ladder_threshold_is_rejected():
    """``attempts_before_escalation: -1`` was accepted by the CLI.

    The ladder compares ``attempt >= threshold``, so a negative threshold
    escalates every call and the cheap model is never used -- a proposal that
    "improves" the bench by spending the flagship on everything.  The value
    check lived only in ``compose_surface``; ``propose`` is the CLI's entry
    point, so the command that exists to reject it reported "accepted".
    """
    from gigaforge.harness.evolve import SurfaceSpec, propose

    with pytest.raises(ValueError, match="must be >= 0"):
        propose(SurfaceSpec(), {"ladder_thresholds": {"attempts_before_escalation": -1}})


def test_a_sane_ladder_threshold_is_accepted():
    """The control: a real tuning change must still pass."""
    from gigaforge.harness.evolve import SurfaceSpec, propose

    proposal = propose(SurfaceSpec(), {"ladder_thresholds": {"attempts_before_escalation": 3}})
    assert proposal.changes["ladder_thresholds"]["attempts_before_escalation"] == 3


# ── a section is prose, not a patch ─────────────────────────────────────────

def test_a_section_wrapped_in_a_diff_fence_is_unwrapped():
    """Measured live: the model answered prose in the diff channel.

    Every section arrived as a fenced ``diff`` block with a ``@@`` header and
    ``+``-prefixed lines.  The text was correct and the file was unreadable --
    5 439 characters of diff syntax around 2 000 of prose.  A channel problem
    is fixed by computation, not by paying for another call.
    """
    from gigaforge.harness.document_pipeline import clean_section_body

    raw = (
        "```diff\n"
        "@@ -0,0 +1,4 @@\n"
        "+## Details\n"
        "+\n"
        "+Caching stores data temporarily.\n"
        "+```\n"
    )
    assert clean_section_body(raw, title="Details") == "Caching stores data temporarily."


def test_plain_prose_is_left_alone():
    """The control: a section that is already prose must not be rewritten."""
    from gigaforge.harness.document_pipeline import clean_section_body

    prose = "Caching stores data temporarily.\n\n- Memory cache\n- Disk cache"
    assert clean_section_body(prose, title="Details") == prose


def test_a_stated_section_count_is_honoured():
    """"with four sections" was parsed as a one-item list named "S".

    The pattern matched the bare word "sections" and captured what followed, so
    the first letter of "sections" became the only section title and the whole
    document was written to a shape the user never asked for.
    """
    from gigaforge.harness.document_pipeline import plan_document

    plan = plan_document("Write a design document about caching in docs/c.md with four sections")
    assert len(plan.outline.sections) == 4
    assert all(len(s.title) > 1 for s in plan.outline.sections)


def test_a_named_section_list_still_wins():
    """The control: an explicit list of titles is not a count."""
    from gigaforge.harness.document_pipeline import plan_document

    plan = plan_document("Write a document with sections for intake, plan, act")
    assert [s.title for s in plan.outline.sections] == ["Intake", "Plan", "Act"]


def test_a_document_with_a_path_reaches_the_pipeline():
    """The route was ``is_text_product``, which is False for a document with a
    path -- so the form that has a file to write never reached the pipeline and
    went to the coder instead."""
    from gigaforge.harness.task_kind import classify_task

    kind = classify_task("Write a design document in docs/caching.md", None)
    assert kind.is_document is True
    assert kind.document_paths == ("docs/caching.md",)


def test_a_section_may_not_open_with_the_document_heading():
    """Measured live: the run was rejected with "section 'Overview' has 0 chars".

    The model re-emitted the document's own H1 inside section one.  ``assemble``
    writes the section heading, so the body began with a heading, the section
    measured as empty, and the gate -- correctly -- failed the document.  The
    gate was right and the body was wrong, so the body is what gets fixed.
    """
    from gigaforge.harness.document_pipeline import clean_section_body

    raw = (
        "# Design Document About Caching Strategies\n\n"
        "Caching is a fundamental technique.\n\n"
        "### Purpose\nIt serves as a reference."
    )
    cleaned = clean_section_body(raw, title="Overview")
    assert cleaned.startswith("Caching is a fundamental technique.")
    assert not cleaned.startswith("#")
