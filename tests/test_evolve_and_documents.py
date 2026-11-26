"""The evolution loop must be a search, not a rewrite.

**The defect this pins.**  GigaForge's quality lives in eight places, and there
was no mechanism to improve any of them except editing by hand.  Letting a model
rewrite the repository is how a harness becomes a random walk with a green
bench.  Harness²'s answer is a frozen search over a declared surface; this is
that mechanism.

**The contract that matters most.**  Omitted is inherited.  A proposal naming
one component must produce a surface that differs in exactly that component —
otherwise every proposal is a full replacement and every review is a full
re-read.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gigaforge.harness.document_pipeline import (  # noqa: E402
    assemble,
    plan_document,
    refinement_issues,
    section_prompt,
)
from gigaforge.harness.evolve import (  # noqa: E402
    EDITABLE_COMPONENTS,
    ProbeResult,
    SurfaceSpec,
    compose,
    compose_surface,
    probe,
    propose,
)
from gigaforge.harness.prune import PRUNE_SET, check_prune  # noqa: E402


# ── the surface ──────────────────────────────────────────────────────────────

def test_omitted_is_inherited() -> None:
    """The contract: a one-field proposal changes one field."""
    incumbent = SurfaceSpec(
        system_prompt="old prompt",
        context_budget=1000,
        tool_set=["read", "write"],
    )
    proposal = propose(incumbent, {"system_prompt": "new prompt"})
    merged = compose_surface(incumbent, proposal)
    assert merged.system_prompt == "new prompt"
    assert merged.context_budget == 1000, "an omitted component was not inherited"
    assert merged.tool_set == ["read", "write"]


def test_an_unknown_component_is_rejected() -> None:
    """A typo must not become a new component nothing reads."""
    incumbent = SurfaceSpec()
    try:
        propose(incumbent, {"system_promt": "typo"})
    except ValueError as exc:
        assert "system_promt" in str(exc)
    else:
        raise AssertionError("a typo was accepted as a component")


def test_a_no_op_proposal_is_rejected() -> None:
    """Probing a change that changes nothing reports a false 'no improvement'."""
    incumbent = SurfaceSpec(system_prompt="same")
    try:
        propose(incumbent, {"system_prompt": "same"})
    except ValueError as exc:
        assert "changes nothing" in str(exc)
    else:
        raise AssertionError("a no-op proposal was accepted")


def test_the_surface_has_exactly_eight_components() -> None:
    assert len(EDITABLE_COMPONENTS) == 8, EDITABLE_COMPONENTS


def test_the_fingerprint_changes_with_the_surface() -> None:
    a = SurfaceSpec(system_prompt="a")
    b = SurfaceSpec(system_prompt="b")
    assert a.fingerprint() != b.fingerprint()
    assert a.fingerprint() == SurfaceSpec(system_prompt="a").fingerprint()


# ── compose ──────────────────────────────────────────────────────────────────

def _probe(rates: dict[str, float], error: str = "") -> ProbeResult:
    return ProbeResult(surface_fingerprint="x", pass_rates=rates, error=error)


def test_a_candidate_within_the_noise_floor_is_rejected() -> None:
    """4/5 -> 5/5 is the same result observed twice."""
    verdict = compose(_probe({"a": 0.8}), _probe({"a": 1.0}), floor=0.35)
    assert not verdict.accepted
    assert "noise floor" in verdict.reason


def test_a_candidate_that_regresses_a_task_is_rejected() -> None:
    """Trading one task for another is a different harness, not a better one."""
    verdict = compose(
        _probe({"a": 1.0, "b": 0.0}),
        _probe({"a": 0.0, "b": 1.0}),
        floor=0.0,
    )
    assert not verdict.accepted
    assert "regressed" in verdict.reason
    assert verdict.regressions == ["a"]


def test_a_real_improvement_is_accepted() -> None:
    verdict = compose(
        _probe({"a": 0.0, "b": 0.0}),
        _probe({"a": 1.0, "b": 1.0}),
        floor=0.1,
    )
    assert verdict.accepted, verdict.reason


def test_an_unmeasurable_candidate_is_a_rejection_not_a_zero() -> None:
    """A broken candidate must not look like a bad one."""
    verdict = compose(_probe({"a": 1.0}), _probe({}, error="boom"))
    assert not verdict.accepted
    assert "could not be measured" in verdict.reason


def test_a_probe_that_raises_is_recorded_as_an_error() -> None:
    def boom(surface: SurfaceSpec) -> dict:
        raise RuntimeError("provider down")

    result = probe(SurfaceSpec(), boom)
    assert result.error
    assert "provider down" in result.error
    assert result.pass_rates == {}


# ── the prune set ────────────────────────────────────────────────────────────

def test_the_prune_set_has_the_five_behaviours() -> None:
    names = {rule.name for rule in PRUNE_SET}
    assert names == {
        "task_leakage", "silent_degradation", "fabricated_evidence",
        "unbounded_spend", "destructive_edit",
    }


def test_a_leaked_candidate_is_pruned() -> None:
    verdict = check_prune({"leakage": {"leaked": True,
                                       "findings": [{"detail": "task name"}]}})
    assert verdict.pruned
    assert "task_leakage" in verdict.tripped


def test_a_reported_degradation_is_not_pruned() -> None:
    """A degradation that is in the report is honest; a hidden one is not."""
    honest = check_prune({"degraded": ["x"], "reported_degradations": True})
    assert not honest.pruned
    hidden = check_prune({"degraded": ["x"]})
    assert hidden.pruned


def test_fabricated_evidence_is_pruned() -> None:
    assert check_prune({"invented_citations": ["https://fake"]}).pruned
    assert check_prune({"claimed_tests_passed": True, "tests_passed": False}).pruned


def test_a_clean_candidate_is_not_pruned() -> None:
    assert not check_prune({"pass_rates": {"a": 1.0}}).pruned


# ── the document pipeline ────────────────────────────────────────────────────

def test_the_shape_comes_from_the_goal_not_the_model() -> None:
    plan = plan_document(
        "Write a short document explaining how a run works, with sections for "
        "intake, plan, act and verify."
    )
    assert [s.title for s in plan.outline.sections] == [
        "Intake", "Plan", "Act", "Verify"
    ]


def test_a_goal_without_named_sections_gets_a_default_shape() -> None:
    plan = plan_document("Write a document about caching.")
    assert len(plan.outline.sections) >= 3
    assert plan.min_chars > 0


def test_a_placeholder_section_is_an_issue() -> None:
    """The exact failure the live runs shipped."""
    plan = plan_document("Write a document with sections for Intro and Details.")
    doc = (
        "# Doc\n\n## Intro\n\nThis section provides an overview of the topic. "
        "It covers the main points and the details.\n\n"
        "## Details\n\n" + "Real content about caching. " * 10 + "\n"
    )
    issues = refinement_issues(doc, plan)
    assert any("placeholder" in i for i in issues), issues


def test_a_missing_section_is_an_issue() -> None:
    plan = plan_document("Write a document with sections for Intro and Details.")
    issues = refinement_issues("# Doc\n\n## Intro\n\n" + "x " * 100, plan)
    assert any("missing section: Details" in i for i in issues), issues


def test_a_thin_section_is_an_issue() -> None:
    plan = plan_document("Write a document with sections for Intro and Details.")
    doc = "# Doc\n\n## Intro\n\nToo short.\n\n## Details\n\n" + "y " * 100
    issues = refinement_issues(doc, plan)
    assert any("chars of content" in i for i in issues), issues


def test_a_good_document_passes() -> None:
    plan = plan_document("Write a document with sections for Intro and Details.")
    doc = (
        "# Doc\n\n## Intro\n\n" + "Caching stores a result so the next call is "
        "cheap. " * 6 + "\n\n## Details\n\n" + "The cache is keyed by the prompt "
        "hash and invalidated on a write. " * 6 + "\n"
    )
    assert refinement_issues(doc, plan) == []


def test_assemble_keeps_the_plan_order() -> None:
    plan = plan_document("Write a document with sections for B and A.")
    doc = assemble(plan, {"A": "a body", "B": "b body"})
    assert doc.index("## B") < doc.index("## A")


def test_the_section_prompt_carries_its_evidence() -> None:
    plan = plan_document("Write a document with sections for Intro.")
    section = plan.outline.sections[0]
    section.evidence = ["the cache is keyed by prompt hash"]
    prompt = section_prompt(section, document_title="Caching")
    assert "the cache is keyed by prompt hash" in prompt
    assert "placeholder" in prompt
