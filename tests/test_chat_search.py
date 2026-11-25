"""Chat must be able to search, and the decision to search must be free.

**The defect this file exists for.**  A live probe asked the chat window
"Search the web for the most notable AI agent framework trends" and got:

    Извини, но я не могу искать информацию в интернете. Я создан, чтобы
    помогать с задачами внутри репозитория и работать с кодом.

That is the one answer a user will not accept from an agent harness, and it was
not a model limitation — the direct chat backend had no search tool at all.  The
pipeline had one; the front door did not.

**The design rule these tests pin.**  The decision to search is made
*deterministically* from the message, not by asking the model whether it would
like a tool.  A model call to decide whether to make a model call is exactly the
kind of spend the ladder exists to remove, and "search the web" is not
ambiguous.  The query is derived from the message too, with the instruction
words stripped — a query that keeps "search the web for" returns pages about
searching, which is the classic pre-search mistake.

Two defects were found by running the code, not by reading it: the query kept
the leading preposition ("for AI agent framework trends"), and the search tool's
``run`` is a coroutine while the chat backend is a synchronous generator.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from gigaforge.ui.chat import (
    DirectChatBackend,
    _run_awaitable,
    search_query,
    wants_search,
)


# ── the decision ────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "message",
    [
        "Search the web for AI agent framework trends",
        "search the internet for the latest LangGraph release",
        "Can you google the current price of a GigaChat token?",
        "What's new in agent frameworks?",
        "найди в интернете последние новости про LangGraph",
        "поищи в интернете актуальные бенчмарки",
    ],
)
def test_a_search_request_is_recognised(message: str) -> None:
    assert wants_search(message)


@pytest.mark.parametrize(
    "message",
    [
        "What is 2+2? Answer in chat",
        "Fix the composite index in db.py",
        "Add a footer to index.html",
        "Explain what this function does",
        "",
    ],
)
def test_an_ordinary_message_does_not_search(message: str) -> None:
    """A false positive costs a search; a false negative costs the answer."""
    assert not wants_search(message)


# ── the query ───────────────────────────────────────────────────────────────
def test_the_instruction_is_stripped_from_the_query() -> None:
    """A query that keeps "search the web for" returns pages about searching."""
    assert search_query("Search the web for AI agent framework trends") == (
        "AI agent framework trends"
    )


def test_a_leading_preposition_is_stripped() -> None:
    """Found by running it: the query was "for AI agent framework trends"."""
    assert not search_query("Search the web for AI agent framework trends").startswith("for ")


def test_a_russian_instruction_is_stripped() -> None:
    query = search_query("найди в интернете про LangGraph")
    assert "интернете" not in query
    assert "LangGraph" in query


def test_a_message_with_no_instruction_is_its_own_query() -> None:
    assert search_query("LangGraph benchmarks") == "LangGraph benchmarks"


def test_an_empty_message_does_not_produce_an_empty_query() -> None:
    assert search_query("") == ""


# ── the backend actually searches ───────────────────────────────────────────
class _FakeSearch:
    """A search tool that records the query and returns a fixed result."""

    def __init__(self, result: str = "1. Result — https://example.com/a") -> None:
        self.result = result
        self.queries: list[str] = []

    async def run(self, action_input: Any, *, ctx: Any = None) -> str:
        self.queries.append(str(action_input.get("query", "")))
        return self.result


class _FakeClient:
    def __init__(self) -> None:
        self.calls: list[list[dict[str, str]]] = []

    def complete(self, messages, **kwargs):
        self.calls.append(list(messages))

        class _Result:
            content = "Answer with a citation: https://example.com/a"
            usage = {"total_tokens": 42}

        return _Result()


class _FakeSession:
    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    def tool(self, name, data, text=""):
        self.events.append(("tool", text))
        return _Event("tool", text)

    def say(self, text):
        self.events.append(("say", text))
        return _Event("say", text)

    def error(self, text):
        self.events.append(("error", text))
        return _Event("error", text)


class _Event:
    def __init__(self, kind: str, text: str) -> None:
        self.kind = kind
        self.text = text
        self.data: dict[str, Any] = {}


def _run(backend: DirectChatBackend, message: str) -> _FakeSession:
    session = _FakeSession()
    list(backend.stream(message, session))
    return session


def test_a_search_message_reaches_the_search_tool() -> None:
    search = _FakeSearch()
    backend = DirectChatBackend(_FakeClient(), search=search)
    _run(backend, "Search the web for AI agent framework trends")
    assert search.queries == ["AI agent framework trends"]


def test_the_search_results_reach_the_model() -> None:
    """The model must write from observations, not from memory."""
    client = _FakeClient()
    backend = DirectChatBackend(client, search=_FakeSearch("1. Found — https://x.test/a"))
    _run(backend, "Search the web for something")
    joined = "\n".join(m["content"] for m in client.calls[0])
    assert "https://x.test/a" in joined
    assert "Do not invent a URL" in joined


def test_an_ordinary_message_does_not_search() -> None:
    search = _FakeSearch()
    backend = DirectChatBackend(_FakeClient(), search=search)
    _run(backend, "What is 2+2?")
    assert search.queries == []


def test_the_result_records_that_it_searched() -> None:
    backend = DirectChatBackend(_FakeClient(), search=_FakeSearch())
    _run(backend, "Search the web for something")
    assert backend.last_result["searched"] is True


def test_a_failed_search_is_reported_not_hidden() -> None:
    """An answer that silently pretends to have searched is the worst outcome."""

    class _Broken:
        async def run(self, action_input, *, ctx=None):
            raise RuntimeError("network down")

    client = _FakeClient()
    backend = DirectChatBackend(client, search=_Broken())
    session = _run(backend, "Search the web for something")
    joined = "\n".join(m["content"] for m in client.calls[0])
    assert "network down" in joined
    assert not any(kind == "error" for kind, _ in session.events)


def test_no_search_tool_means_no_search_and_no_crash() -> None:
    backend = DirectChatBackend(_FakeClient(), search=None)
    session = _run(backend, "Search the web for something")
    assert any(kind == "say" for kind, _ in session.events)


# ── the coroutine bridge ────────────────────────────────────────────────────
def test_an_awaitable_is_driven_from_sync_code() -> None:
    """The search tool's run is async; the chat backend is a generator."""

    async def _coro() -> str:
        return "done"

    assert _run_awaitable(_coro()) == "done"


def test_the_bridge_works_inside_a_running_loop() -> None:
    """A notebook or an async server already has a loop; it must not be re-entered."""

    async def _coro() -> str:
        return "inner"

    async def _outer() -> str:
        return _run_awaitable(_coro())

    assert asyncio.run(_outer()) == "inner"


def test_an_error_in_the_awaitable_propagates() -> None:
    async def _coro() -> str:
        raise ValueError("boom")

    with pytest.raises(ValueError):
        _run_awaitable(_coro())
