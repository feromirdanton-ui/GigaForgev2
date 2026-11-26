"""The search trajectory: budget, history, stall, novelty, prune set.

Each test names the failure it prevents.  The failures are not hypothetical:
every one of them is a way a self-improving harness reports progress it did not
make.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gigaforge.harness.evolve import (  # noqa: E402
    EDITABLE_COMPONENTS,
    ProbeResult,
    SurfaceSpec,
    run_round,
)
from gigaforge.harness.search import (  # noqa: E402
    STRUCTURAL_COMPONENTS,
    History,
    edit_budget,
    enforce_budget,
    novelty,
)


# ── the annealed budget ────────────────────────────────────────────────────
def test_the_budget_starts_wide_and_narrows_to_one() -> None:
    """Early rounds may bundle; the last round is a single-variable experiment.

    A constant budget of one is slow to leave a bad start; a constant budget of
    eight makes every candidate unattributable.  The point of annealing is that
    the *last* rounds are the ones whose result means something.
    """
    budgets = [edit_budget(t, 10) for t in range(10)]
    assert budgets[0] == 3
    assert budgets[-1] == 1
    assert budgets == sorted(budgets, reverse=True)


def test_the_budget_never_falls_below_the_floor() -> None:
    for t in range(0, 40):
        assert edit_budget(t, 10) >= 1


def test_a_bundle_larger_than_the_budget_is_refused_not_trimmed() -> None:
    """Trimming would measure a candidate nobody proposed.

    If the proposer asks for four components and the round allows one, silently
    keeping one means the score is attributed to a change the proposer did not
    make.  A refusal names the budget so the proposer can split the proposal.
    """
    with pytest.raises(ValueError, match="allows 1 independent edit"):
        enforce_budget({"system_prompt": "a", "skills": ["b"]}, 1)


def test_a_bundle_within_the_budget_passes_through() -> None:
    assert enforce_budget({"system_prompt": "a"}, 1) == {"system_prompt": "a"}


# ── the edit history ───────────────────────────────────────────────────────
def test_the_history_records_the_hypothesis_and_the_verdict() -> None:
    hist = History()
    hist.record(round=0, component="skills", hypothesis="add a lint skill",
                score_before=0.5, score_after=0.7, verdict="accepted", adopted=True)
    assert hist.records[0].hypothesis == "add a lint skill"
    assert hist.records[0].delta == pytest.approx(0.2)
    assert hist.records[0].adopted is True


def test_a_falsified_hypothesis_is_not_offered_again() -> None:
    """The proposer is conditioned on the history, so it does not redraw.

    Without this the search re-proposes the same idea every round and reports
    "no improvement" as if it had tried something new.
    """
    hist = History()
    hist.record(round=0, component="skills", hypothesis="add a lint skill",
                verdict="rejected", adopted=False)
    hist.record(round=1, component="skills", hypothesis="add a test skill",
                verdict="accepted", adopted=True)
    falsified = hist.falsified()
    assert any("lint skill" in item for item in falsified)
    assert not any("test skill" in item for item in falsified)


def test_the_history_round_trips_through_jsonl(tmp_path: Path) -> None:
    hist = History()
    hist.record(round=0, component="tool_set", hypothesis="h", adopted=True)
    hist.record(round=1, component="skills", hypothesis="g", adopted=False)
    path = hist.save(tmp_path / "history.jsonl")
    loaded = History.load(path)
    assert len(loaded.records) == 2
    assert loaded.records[0].component == "tool_set"
    assert loaded.records[1].adopted is False


def test_a_truncated_final_line_does_not_lose_the_complete_records() -> None:
    """A crash mid-write is not a corrupt history."""
    text = (
        '{"round": 0, "component": "skills", "adopted": true}\n'
        '{"round": 1, "component": "tool_'
    )
    hist = History.from_jsonl(text)
    assert len(hist.records) == 1
    assert hist.records[0].component == "skills"


# ── stall and exploration ──────────────────────────────────────────────────
def test_a_search_that_has_barely_started_is_not_called_stalled() -> None:
    """Two edits is not evidence of a stall; redirecting then is noise."""
    hist = History()
    hist.record(round=0, component="skills", adopted=False)
    hist.record(round=1, component="skills", adopted=False)
    assert hist.stall_flag(window=3) is False


def test_three_rounds_without_a_win_is_a_stall() -> None:
    hist = History()
    for t in range(3):
        hist.record(round=t, component="skills", adopted=False)
    assert hist.stall_flag(window=3) is True


def test_a_stalled_search_is_directed_to_an_untried_component() -> None:
    """A search that keeps editing the same prompt is polishing, not searching."""
    hist = History()
    for t in range(3):
        hist.record(round=t, component="system_prompt", adopted=False)
    directives = hist.exploration_directives(EDITABLE_COMPONENTS)
    assert directives
    assert "system_prompt" not in directives


def test_a_yielding_search_gets_no_directive() -> None:
    """A directive is a correction; issuing one to a working search is noise."""
    hist = History()
    for t in range(3):
        hist.record(round=t, component="skills", adopted=True)
    assert hist.exploration_directives(EDITABLE_COMPONENTS) == []


# ── the prune set ──────────────────────────────────────────────────────────
def test_a_component_tried_twice_and_never_helping_is_prunable() -> None:
    hist = History()
    hist.record(round=0, component="retry_policy", adopted=False)
    hist.record(round=1, component="retry_policy", adopted=False)
    assert "retry_policy" in hist.prune_set()


def test_one_failed_edit_is_not_a_prunable_component() -> None:
    """One failed edit is a hypothesis that did not pan out, not a dead component."""
    hist = History()
    hist.record(round=0, component="retry_policy", adopted=False)
    assert hist.prune_set() == []


def test_a_component_that_won_once_is_never_pruned() -> None:
    hist = History()
    hist.record(round=0, component="skills", adopted=True)
    hist.record(round=1, component="skills", adopted=False)
    hist.record(round=2, component="skills", adopted=False)
    assert "skills" not in hist.prune_set()


# ── novelty ────────────────────────────────────────────────────────────────
def test_a_search_that_only_tuned_numbers_has_full_structural_novelty() -> None:
    """The state that looks converged while being one-dimensional."""
    hist = History()
    hist.record(round=0, component="ladder_thresholds", adopted=True)
    assert novelty(hist) == 1.0


def test_a_structural_win_reduces_novelty() -> None:
    hist = History()
    hist.record(round=0, component="tool_set", adopted=True)
    assert novelty(hist) == pytest.approx(1 - 1 / len(STRUCTURAL_COMPONENTS))


def test_novelty_is_a_share_so_it_compares_across_search_lengths() -> None:
    short = History()
    short.record(round=0, component="tool_set", adopted=True)
    long = History()
    for t in range(20):
        long.record(round=t, component="ladder_thresholds", adopted=True)
    long.record(round=20, component="tool_set", adopted=True)
    assert novelty(short) == pytest.approx(novelty(long))


# ── the round, end to end ──────────────────────────────────────────────────
def _probe(rates: dict[str, float], cost: int = 100) -> ProbeResult:
    return ProbeResult(surface_fingerprint="x", pass_rates=rates, cost_micros=cost)


def test_a_round_that_improves_is_adopted_and_recorded() -> None:
    hist = History()
    incumbent = _probe({"a": 0.5, "b": 0.5})
    outcome = run_round(
        SurfaceSpec(), incumbent, {"skills": ["lint"]},
        round_index=0, total_rounds=10,
        run_bench=lambda s: {"pass_rates": {"a": 1.0, "b": 1.0}},
        history=hist, rationale="a lint skill catches the failure earlier",
    )
    assert outcome.adopted is True
    assert hist.records[0].adopted is True
    assert hist.records[0].component == "skills"


def test_a_pruned_candidate_is_never_probed() -> None:
    """The score must not exist: a score that exists gets averaged in."""
    called = {"n": 0}

    def bench(surface: SurfaceSpec) -> dict:
        called["n"] += 1
        return {"pass_rates": {"a": 1.0}}

    outcome = run_round(
        SurfaceSpec(), _probe({"a": 0.0}), {"skills": ["x"]},
        round_index=0, total_rounds=10, run_bench=bench,
        observation={"invented_citations": ["https://made.up"]},
    )
    assert outcome.pruned == ["fabricated_evidence"]
    assert called["n"] == 0


def test_a_round_over_budget_never_reaches_the_bench() -> None:
    called = {"n": 0}

    def bench(surface: SurfaceSpec) -> dict:
        called["n"] += 1
        return {"pass_rates": {"a": 1.0}}

    outcome = run_round(
        SurfaceSpec(), _probe({"a": 0.0}),
        {"skills": ["x"], "tool_set": ["y"], "context_budget": 10},
        round_index=9, total_rounds=10, run_bench=bench,
    )
    assert "allows 1 independent edit" in outcome.rejected_reason
    assert called["n"] == 0


def test_a_round_that_regresses_is_rejected_even_when_the_mean_rises() -> None:
    """A change that trades one task for another is a different harness."""
    outcome = run_round(
        SurfaceSpec(), _probe({"a": 1.0, "b": 0.0}), {"skills": ["x"]},
        round_index=0, total_rounds=10,
        run_bench=lambda s: {"pass_rates": {"a": 0.0, "b": 1.0}},
    )
    assert outcome.adopted is False
    assert "regressed" in (outcome.verdict.reason if outcome.verdict else "")
