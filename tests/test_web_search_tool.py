"""Web search: budgeted, cached, and honest when the network is gone.

A search is the most expensive observation in the loop, and the three failure
modes are all silent ones:

* **no budget** — the model searches on every step and the run dies on tokens;
* **no cache** — one task runs goal, then agent, then subagents, and each mode
  pays for the same query again;
* **no honest failure** — a tool that returns ``""`` on a network error reads
  to the model as "there are no results", which is the opposite of the truth.

Each test below pins one of those, and the transport is injected so the suite
never touches the network.
"""
from __future__ import annotations

import asyncio
import urllib.error

import pytest

from gigaforge.agents.observation_cache import CACHEABLE_TOOLS, READ_ONLY_TOOLS
from gigaforge.agents.web_search_tool import (
    SearchBudget,
    SearchCache,
    WebSearchTool,
    format_results,
    parse_results,
)

PAGE = """
<html><body>
<a class="result__a" href="https://example.com/a">First <b>Result</b></a>
<a class="result__snippet" href="x">A snippet about the first result.</a>
<a class="result__a" href="https://example.com/b">Second Result</a>
<a class="result__snippet" href="x">Another snippet.</a>
</body></html>
"""


def _run(tool: WebSearchTool, query: str) -> str:
    return asyncio.run(tool.run(query))


class TestParsing:
    def test_parses_titles_urls_and_snippets(self) -> None:
        results = parse_results(PAGE)
        assert len(results) == 2
        assert results[0][0] == "First Result"
        assert results[0][1] == "https://example.com/a"
        assert "snippet about the first" in results[0][2]

    def test_unwraps_a_redirect_url(self) -> None:
        page = (
            '<a class="result__a" href="https://duckduckgo.com/l/?uddg='
            'https%3A%2F%2Freal.example%2Fx">T</a>'
        )
        assert parse_results(page)[0][1] == "https://real.example/x"

    def test_a_changed_page_yields_no_results_not_an_exception(self) -> None:
        assert parse_results("<html><body>nothing here</body></html>") == []

    def test_format_is_readable(self) -> None:
        text = format_results("q", [("T", "https://e.com", "S")])
        assert "Search results for 'q'" in text
        assert "https://e.com" in text

    def test_format_says_so_when_empty(self) -> None:
        assert "No results" in format_results("q", [])


class TestBudget:
    def test_budget_is_spent_and_then_refused(self) -> None:
        calls: list[str] = []

        def opener(url: str, timeout: float) -> str:
            calls.append(url)
            return PAGE

        tool = WebSearchTool(budget=SearchBudget(max_calls=2), cache=SearchCache(),
                             opener=opener)
        assert "Search results" in _run(tool, "one")
        assert "Search results" in _run(tool, "two")
        third = _run(tool, "three")
        assert third.startswith("ERROR")
        assert "budget exhausted" in third
        assert len(calls) == 2, "the third call reached the network"

    def test_a_refusal_is_an_observation_not_an_exception(self) -> None:
        tool = WebSearchTool(budget=SearchBudget(max_calls=0), cache=SearchCache(),
                             opener=lambda url, timeout: PAGE)
        out = _run(tool, "q")
        assert out.startswith("ERROR")
        assert "continue" in out.lower() or "work from" in out.lower()

    def test_budget_describe(self) -> None:
        budget = SearchBudget(max_calls=3)
        budget.take()
        assert budget.describe() == {"max_calls": 3, "used": 1, "remaining": 2}


class TestCache:
    def test_a_repeat_query_does_not_hit_the_network(self) -> None:
        calls: list[str] = []

        def opener(url: str, timeout: float) -> str:
            calls.append(url)
            return PAGE

        tool = WebSearchTool(budget=SearchBudget(max_calls=5), cache=SearchCache(),
                             opener=opener)
        first = _run(tool, "gigaforge harness")
        second = _run(tool, "  GigaForge   Harness  ")
        assert len(calls) == 1, "the cache did not absorb the repeat"
        assert "cached" in second
        assert first.split("\n")[0] == second.split("\n")[0]

    def test_a_cache_hit_does_not_spend_budget(self) -> None:
        tool = WebSearchTool(budget=SearchBudget(max_calls=1), cache=SearchCache(),
                             opener=lambda url, timeout: PAGE)
        _run(tool, "q")
        _run(tool, "q")
        assert tool.budget.used == 1

    def test_the_cache_is_shared_across_tools_by_default(self) -> None:
        """One task runs goal, then agent, then subagents — each builds its own
        tool, and the second mode must not pay for the same query again."""
        from gigaforge.agents.web_search_tool import SEARCH_CACHE

        SEARCH_CACHE.clear()
        calls: list[str] = []

        def opener(url: str, timeout: float) -> str:
            calls.append(url)
            return PAGE

        first = WebSearchTool(budget=SearchBudget(max_calls=3), opener=opener)
        second = WebSearchTool(budget=SearchBudget(max_calls=3), opener=opener)
        _run(first, "shared query")
        _run(second, "shared query")
        assert len(calls) == 1
        SEARCH_CACHE.clear()

    def test_an_expired_entry_is_not_served(self) -> None:
        cache = SearchCache(ttl=0.0)
        cache.put("q", "old")
        assert cache.get("q") is None

    def test_cache_describe(self) -> None:
        cache = SearchCache()
        cache.put("q", "v")
        cache.get("q")
        assert cache.describe()["hits"] == 1


class TestHonestDegradation:
    def test_network_error_is_explicit(self) -> None:
        def opener(url: str, timeout: float) -> str:
            raise urllib.error.URLError("no route to host")

        tool = WebSearchTool(cache=SearchCache(), opener=opener)
        out = _run(tool, "q")
        assert out.startswith("ERROR")
        assert "unavailable" in out
        assert "continue without it" in out

    def test_http_error_is_explicit(self) -> None:
        def opener(url: str, timeout: float) -> str:
            raise urllib.error.HTTPError("u", 503, "busy", {}, None)

        tool = WebSearchTool(cache=SearchCache(), opener=opener)
        out = _run(tool, "q")
        assert "HTTP 503" in out

    def test_timeout_is_explicit(self) -> None:
        def opener(url: str, timeout: float) -> str:
            raise TimeoutError("timed out")

        tool = WebSearchTool(cache=SearchCache(), opener=opener)
        assert "unavailable" in _run(tool, "q")

    def test_an_unexpected_error_does_not_kill_the_loop(self) -> None:
        def opener(url: str, timeout: float) -> str:
            raise RuntimeError("something odd")

        tool = WebSearchTool(cache=SearchCache(), opener=opener)
        out = _run(tool, "q")
        assert out.startswith("ERROR")

    def test_an_empty_query_is_refused(self) -> None:
        tool = WebSearchTool(cache=SearchCache(), opener=lambda u, t: PAGE)
        assert _run(tool, "   ").startswith("ERROR")

    def test_a_failed_search_is_not_cached(self) -> None:
        """Caching a failure would make a transient outage permanent."""
        state = {"fail": True}

        def opener(url: str, timeout: float) -> str:
            if state["fail"]:
                raise urllib.error.URLError("down")
            return PAGE

        tool = WebSearchTool(cache=SearchCache(), opener=opener)
        assert _run(tool, "q").startswith("ERROR")
        state["fail"] = False
        assert "Search results" in _run(tool, "q")


class TestRegistration:
    def test_the_tool_is_registered_in_every_registry(self) -> None:
        from gigaforge.agents.react import ToolRegistry

        registry = ToolRegistry()
        registry.install_defaults(sandbox=None)
        assert "web_search" in registry.names()

    def test_web_search_is_read_only_but_not_tree_cached(self) -> None:
        assert "web_search" in READ_ONLY_TOOLS
        assert "web_search" not in CACHEABLE_TOOLS, (
            "a search result is a fact about the world, not about the tree; "
            "the tree-stamp invalidation would be meaningless for it"
        )

    def test_web_search_may_run_in_a_parallel_batch(self) -> None:
        import inspect

        from gigaforge.agents.react import ReActRunner

        source = inspect.getsource(ReActRunner._run_parallel)
        assert "READ_ONLY_TOOLS" in source

    def test_the_prompt_mentions_the_tool(self) -> None:
        from gigaforge.agents.react import _REACT_PROMPT

        assert "web_search" in _REACT_PROMPT
        assert "кэшируются" in _REACT_PROMPT

    def test_the_compressor_kind_is_defined(self) -> None:
        from gigaforge.agents.react import _kind_for_tool

        assert _kind_for_tool("web_search") == "builderr"
