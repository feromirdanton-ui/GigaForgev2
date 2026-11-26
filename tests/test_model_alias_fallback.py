"""A stale model name must not become a hard outage when the catalogue is down.

Measured: after a series of live runs the catalogue answered 429, ``_catalogue()``
returned empty, the remap was skipped, ``GigaChat`` went to the provider
unchanged, and every SWE-bench instance reported "did not produce an executable
plan" with **0 tokens**.  A stale name in ``.env`` was presented as a planning
defect, and the run never reached the model.

The mapping is knowledge, not a network call.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gigaforge.client.client import _LEGACY_MODEL_ALIASES, GigaClient  # noqa: E402


def _client(catalogue: set[str] | None) -> GigaClient:
    client = GigaClient()
    client._catalogue_cache = catalogue if catalogue is not None else set()
    return client


def test_a_stale_name_is_remapped_when_the_catalogue_is_unavailable() -> None:
    """The exact live failure: 429 on the catalogue, 404 on every call."""
    client = _client(set())
    assert client._resolve_model("GigaChat") == "GigaChat-2"


def test_the_pro_and_max_names_are_remapped_too() -> None:
    client = _client(set())
    assert client._resolve_model("GigaChat-Pro") == "GigaChat-2-Pro"
    assert client._resolve_model("GigaChat-Max") == "GigaChat-2-Max"


def test_a_current_name_is_passed_through_untouched() -> None:
    """A name the table does not know is not guessed at."""
    client = _client(set())
    assert client._resolve_model("GigaChat-2") == "GigaChat-2"
    assert client._resolve_model("some-other-model") == "some-other-model"


def test_the_catalogue_still_wins_when_it_is_available() -> None:
    """The catalogue is the authority; the table is the fallback."""
    client = _client({"GigaChat-2", "GigaChat-2-Pro"})
    assert client._resolve_model("GigaChat-2-Pro") == "GigaChat-2-Pro"


def test_a_name_the_catalogue_lists_is_never_remapped() -> None:
    client = _client({"GigaChat", "GigaChat-2"})
    assert client._resolve_model("GigaChat") == "GigaChat"


def test_an_empty_request_is_returned_as_is() -> None:
    assert _client(set())._resolve_model("") == ""


def test_the_table_only_contains_names_that_were_renamed() -> None:
    """A table that maps a current name would silently swap a working model."""
    for old, new in _LEGACY_MODEL_ALIASES.items():
        assert old != new
        assert new.startswith("GigaChat-2")
