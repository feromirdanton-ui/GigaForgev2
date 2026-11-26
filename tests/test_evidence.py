"""Grounded evidence: a section's facts come from a search, not from memory.

The central test is that an unsourced item is distinguishable from a sourced
one — that distinction is the whole mechanism.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gigaforge.harness.evidence import (  # noqa: E402
    Evidence,
    EvidenceSet,
    evidence_prompt_block,
    gather,
    parse_search_results,
)


#: The exact shape ``format_results`` in ``web_search_tool`` produces.
OBSERVATION = """Search results for 'caching strategies Overview':
1. Cache replacement policies - Wikipedia
   https://en.wikipedia.org/wiki/Cache_replacement_policies
   A cache replacement policy decides which entry to evict.
2. HTTP caching - MDN
   https://developer.mozilla.org/en-US/docs/Web/HTTP/Caching
   HTTP caching stores responses to reduce latency.
"""


def test_a_search_observation_becomes_sourced_evidence() -> None:
    items = parse_search_results(OBSERVATION)
    assert len(items) == 2
    assert items[0].source == "https://en.wikipedia.org/wiki/Cache_replacement_policies"
    assert "evict" in items[0].claim
    assert all(item.sourced for item in items)


def test_a_result_with_no_url_is_dropped() -> None:
    """An uncitable result is exactly the "fact" this module keeps out."""
    text = "1. A fact with no source\n   no url here\n"
    assert parse_search_results(text) == []


def test_a_result_with_no_snippet_still_yields_a_source() -> None:
    """The title and URL are a source; the writer can be told to read it."""
    text = "1. Some page\n   https://example.com/page\n"
    items = parse_search_results(text)
    assert len(items) == 1
    assert items[0].claim == "Some page"
    assert items[0].sourced


def test_the_tool_s_no_results_text_is_not_evidence() -> None:
    """Treating "No results" as evidence would let a section claim grounding."""
    assert parse_search_results("No results for 'x'.") == []


# ── gather ─────────────────────────────────────────────────────────────────
def test_gather_returns_what_the_search_found() -> None:
    result = gather("caching", lambda q: OBSERVATION)
    assert result.grounded is True
    assert len(result.sourced) == 2
    assert result.query == "caching"


def test_a_failed_search_is_an_empty_set_not_an_exception() -> None:
    """The document must still be written; what changes is that it is ungrounded."""
    def boom(query: str) -> str:
        raise RuntimeError("network down")

    result = gather("caching", boom)
    assert result.grounded is False
    assert "network down" in result.error


def test_a_search_that_finds_nothing_is_ungrounded() -> None:
    result = gather("caching", lambda q: "No results for 'caching'.")
    assert result.grounded is False
    assert "no results" in result.error


def test_an_empty_query_does_not_search() -> None:
    called = {"n": 0}

    def search(query: str) -> str:
        called["n"] += 1
        return OBSERVATION

    result = gather("   ", search)
    assert called["n"] == 0
    assert result.error == "no query"


def test_gather_respects_the_limit() -> None:
    many = "\n".join(
        f"{i}. Title {i}\n   https://example.com/{i}\n   Snippet {i}."
        for i in range(1, 20)
    )
    result = gather("q", lambda q: many, limit=3)
    assert len(result.items) == 3


# ── the prompt block ───────────────────────────────────────────────────────
def test_a_grounded_section_is_given_its_sources() -> None:
    """A writer told "use only these facts" and given no sources invents one."""
    result = gather("caching", lambda q: OBSERVATION)
    block = evidence_prompt_block(result)
    assert "https://en.wikipedia.org" in block
    assert "Use only these facts" in block


def test_an_ungrounded_section_is_told_not_to_invent_a_source() -> None:
    block = evidence_prompt_block(EvidenceSet())
    assert "do not invent a source" in block
    assert "No sources were found" in block


def test_an_unsourced_item_renders_as_unsourced() -> None:
    """The distinction has to survive into the prompt, or it is only internal."""
    assert "unsourced" in Evidence(claim="a fact").render()
    assert "source:" in Evidence(claim="a fact", source="https://x").render()
