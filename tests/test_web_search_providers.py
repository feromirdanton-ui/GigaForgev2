"""``web_search``: both call shapes, and a provider that actually answers.

**The two defects these tests exist for**, both found by running the tool
against the live network rather than by reading it:

1. ``run`` assumed its argument was a string and called ``.strip()`` on it.  The
   runner passes a **dict** when the model emits structured arguments — which is
   the shape the prompt asks for — so a structured call raised
   ``AttributeError: 'dict' object has no attribute 'strip'``.  The tool was
   unreachable for its own documented call shape.
2. The only providers were DuckDuckGo's HTML endpoints, which answer ``HTTP
   202`` with an anti-bot page and zero result links from this sandbox.  Every
   search therefore returned "unavailable", and the model told the user "since
   web_search is unavailable, I cannot determine the current stable Python
   release".  The tool was not broken; its provider list was.

The tests are written so that a *single* provider would fail them: the
provider-order test asserts the Wikipedia endpoint is tried, and the parsing
tests assert the JSON response is understood, so a change that drops the
working provider breaks a test rather than silently degrading to "unavailable".
"""
from __future__ import annotations

import asyncio
import json

import pytest

from gigaforge.agents.web_search_tool import (
    SearchCache,
    WebSearchTool,
    parse_wikipedia,
    _providers,
)


WIKIPEDIA_RESPONSE = json.dumps({
    "batchcomplete": "",
    "query": {
        "searchinfo": {"totalhits": 6459},
        "search": [
            {
                "ns": 0,
                "title": "Python (programming language)",
                "pageid": 23862,
                "snippet": "Python is a <span class=\"searchmatch\">high-level</span> "
                           "language &amp; it is widely used.",
            },
            {"ns": 0, "title": "Outline of Python", "snippet": "An outline."},
        ],
    },
})


def _tool(opener, **kwargs) -> WebSearchTool:
    """A tool whose transport is a stub, so no test touches the network.

    Each test gets its **own** cache: the module-level one is process-wide by
    design (three modes of one task share it), which would make the second test
    to ask for "python" read the first test's answer and never call its own
    opener — the tests would pass while testing nothing.
    """
    kwargs.setdefault("cache", SearchCache())
    return WebSearchTool(opener=opener, **kwargs)


def _run(tool: WebSearchTool, payload) -> str:
    return asyncio.run(tool.run(payload))


class TestCallShapes:
    """Both shapes a caller sends must work; one of them used to raise."""

    def test_a_dict_query_is_understood(self) -> None:
        """The shape the prompt asks for.  This raised AttributeError."""
        tool = _tool(lambda url, timeout: WIKIPEDIA_RESPONSE)
        out = _run(tool, {"query": "python"})
        assert "Python (programming language)" in out

    def test_a_string_query_is_understood(self) -> None:
        tool = _tool(lambda url, timeout: WIKIPEDIA_RESPONSE)
        out = _run(tool, "python")
        assert "Python (programming language)" in out

    @pytest.mark.parametrize("payload", [{"q": "python"}, {"text": "python"}])
    def test_a_dict_with_an_alternative_key_is_understood(self, payload: dict) -> None:
        tool = _tool(lambda url, timeout: WIKIPEDIA_RESPONSE)
        assert "Python" in _run(tool, payload)

    def test_an_empty_query_is_an_observation_not_an_exception(self) -> None:
        tool = _tool(lambda url, timeout: WIKIPEDIA_RESPONSE)
        out = _run(tool, {"query": "   "})
        assert out.startswith("ERROR:") and "needs a query" in out

    def test_a_dict_with_no_recognised_key_still_searches(self) -> None:
        """Discarding the call would lose the model's work; join the values."""
        tool = _tool(lambda url, timeout: WIKIPEDIA_RESPONSE)
        assert "Python" in _run(tool, {"query_text": "python"})


class TestProviders:
    def test_the_wikipedia_endpoint_is_offered(self) -> None:
        """The only keyless provider measured to answer from this sandbox."""
        urls = [url for url, _ in _providers("python")]
        assert any("wikipedia.org" in url for url in urls), (
            "dropping the working provider would make every search 'unavailable'"
        )

    def test_a_configured_endpoint_comes_first(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GIGAFORGE_SEARCH_URL", "https://search.internal/?q={query}")
        first, _ = _providers("python")[0]
        assert first.startswith("https://search.internal/")

    def test_the_html_endpoints_remain_as_later_providers(self) -> None:
        urls = [url for url, _ in _providers("python")]
        assert any("duckduckgo" in url for url in urls)

    def test_the_query_is_url_encoded(self) -> None:
        urls = [url for url, _ in _providers("python 3.14 release")]
        assert all(" " not in url for url in urls)


class TestParseWikipedia:
    def test_titles_become_results(self) -> None:
        results = parse_wikipedia(WIKIPEDIA_RESPONSE)
        assert [r[0] for r in results] == [
            "Python (programming language)", "Outline of Python",
        ]

    def test_markup_and_entities_are_stripped_from_the_snippet(self) -> None:
        """The observation is read by a model; raw markup is wasted tokens."""
        snippet = parse_wikipedia(WIKIPEDIA_RESPONSE)[0][2]
        assert "<span" not in snippet
        assert "&amp;" not in snippet
        assert "high-level" in snippet and "&" in snippet

    def test_the_url_points_at_the_article(self) -> None:
        url = parse_wikipedia(WIKIPEDIA_RESPONSE)[0][1]
        assert url.startswith("https://en.wikipedia.org/wiki/")

    def test_a_non_json_body_is_no_results(self) -> None:
        assert parse_wikipedia("<html>not json</html>") == []

    def test_an_empty_response_is_no_results(self) -> None:
        assert parse_wikipedia(json.dumps({})) == []


class TestFallback:
    def test_a_dead_first_provider_falls_through_to_the_next(self) -> None:
        """The measured shape: Wikipedia fails, an HTML provider answers.

        A tool that stopped at the first provider would report "unavailable"
        here, which is exactly the live failure.
        """
        calls: list[str] = []

        def opener(url: str, timeout: float) -> str:
            calls.append(url)
            if "wikipedia" in url:
                raise OSError("network unreachable")
            return (
                '<a class="result__a" href="https://example.com/x">Example result</a>'
            )

        out = _run(_tool(opener), {"query": "python"})
        assert "example.com" in out or "Example result" in out
        assert any("wikipedia" in url for url in calls)

    def test_every_provider_failing_is_still_an_observation(self) -> None:
        def opener(url: str, timeout: float) -> str:
            raise OSError("network unreachable")

        out = _run(_tool(opener), {"query": "python"})
        assert out.startswith("ERROR: web_search unavailable")
        assert "continue without it" in out

    def test_the_budget_stops_the_second_call(self) -> None:
        """A refusal is an observation the model can act on, not an exception."""
        tool = _tool(lambda url, timeout: WIKIPEDIA_RESPONSE)
        assert "Python" in _run(tool, {"query": "one"})
        assert "Python" in _run(tool, {"query": "two"})
        assert "Python" in _run(tool, {"query": "three"})
        out = _run(tool, {"query": "four"})
        assert "budget exhausted" in out

    def test_a_repeated_query_is_served_from_cache(self) -> None:
        calls: list[str] = []

        def opener(url: str, timeout: float) -> str:
            calls.append(url)
            return WIKIPEDIA_RESPONSE

        tool = _tool(opener)
        _run(tool, {"query": "python"})
        before = len(calls)
        out = _run(tool, {"query": "python"})
        assert len(calls) == before, "a cached query must not hit the network"
        assert "cached" in out
