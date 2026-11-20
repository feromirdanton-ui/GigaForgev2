"""Regression tests for the P1 review findings.

Three findings, all of the same shape as the P0 ones: a capability existed but
nothing exercised it.

* **Structured output had no source.**  ``response_format`` travelled the whole
  stack since ``99967d2``, but no caller ever built one, so every agent still
  parsed with ``json_repair``.  ``gigaforge.agents.schemas`` is the source, and
  the planner/reviewer now pass it.
* **The context guard was only in the non-default engine.**  ``ContextWindowGuard``
  was wired into the LangGraph graph; the classic loop — the default — had none.
* **The server count cost a round trip per step.**  A ReAct trajectory grows
  monotonically, so the shared prefix was re-counted on every step.

Offline and deterministic: no GigaChat key, no network.
"""
from __future__ import annotations

import inspect
from typing import Any


# ── structured output has a source ──────────────────────────────────────────
def test_plan_schema_is_a_valid_json_schema() -> None:
    """The schema must be well-formed enough for the provider to accept it."""
    from gigaforge.agents.schemas import PLAN_SCHEMA

    assert PLAN_SCHEMA["type"] == "object"
    assert "steps" in PLAN_SCHEMA["required"]
    step = PLAN_SCHEMA["properties"]["steps"]["items"]
    assert step["properties"]["tool"]["enum"] == [
        "read_structure",
        "edit_function",
        "run_tests",
        "grep",
    ]


def test_plan_schema_matches_the_planner_allowlist() -> None:
    """The schema's enum and the parser's allowlist must not drift apart.

    A schema that permits a tool the parser rejects would turn a valid model
    answer into a repair loop.
    """
    from gigaforge.agents.planner import ALLOWED_TOOLS
    from gigaforge.agents.schemas import PLAN_SCHEMA

    enum = set(PLAN_SCHEMA["properties"]["steps"]["items"]["properties"]["tool"]["enum"])
    assert enum == set(ALLOWED_TOOLS)


def test_response_format_envelope_matches_the_sdk() -> None:
    """``{type, schema, strict}`` — the fields ``JsonSchemaResponseFormat`` has."""
    from gigaforge.agents.schemas import plan_response_format

    envelope = plan_response_format()
    assert envelope["type"] == "json_schema"
    assert isinstance(envelope["schema"], dict)
    assert envelope["strict"] is True


def test_planner_passes_a_response_format() -> None:
    """The planner must actually send the schema, not merely be able to."""
    from gigaforge.agents import planner

    source = inspect.getsource(planner.PlannerAgent.plan)
    assert "response_format=plan_response_format()" in source


def test_reviewer_passes_a_response_format() -> None:
    from gigaforge.agents import reviewer

    source = inspect.getsource(reviewer.ReviewerAgent.review)
    assert "response_format=review_response_format()" in source


def test_json_repair_remains_the_fallback() -> None:
    """Structured output is an optimisation; a fenced answer must still parse."""
    from gigaforge.agents.planner import parse_plan_json

    steps, error = parse_plan_json(
        '```json\n{"steps":[{"tool":"grep","args":{"q":"x"}}]}\n```'
    )
    assert error is None
    assert steps is not None and steps[0].tool == "grep"


# ── the guard is in the default engine ──────────────────────────────────────
def test_classic_loop_checks_the_context_window() -> None:
    """``agents/react.py`` is the default engine; it must guard too."""
    from gigaforge.agents import react

    source = inspect.getsource(react.ReActRunner.run)
    assert "window_guard.check" in source
    assert "ContextOverflowError" in source


def test_classic_loop_guard_uses_the_client_counter() -> None:
    """The guard must be built from the client's ``count_tokens``."""
    from gigaforge.agents import react

    source = inspect.getsource(react.ReActRunner.window_guard.fget)
    assert "count_tokens" in source
    assert "CachedTokenCounter" in source


def test_overflow_stops_the_loop_with_a_typed_reason() -> None:
    """An overflow is a finish reason, not an exception escaping the runner."""
    from gigaforge.agents import react

    source = inspect.getsource(react.ReActRunner.run)
    assert 'finish_reason = "context_overflow"' in source


# ── the counter is cached per trajectory prefix ─────────────────────────────
def test_cached_counter_calls_the_server_once_per_distinct_prefix() -> None:
    """A growing trajectory must not re-count its shared prefix."""
    from gigaforge.core.token_cache import CachedTokenCounter

    calls: list[int] = []

    def _counter(messages: Any, model: Any) -> int:
        calls.append(len(messages))
        return sum(len(str(m.get("content", ""))) for m in messages)

    cached = CachedTokenCounter(_counter)
    first = [{"role": "user", "content": "a" * 10}]
    second = first + [{"role": "assistant", "content": "b" * 10}]

    assert cached(first) == 10
    assert cached(first) == 10  # cache hit
    assert cached(second) == 20
    assert calls == [1, 2], "the shared prefix was counted twice"
    assert cached.stats()["hits"] == 1


def test_cached_counter_is_bounded() -> None:
    """A long run must not grow the cache without limit."""
    from gigaforge.core.token_cache import CachedTokenCounter

    cached = CachedTokenCounter(lambda m, model: 1, capacity=3)
    for index in range(10):
        cached([{"role": "user", "content": str(index)}])
    assert cached.size == 3


def test_cached_counter_fails_open() -> None:
    """A broken counter returns ``None`` so the guard keeps its estimate."""
    from gigaforge.core.token_cache import CachedTokenCounter

    def _boom(messages: Any, model: Any) -> int:
        raise RuntimeError("endpoint down")

    assert CachedTokenCounter(_boom)([{"role": "user", "content": "x"}]) is None


def test_trajectory_key_separates_models_and_roles() -> None:
    """Same text under a different model, or a different role, is a new count."""
    from gigaforge.core.token_cache import trajectory_key

    base = [{"role": "user", "content": "hello"}]
    assert trajectory_key(base, "GigaChat") != trajectory_key(base, "GigaChat-Pro")
    assert trajectory_key(base) != trajectory_key([{"role": "system", "content": "hello"}])


# ── the legacy provider is honest ───────────────────────────────────────────
def test_legacy_provider_count_tokens_is_a_constant_none() -> None:
    """No ``getattr`` dance: the legacy transport simply cannot count."""
    import asyncio

    from gigaforge.providers.gigachat import GigaChatProvider

    class _Client:
        pass

    provider = GigaChatProvider(_Client())  # type: ignore[arg-type]
    assert asyncio.run(provider.count_tokens([{"role": "user", "content": "x"}])) is None
    assert asyncio.run(
        provider.count_tokens([{"role": "user", "content": "x"}], functions=[{"name": "f"}])
    ) is None
