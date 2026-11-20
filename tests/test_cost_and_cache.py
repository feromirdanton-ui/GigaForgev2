"""A run must be able to say what it cost and whether its cache works.

**The gap.**  The ledger recorded tokens and a bare ``cache_hit: bool``.  Tokens
alone do not answer the question a budget owner asks — the same token count on
a different model is a different bill — and a boolean says whether the *last*
lookup hit, not whether the cache earns its keep.  Neither number existed in
``PipelineResult`` at all, so a report could not show ``cost_per_task`` or
``cache_hit_rate`` even though the price table and the cache were both there.

These tests pin the arithmetic, the per-phase breakdown, and the fact that the
numbers reach the result rather than dying in the ledger.
"""
from __future__ import annotations

import pytest

from gigaforge.core.cost import ModelPrice, PriceTable
from gigaforge.core.state import TaskState
from gigaforge.harness.ledger import RunLedger


class TestLedgerCost:
    def test_cost_is_accumulated_from_the_price_table(self) -> None:
        ledger = RunLedger(run_id="r")
        prices = PriceTable({"m": ModelPrice("m", 1.0, 2.0)})
        # 1M prompt tokens at $1/M + 1M completion at $2/M = $3 = 3_000_000 micros
        ledger.note_cost(model="m", phase="code", tokens_in=1_000_000,
                         tokens_out=1_000_000, prices=prices)
        assert ledger.cost_micros == 3_000_000
        assert ledger.cost_usd == pytest.approx(3.0)

    def test_cost_is_broken_down_by_phase(self) -> None:
        ledger = RunLedger(run_id="r")
        prices = PriceTable({"m": ModelPrice("m", 1.0, 0.0)})
        ledger.note_cost(model="m", phase="plan", tokens_in=1_000_000,
                         tokens_out=0, prices=prices)
        ledger.note_cost(model="m", phase="code", tokens_in=2_000_000,
                         tokens_out=0, prices=prices)
        assert ledger.phase_cost_micros == {"plan": 1_000_000, "code": 2_000_000}
        assert ledger.cost_micros == 3_000_000

    def test_an_unknown_model_costs_zero_but_is_still_counted(self) -> None:
        """A price is never invented; the tokens are still real."""
        ledger = RunLedger(run_id="r")
        ledger.note_cost(model="nobody-knows-this", phase="code",
                         tokens_in=1000, tokens_out=1000)
        assert ledger.cost_micros == 0

    def test_cost_per_task_is_the_run_cost(self) -> None:
        ledger = RunLedger(run_id="r")
        prices = PriceTable({"m": ModelPrice("m", 1.0, 0.0)})
        ledger.note_cost(model="m", phase="code", tokens_in=500_000,
                         tokens_out=0, prices=prices)
        assert ledger.cost_per_task_usd == pytest.approx(0.5)


class TestLedgerCache:
    def test_the_rate_is_hits_over_lookups(self) -> None:
        ledger = RunLedger(run_id="r")
        for hit in (True, False, True, True):
            ledger.note_cache(hit=hit)
        assert ledger.cache_hits == 3
        assert ledger.cache_lookups == 4
        assert ledger.cache_hit_rate == pytest.approx(0.75)

    def test_no_lookups_is_zero_not_a_division_error(self) -> None:
        assert RunLedger(run_id="r").cache_hit_rate == 0.0

    def test_the_numbers_reach_to_dict(self) -> None:
        ledger = RunLedger(run_id="r")
        ledger.note_cache(hit=True)
        ledger.note_cache(hit=False)
        payload = ledger.to_dict()
        assert payload["cache_hit_rate"] == 0.5
        assert payload["cache_lookups"] == 2
        assert "cost_per_task_usd" in payload


class TestStateCost:
    def test_a_priced_call_accumulates_cost(self) -> None:
        state = TaskState(run_id="r", goal="g")
        state.record_phase_usage(
            "code",
            {"prompt_tokens": 1_000_000, "completion_tokens": 0,
             "total_tokens": 1_000_000, "model": "GigaChat"},
        )
        # GigaChat is $0.50/M prompt in the default table.
        assert state.cost_micros == 500_000
        assert state.phase_cost_micros["code"] == 500_000

    def test_an_unpriced_call_still_counts_tokens(self) -> None:
        state = TaskState(run_id="r", goal="g")
        state.record_phase_usage(
            "code",
            {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15,
             "model": "unknown-model"},
        )
        assert state.cost_micros == 0
        assert state.total_tokens() == 15

    def test_cache_rate_on_state(self) -> None:
        state = TaskState(run_id="r", goal="g")
        state.note_cache(hit=True)
        state.note_cache(hit=False)
        assert state.cache_hit_rate == 0.5


class TestDegradationIsRecorded:
    def test_a_degradation_is_kept_with_its_reason(self) -> None:
        """'We did not check' must never be reported as 'we checked and passed'."""
        state = TaskState(run_id="r", goal="g")
        state.note_degraded(what="render_check", why="chromium not installed",
                            where="settle")
        assert state.degraded == [
            {"what": "render_check", "why": "chromium not installed", "where": "settle"}
        ]

    def test_a_long_reason_is_truncated_not_dropped(self) -> None:
        state = TaskState(run_id="r", goal="g")
        state.note_degraded(what="x", why="y" * 1000)
        assert len(state.degraded[0]["why"]) == 400
