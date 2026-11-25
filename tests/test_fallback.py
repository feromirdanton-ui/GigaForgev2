"""The deterministic fallback for a model that will not emit JSON.

The behaviour under test: a parse failure is nudged once, then the harness takes
over the part the model is failing at — a deterministic search, then a forced
finish — instead of spending the whole step budget on reformat requests.
"""
from __future__ import annotations

from gigaforge.agents.fallback import (
    EXPLORE,
    FORCE_FINISH,
    NUDGE,
    ParseFailureFallback,
    derive_grep_pattern,
)


# ── the pattern ─────────────────────────────────────────────────────────────
def test_an_identifier_is_preferred_over_prose() -> None:
    """A symbol name is what actually appears in the source."""
    pattern = derive_grep_pattern("Please fix the validate_email function in the code")
    assert "validate_email" in pattern
    assert "the" not in pattern.split("|")


def test_stopwords_are_never_searched_for() -> None:
    """A grep for 'the' over a repository is a wasted tool call."""
    pattern = derive_grep_pattern("the file should be updated with the new value")
    terms = pattern.split("|") if pattern else []
    assert "the" not in terms
    assert "file" not in terms
    assert "should" not in terms
    assert "updated" not in terms


def test_a_goal_of_pure_prose_yields_no_pattern() -> None:
    """No searchable term means no grep — the caller must be able to tell.

    The goal here is deliberately all stopwords: a pattern built from it would
    match half the repository, which is worse than not searching at all.
    """
    assert derive_grep_pattern("please do it and make the file") == ""


def test_a_backticked_token_wins() -> None:
    """The user pointing at a symbol directly is the strongest signal."""
    pattern = derive_grep_pattern("look at `parse_config` and `load_settings`")
    assert "parse_config" in pattern
    assert "load_settings" in pattern


def test_the_pattern_is_bounded() -> None:
    pattern = derive_grep_pattern("alpha_one beta_two gamma_three delta_four epsilon_five")
    assert len(pattern.split("|")) <= 3


def test_a_regex_metacharacter_in_the_goal_is_escaped() -> None:
    """A goal is text, not a regex — an unescaped '.' would match anything."""
    pattern = derive_grep_pattern("fix `a.b`")
    assert "a\\.b" in pattern


# ── the escalation ──────────────────────────────────────────────────────────
def test_the_first_failure_only_nudges() -> None:
    """A nudge is cheaper than any fallback, and the model usually complies."""
    fallback = ParseFailureFallback()
    assert fallback.next_action("fix validate_email", 1).kind == NUDGE


def test_the_second_failure_explores_deterministically() -> None:
    fallback = ParseFailureFallback()
    action = fallback.next_action("fix validate_email", 2)
    assert action.kind == EXPLORE
    assert action.action == "grep"
    assert "validate_email" in action.action_input


def test_exploration_happens_at_most_once() -> None:
    """A fallback that repeats is just another loop."""
    fallback = ParseFailureFallback(explore_after=2, finish_after=10)
    assert fallback.next_action("fix validate_email", 2).kind == EXPLORE
    assert fallback.next_action("fix validate_email", 3).kind == NUDGE
    assert fallback.next_action("fix validate_email", 4).kind == NUDGE


def test_the_third_failure_forces_the_finish() -> None:
    """Three failures is not a formatting accident."""
    fallback = ParseFailureFallback()
    assert fallback.next_action("fix validate_email", 3).kind == FORCE_FINISH


def test_an_unsearchable_goal_never_explores() -> None:
    """No pattern means no grep — nudging is the honest response."""
    fallback = ParseFailureFallback()
    assert fallback.next_action("the file should be updated", 2).kind == NUDGE


def test_every_decision_is_recorded() -> None:
    fallback = ParseFailureFallback()
    for count in (1, 2, 3):
        fallback.next_action("fix validate_email", count)
    described = fallback.describe()
    assert [d["kind"] for d in described["decisions"]] == [NUDGE, EXPLORE, FORCE_FINISH]
    assert described["explored"] is True


# ── the loop actually uses it ───────────────────────────────────────────────
def test_the_react_loop_stops_asking_after_three_parse_failures() -> None:
    """End-to-end: a model that only ever answers in prose must not eat the budget.

    Before the fallback, every step was a reformat request and the episode ended
    with nothing.  Now the third failure breaks out and the forced finish runs.
    Driven with ``asyncio.run`` — the project does not depend on pytest-asyncio.
    """
    import asyncio

    from gigaforge.agents.react import ReActRunner

    class _ProseClient:
        """Always answers in prose — never JSON."""

        def __init__(self) -> None:
            self.calls = 0

        async def chat(self, *args, **kwargs):
            self.calls += 1
            from gigaforge.client.giga_client import ChatResult

            return ChatResult(
                content="I think the answer is in parser.py, let me explain...",
                usage={"prompt_tokens": 10, "completion_tokens": 10},
            )

    class _Budgets:
        def remaining(self, phase):
            return 10**9

        def task_remaining_for(self, task_id):
            return 10**9

        def check(self, *a, **k):
            return None

        def commit_task(self, *a, **k):
            return None

        def check_overshoot(self, *a, **k):
            return None

        def reserve(self, *a, **k):
            from tests.budget_stubs import NullClaim

            return NullClaim()

    class _Router:
        def route(self, phase):
            return "GigaChat-2-Lite"

    class _Tracer:
        def trace(self, *a, **k):
            return None

    class _State:
        task_id = "t1"

        def serialize_prefix(self):
            return {}

    class _Ctx:
        sandbox = None
        worktree = None
        repo_root = None
        index = None

    runner = ReActRunner(
        client=_ProseClient(),
        router=_Router(),
        budgets=_Budgets(),
        tracer=_Tracer(),
        max_steps=8,
    )
    result = asyncio.run(runner.run("fix validate_email", _State(), ctx=_Ctx()))

    # The loop must not have spent all 8 steps on reformat requests: the third
    # failure breaks out, and the forced finish is one more call.
    assert result.llm_calls <= 4, (
        f"the loop made {result.llm_calls} calls; a prose-only model must not "
        "consume the whole step budget"
    )
    assert any(
        step.get("fallback") == FORCE_FINISH for step in result.steps
    ), "the forced-finish fallback never fired"
