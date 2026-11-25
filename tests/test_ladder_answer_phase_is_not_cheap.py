"""For an answer goal the summarize phase IS the deliverable.

Measured live: "What is 2+2? Answer in chat, do not change any files." went to
the cheap model, which returned an empty string; the run reported
``finish_action`` with ``chars: 0`` twice and then ``needs_attention``.  A phase
that writes the product is not a cheap phase, whatever it is called.
"""
from __future__ import annotations

from gigaforge.core.ladder import Ladder, LadderPolicy, Level, Signals


def _signals(**kw):
    base = dict(
        phase="summarize", attempt=0, repeats=0, contract_rejected=False,
        files_touched=0, sections=0, tokens_used=0, tokens_budget=0,
    )
    base.update(kw)
    return Signals(**base)


def test_an_answer_goal_gets_a_strong_model_for_its_reply() -> None:
    ladder = Ladder(LadderPolicy(allow_flagship=True))
    assert ladder.decide(_signals(task_kind="answer")).level is Level.L2_STRONG


def test_a_summary_of_an_artifact_stays_cheap() -> None:
    ladder = Ladder(LadderPolicy(allow_flagship=True))
    assert ladder.decide(_signals(task_kind="artifact")).level is Level.L1_CHEAP
