"""Model pricing and cost accounting (V2).

V1 counted tokens but never turned them into money, so a run could not answer
the one question a budget owner actually asks: *what did this cost?*

V2 adds a price table and a cost accumulator.  Two deliberate choices:

* **integer micro-units.**  Costs are accumulated as integer micro-USD
  (1e-6 USD) so a long run cannot drift through float rounding, and the value
  round-trips through SQLite's ``cost_micros`` column without a cast.
* **prices are configuration, not constants.**  The defaults below are a
  starting point; every one of them is overridable from the environment, because
  a price that is baked into code is a price that silently goes stale.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

MICROS_PER_USD = 1_000_000


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class ModelPrice:
    """USD per one million tokens, split by direction."""

    model: str
    prompt_per_million: float = 0.0
    completion_per_million: float = 0.0

    def cost_micros(self, prompt_tokens: int, completion_tokens: int) -> int:
        prompt = max(0, int(prompt_tokens or 0))
        completion = max(0, int(completion_tokens or 0))
        micros = (
            prompt * self.prompt_per_million + completion * self.completion_per_million
        )
        return int(round(micros))

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "prompt_per_million": self.prompt_per_million,
            "completion_per_million": self.completion_per_million,
        }


#: Default prices.  These are *placeholders* for a self-hosted / contract-priced
#: deployment and are meant to be overridden with ``GF_CORE__PRICES__*``.
DEFAULT_PRICES: dict[str, ModelPrice] = {
    "GigaChat": ModelPrice("GigaChat", prompt_per_million=0.50, completion_per_million=1.50),
    "GigaChat-Pro": ModelPrice("GigaChat-Pro", prompt_per_million=2.50, completion_per_million=10.00),
    "GigaChat-Max": ModelPrice("GigaChat-Max", prompt_per_million=5.00, completion_per_million=20.00),
}

#: Used for any model the table does not know.  Zero by default: an unknown
#: model must not invent a cost, but the *tokens* are still counted.
UNKNOWN_PRICE = ModelPrice("<unknown>", 0.0, 0.0)


class PriceTable:
    """Lookup of :class:`ModelPrice` by model name, with a safe fallback."""

    def __init__(
        self,
        prices: Optional[Mapping[str, ModelPrice]] = None,
        *,
        default: Optional[ModelPrice] = None,
    ) -> None:
        self._prices: dict[str, ModelPrice] = dict(DEFAULT_PRICES)
        for name, price in (prices or {}).items():
            self._prices[str(name)] = price
        self._default = default or UNKNOWN_PRICE

    def price_for(self, model: str) -> ModelPrice:
        """Exact match first, then a case-insensitive match, then the default."""
        if not model:
            return self._default
        exact = self._prices.get(model)
        if exact is not None:
            return exact
        lowered = model.strip().lower()
        for name, price in self._prices.items():
            if name.lower() == lowered:
                return price
        return self._default

    def cost_micros(self, model: str, prompt_tokens: int, completion_tokens: int) -> int:
        return self.price_for(model).cost_micros(prompt_tokens, completion_tokens)

    def cost_usd(self, model: str, prompt_tokens: int, completion_tokens: int) -> float:
        return self.cost_micros(model, prompt_tokens, completion_tokens) / MICROS_PER_USD

    def known(self) -> list[str]:
        return sorted(self._prices)

    def to_dict(self) -> dict[str, Any]:
        return {name: price.to_dict() for name, price in sorted(self._prices.items())}

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "PriceTable":
        """Build a table from ``GF_CORE__PRICES__<model>__prompt_per_million``.

        The model name is used verbatim (case preserved) so a deployment can
        price ``GigaChat-Pro`` and ``gigachat-pro`` as two entries if it wants.
        """
        source = os.environ if env is None else env
        prefix = "GF_CORE__PRICES__"
        overrides: dict[str, ModelPrice] = {}
        for raw_key, value in source.items():
            if not raw_key.startswith(prefix):
                continue
            parts = raw_key[len(prefix):].split("__")
            if len(parts) != 2:
                continue
            model, knob = parts[0], parts[1].lower()
            current = overrides.get(model) or DEFAULT_PRICES.get(model) or ModelPrice(model)
            if knob == "prompt_per_million":
                overrides[model] = ModelPrice(
                    model, _as_float(value, current.prompt_per_million), current.completion_per_million
                )
            elif knob == "completion_per_million":
                overrides[model] = ModelPrice(
                    model, current.prompt_per_million, _as_float(value, current.completion_per_million)
                )
        return cls(overrides)


@dataclass
class CostAccumulator:
    """Running cost total, broken down by model and by phase."""

    prices: PriceTable = field(default_factory=PriceTable)
    total_micros: int = 0
    by_model: dict[str, int] = field(default_factory=dict)
    by_phase: dict[str, int] = field(default_factory=dict)
    calls: int = 0

    def add(
        self,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        *,
        phase: str = "",
    ) -> int:
        micros = self.prices.cost_micros(model, prompt_tokens, completion_tokens)
        self.total_micros += micros
        self.calls += 1
        key = model or "<unknown>"
        self.by_model[key] = self.by_model.get(key, 0) + micros
        if phase:
            self.by_phase[phase] = self.by_phase.get(phase, 0) + micros
        return micros

    @property
    def total_usd(self) -> float:
        return self.total_micros / MICROS_PER_USD

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_micros": self.total_micros,
            "total_usd": round(self.total_usd, 6),
            "calls": self.calls,
            "by_model_micros": dict(sorted(self.by_model.items())),
            "by_phase_micros": dict(sorted(self.by_phase.items())),
        }


def cost_micros(
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    *,
    prices: Optional[PriceTable] = None,
) -> int:
    """Convenience one-shot cost lookup."""
    return (prices or PriceTable()).cost_micros(model, prompt_tokens, completion_tokens)


__all__ = [
    "ModelPrice",
    "PriceTable",
    "CostAccumulator",
    "DEFAULT_PRICES",
    "UNKNOWN_PRICE",
    "MICROS_PER_USD",
    "cost_micros",
]
