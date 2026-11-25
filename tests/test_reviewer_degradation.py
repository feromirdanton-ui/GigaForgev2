"""A judge that cannot answer must not reject the work.

**The defect this pins.**  Both review strategies returned ``reject`` when the
reviewer's answer could not be parsed::

    return ReviewOutcome(verdict="reject",
                         issues=["reviewer returned an unparseable verdict"])

Measured live: a 95-byte diff that had been produced and applied was thrown away
because the reviewer's answer was not JSON.  The incentive is inverted — the
reviewer that says nothing is punished harder than the one that says "reject" —
and the work is lost either way.

The rule now: an unparseable answer is a *harness degradation*, recorded as an
advisory issue, and the run proceeds.  It must not be presented as a clean
approval either, which is what ``advisory`` is for.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gigaforge.core.strategies import (  # noqa: E402
    ConstitutionalReview,
    LLMReview,
    _parse_review_json,
)


def _ask_garbage(phase: str, prompt: str, temperature: float | None = None) -> str:
    return "I think this looks fine, but I will not say so in JSON."


def _ask_reject(phase: str, prompt: str, temperature: float | None = None) -> str:
    return '{"verdict": "reject", "issues": ["the function is still wrong"]}'


def _ask_ok(phase: str, prompt: str, temperature: float | None = None) -> str:
    return '{"verdict": "ok", "issues": []}'


def test_an_unparseable_verdict_does_not_reject_the_diff() -> None:
    outcome = LLMReview().review("--- a\n+++ b\n", goal="fix it", ask=_ask_garbage)
    assert outcome.verdict == "ok", outcome
    assert outcome.advisory is True
    assert any("could not be parsed" in issue for issue in outcome.issues), outcome.issues


def test_an_unparseable_verdict_is_not_a_clean_approval() -> None:
    """The run proceeds, but the report must not claim the review passed."""
    outcome = LLMReview().review("--- a\n+++ b\n", goal="fix it", ask=_ask_garbage)
    assert outcome.issues, "a degradation with no issue text reads as a pass"


def test_a_real_rejection_still_rejects() -> None:
    """The fix must not turn every rejection into an approval."""
    outcome = LLMReview().review("--- a\n+++ b\n", goal="fix it", ask=_ask_reject)
    assert outcome.verdict == "reject", outcome
    assert outcome.issues == ["the function is still wrong"]


def test_a_real_approval_still_approves() -> None:
    outcome = LLMReview().review("--- a\n+++ b\n", goal="fix it", ask=_ask_ok)
    assert outcome.verdict == "ok", outcome
    assert outcome.issues == []


def test_the_constitutional_judge_degrades_the_same_way() -> None:
    """Both strategies must agree, or the behaviour depends on an env var.

    The diff is a *valid* one on purpose: the constitution runs deterministic
    principles first, and an invalid diff is rejected by the gate before the
    judge is ever asked.  That rejection is correct and is not what this test is
    about — the question is what happens when the judge answers garbage.
    """
    valid = (
        "--- a/calc.py\n"
        "+++ b/calc.py\n"
        "@@ -1,2 +1,2 @@\n"
        " def add(a, b):\n"
        "-    return a - b\n"
        "+    return a + b\n"
    )
    review = ConstitutionalReview()
    outcome = review.review(valid, goal="fix it", ask=_ask_garbage)
    assert outcome.verdict == "ok", outcome


def test_the_parser_still_returns_none_for_garbage() -> None:
    """The degradation lives in the strategy, not in the parser."""
    assert _parse_review_json("no json here")[0] is None
    assert _parse_review_json('{"verdict": "ok"}')[0] == "ok"
