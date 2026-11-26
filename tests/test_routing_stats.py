"""The routing log is evidence; the routing table is a measurement.

**The gap this closes.**  The run report recorded one row per model call: phase,
level, tokens, model.  That says what happened, not whether the decision was
worth its price.  The question an operator asks is "is L3 worth 2x L2?" — and
answering it needs the calls grouped by rung with their outcome and their cost.

The table is a pure function of the rows the pipeline already writes, so it
cannot disagree with the log and costs nothing at run time.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gigaforge.core.routing_stats import (  # noqa: E402
    LevelStats,
    routing_advice,
    routing_table,
)


def _row(phase: str, level: int, model: str = "", tin: int = 0, tout: int = 0, **kw):
    return {
        "phase": phase,
        "level": level,
        "model": model,
        "tokens_in": tin,
        "tokens_out": tout,
        **kw,
    }


def test_calls_are_grouped_by_rung() -> None:
    rows = [
        _row("code", 2, "GigaChat-2-Pro", 100, 50),
        _row("code", 3, "GigaChat-2-Max", 200, 80),
        _row("review", 1, "GigaChat-2", 30, 10),
    ]
    table = routing_table(rows)
    assert [entry.level for entry in table] == [1, 2, 3]
    assert {entry.level: entry.calls for entry in table} == {1: 1, 2: 1, 3: 1}


def test_tokens_are_summed_per_rung() -> None:
    rows = [_row("code", 2, tin=100, tout=50), _row("code", 2, tin=200, tout=80)]
    entry = routing_table(rows)[0]
    assert entry.tokens_in == 300
    assert entry.tokens_out == 130
    assert entry.tokens_per_call == 215


def test_acceptance_rate_counts_only_explicit_accepts() -> None:
    rows = [
        _row("code", 3, accepted=True),
        _row("code", 3, accepted=False),
        _row("code", 3),
    ]
    entry = routing_table(rows)[0]
    assert entry.calls == 3
    assert entry.accepted == 1
    assert entry.acceptance_rate == pytest.approx(1 / 3, rel=1e-3)


def test_phase_cost_is_attributed_evenly_across_its_calls() -> None:
    """Cost is priced per phase, not per call; even attribution is what the data supports."""
    rows = [_row("code", 2), _row("code", 3)]
    table = routing_table(rows, phase_cost={"code": 1000})
    assert {entry.level: entry.cost_micros for entry in table} == {2: 500, 3: 500}


def test_a_row_without_a_level_is_not_counted_as_strong() -> None:
    """Counting an undecided call as L2 would inflate the rung being measured."""
    rows = [_row("code", 2), {"phase": "code", "level": None, "tokens_in": 999}]
    table = routing_table(rows)
    assert len(table) == 1
    assert table[0].calls == 1
    assert table[0].tokens_in == 0


def test_models_are_counted_per_rung() -> None:
    rows = [_row("code", 3, "GigaChat-2-Max"), _row("fix", 3, "GigaChat-2-Max")]
    entry = routing_table(rows)[0]
    assert entry.models == {"GigaChat-2-Max": 2}


def test_an_empty_log_says_so() -> None:
    assert routing_table([]) == []
    assert routing_advice([]) == [
        "no levelled calls in this run: the ladder was not consulted"
    ]


def test_advice_states_the_cost_ratio_it_can_see() -> None:
    table = [
        LevelStats(level=1, calls=2, accepted=2, cost_micros=200),
        LevelStats(level=2, calls=2, accepted=1, cost_micros=600),
    ]
    notes = routing_advice(table)
    assert any("L2 costs 3.0x L1" in note for note in notes), notes


def test_advice_flags_a_rung_that_never_succeeded() -> None:
    table = [LevelStats(level=3, calls=4, accepted=0, cost_micros=4000)]
    notes = routing_advice(table)
    assert any("none accepted" in note for note in notes), notes


def test_the_report_carries_the_table() -> None:
    from gigaforge.core.pipeline import PipelineResult

    result = PipelineResult(
        run_id="r",
        routing=[_row("code", 2, "GigaChat-2-Pro", 10, 5)],
        phase_cost_micros={"code": 100},
    )
    payload = result.to_dict()
    assert payload["routing_table"][0]["level"] == 2
    assert payload["routing_table"][0]["cost_micros"] == 100
