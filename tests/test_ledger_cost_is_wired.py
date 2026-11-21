"""A cost column that is always zero is worse than no cost column.

**The defect.**  ``RunLedger.note_cost`` existed, was correct, and **was never
called by anything**.  ``grep -rn "note_cost" gigaforge/`` returned exactly one
hit: its own definition.  So the ledger's cost columns were structurally zero —
measured live, a run that spent 12 769 prompt + 2 095 completion tokens across
six calls reported ``cost_per_task_usd = 0.0`` and ``phase_cost_micros`` all
zero.

A report that always says 0.00 is worse than a report with no cost column,
because it looks authoritative.  The reader cannot tell "this run was free" from
"nobody computed this".

The fix is not a new calculation — it is *calling* the one that exists, with the
per-phase token totals the budget tracker already holds, so the report can say
where the money went and not only how much.
"""
from __future__ import annotations

from gigaforge.core.cost import PriceTable
from gigaforge.harness.ledger import RunLedger


class TestTheLedgerCanPriceACall:
    def test_note_cost_adds_micros(self) -> None:
        ledger = RunLedger(run_id="r", goal="g", engine="e")
        micros = ledger.note_cost(
            model="GigaChat", phase="code", tokens_in=1_000_000, tokens_out=0,
            prices=PriceTable({"GigaChat": __import__("gigaforge.core.cost", fromlist=["ModelPrice"]).ModelPrice("GigaChat", 1.0, 2.0)}),
        )
        assert micros == 1_000_000
        assert ledger.cost_micros == 1_000_000
        assert ledger.cost_usd == 1.0

    def test_cost_is_attributed_to_the_phase(self) -> None:
        ledger = RunLedger(run_id="r", goal="g", engine="e")
        prices = PriceTable()
        ledger.note_cost(model="GigaChat", phase="plan", tokens_in=1000, tokens_out=1000, prices=prices)
        ledger.note_cost(model="GigaChat", phase="code", tokens_in=2000, tokens_out=2000, prices=prices)
        assert set(ledger.phase_cost_micros) == {"plan", "code"}
        assert ledger.phase_cost_micros["code"] > ledger.phase_cost_micros["plan"]

    def test_cost_per_task_is_the_run_total(self) -> None:
        ledger = RunLedger(run_id="r", goal="g", engine="e")
        ledger.note_cost(model="GigaChat", phase="code", tokens_in=1000, tokens_out=1000)
        assert ledger.cost_per_task_usd == ledger.cost_usd > 0

    def test_an_unknown_model_costs_zero_but_is_not_an_error(self) -> None:
        ledger = RunLedger(run_id="r", goal="g", engine="e")
        assert ledger.note_cost(model="Foreign-9000", phase="code", tokens_in=1000, tokens_out=1000) == 0


class TestTheHarnessActuallyCallsIt:
    def test_copy_accounting_prices_the_phases(self) -> None:
        """The wiring, not the arithmetic: this is what was missing."""
        import inspect

        from gigaforge.harness import Harness

        source = inspect.getsource(Harness._record_result)
        assert "note_cost" in source, (
            "note_cost is defined and never called: the cost columns are "
            "structurally zero"
        )
        assert "PriceTable.from_env" in source, (
            "the price table must read the deployment's configuration"
        )

    def test_the_ledger_model_is_taken_from_the_router(self) -> None:
        import inspect

        from gigaforge.harness import Harness

        source = inspect.getsource(Harness.run)
        assert "_ledger_model" in source, (
            "the ledger needs a model name to look up a price"
        )
