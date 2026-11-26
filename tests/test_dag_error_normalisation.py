"""A refused run must say why, and "ok" is not a reason.

**The defect this pins.**  ``_collect`` starts from ``dag.error`` and then fills
in the real reason only when that string is empty.  The DAG reports ``"ok"`` on a
run that reached the end of its nodes, and ``"ok"`` is truthy — so every branch
that explains a refusal was skipped.  Measured live: a document the contract
refused came back as ``status=rejected, accepted=False, error="ok"``.

A caller reading that has no way to tell a contract refusal from a crash, and the
report cannot be trusted about the one thing it exists to say.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gigaforge.core.pipeline import _normalise_dag_error  # noqa: E402


@pytest.mark.parametrize("value", ["ok", "OK", " ok ", "none", "success", "succeeded"])
def test_a_success_word_is_not_an_error(value: str) -> None:
    assert _normalise_dag_error(value) == ""


@pytest.mark.parametrize(
    "value",
    [
        "the artifact did not meet the goal: section 'features'",
        "review rejected the change: missing footer",
        "the model did not produce a diff",
    ],
)
def test_a_real_reason_survives(value: str) -> None:
    assert _normalise_dag_error(value) == value


def test_none_and_empty_are_empty() -> None:
    assert _normalise_dag_error(None) == ""
    assert _normalise_dag_error("") == ""


def test_a_reason_that_merely_contains_ok_is_kept() -> None:
    """The check is the whole word, not a substring: "token ok" is a reason."""
    assert _normalise_dag_error("token ok but the diff was empty") != ""
