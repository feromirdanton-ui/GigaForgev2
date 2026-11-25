"""A run that reports zero cost is not a cheap run.

**The defect, measured live.**  A goal run spent 12 769 prompt + 2 095 completion
tokens across six calls and reported ``cost_usd = 0.0``, ``cost_micros = 0``,
``phase_cost_micros = {'plan': 0, 'code': 0, 'review': 0, 'summarize': 0}``.  Two
separate causes, both silent:

1. ``RunState.record_phase_usage`` built its table with ``PriceTable()``, which
   uses the built-in placeholders and ignores ``GF_CORE__PRICES__*`` entirely —
   so a deployment that configured its own prices was priced with someone
   else's.  ``from_env()`` is the constructor that reads the configuration.

2. ``price_for`` matched exactly or case-insensitively, and the router serves
   ``GigaChat-2-Max`` and ``GigaChat-Plus`` while the table is keyed by
   ``GigaChat-Max`` and ``GigaChat-Plus``.  Every call fell to the unknown
   sentinel, which is priced at zero by design.

The second is the more dangerous one, because the sentinel's zero is
indistinguishable in the report from a genuinely free run.  A number that means
"not computed" must not be the same number as "computed, and it was zero".
"""
from __future__ import annotations

import pytest

from gigaforge.core.cost import (
    DEFAULT_PRICES,
    UNKNOWN_PRICE,
    ModelPrice,
    PriceTable,
)


class TestTheTableReadsItsConfiguration:
    def test_from_env_reads_the_configured_prices(self, monkeypatch) -> None:
        monkeypatch.setenv("GF_CORE__PRICES__GigaChat__prompt_per_million", "0.50")
        monkeypatch.setenv("GF_CORE__PRICES__GigaChat__completion_per_million", "1.50")
        table = PriceTable.from_env()
        price = table.price_for("GigaChat")
        assert price.prompt_per_million == 0.50
        assert price.completion_per_million == 1.50

    def test_from_env_overrides_a_default(self, monkeypatch) -> None:
        monkeypatch.setenv("GF_CORE__PRICES__GigaChat__prompt_per_million", "9.99")
        assert PriceTable.from_env().price_for("GigaChat").prompt_per_million == 9.99

    def test_from_env_accepts_windows_normalized_keys(self) -> None:
        table = PriceTable.from_env(
            {"GF_CORE__PRICES__GIGACHAT__PROMPT_PER_MILLION": "9.99"}
        )
        assert table.price_for("GigaChat").prompt_per_million == 9.99

    def test_the_bare_constructor_does_not_read_the_environment(self, monkeypatch) -> None:
        """This is why the state must call ``from_env``, not ``PriceTable()``."""
        monkeypatch.setenv("GF_CORE__PRICES__GigaChat__prompt_per_million", "9.99")
        assert PriceTable().price_for("GigaChat").prompt_per_million == 0.50


class TestFamilyPrefixResolution:
    """The router's catalogue names are not the table's keys."""

    @pytest.mark.parametrize(
        "served",
        [
            "GigaChat-2-Max",
            "GigaChat-2-Pro",
            "GigaChat-Plus",
            "GigaChat-Max-preview",
            "GigaChat-2",
            "GigaChat",
        ],
    )
    def test_every_served_model_has_a_real_price(self, served) -> None:
        """The catalogue the router serves must not fall to the unknown sentinel.

        This is the assertion that matters: not *which* family a name resolves
        to, but that it resolves to a price at all.  The live run was served
        ``GigaChat-2-Max`` and ``GigaChat-Plus`` and priced both at zero.
        """
        assert PriceTable().price_for(served) is not UNKNOWN_PRICE, served

    def test_a_preview_suffix_resolves_by_prefix(self) -> None:
        """``GigaChat-Max-preview`` is not a key; its family is."""
        assert PriceTable().price_for("GigaChat-Max-preview").model == "GigaChat-Max"

    def test_the_longest_prefix_wins(self) -> None:
        table = PriceTable(
            {
                "Vendor": ModelPrice("Vendor", 1.0, 1.0),
                "Vendor-2": ModelPrice("Vendor-2", 2.0, 2.0),
                "Vendor-2-Max": ModelPrice("Vendor-2-Max", 3.0, 3.0),
            }
        )
        assert table.price_for("Vendor-2-Max").prompt_per_million == 3.0
        assert table.price_for("Vendor-2-Pro").prompt_per_million == 2.0
        assert table.price_for("Vendor-Lite").prompt_per_million == 1.0

    def test_an_exact_match_still_wins_over_a_prefix(self) -> None:
        table = PriceTable(
            {
                "Vendor": ModelPrice("Vendor", 1.0, 1.0),
                "Vendor-Pro": ModelPrice("Vendor-Pro", 7.0, 7.0),
            }
        )
        assert table.price_for("Vendor-Pro").prompt_per_million == 7.0

    def test_a_genuinely_unknown_model_is_still_unknown(self) -> None:
        """The prefix rule must not invent a price for a foreign model."""
        assert PriceTable().price_for("SomeOtherVendor-9000") is UNKNOWN_PRICE

    def test_an_empty_model_is_unknown(self) -> None:
        assert PriceTable().price_for("") is UNKNOWN_PRICE


class TestTheCostIsReal:
    def test_a_live_sized_run_costs_something(self) -> None:
        """The measured live numbers must produce a non-zero cost."""
        table = PriceTable()
        micros = table.cost_micros("GigaChat-2-Max", 12769, 2095)
        assert micros > 0, "a 14 864-token run priced at zero is a bug, not a bargain"

    def test_the_arithmetic_is_prompt_plus_completion(self) -> None:
        table = PriceTable({"m": ModelPrice("m", 1.0, 2.0)})
        # 1M prompt at 1.0 + 1M completion at 2.0 = 3.0 USD = 3_000_000 micros
        assert table.cost_micros("m", 1_000_000, 1_000_000) == 3_000_000

    def test_the_default_table_prices_the_served_catalogue(self) -> None:
        """Every model the live run actually used must have a real price."""
        table = PriceTable()
        for served in ("GigaChat-2-Max", "GigaChat-Plus", "GigaChat"):
            assert table.price_for(served) is not UNKNOWN_PRICE, served
        assert set(DEFAULT_PRICES) >= {"GigaChat", "GigaChat-Pro", "GigaChat-Max"}
