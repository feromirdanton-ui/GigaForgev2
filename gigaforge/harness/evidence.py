"""Grounded evidence: a section's facts come from a search, not from the model.

**The defect this closes.**  The document pipeline's literature stage asked the
model "list the concrete facts needed to write section X".  That is a request
for the model's memory, and a model asked for facts produces them whether or not
it has them — the same failure the citation gate catches after the fact, moved
one stage earlier where it is cheaper to prevent.  PaperOrchestra's literature
agent *searches*; the model then writes from what the search returned.

**What is ported and what is not.**  The mechanism — gather, then write only
from what was gathered — is ported.  Semantic Scholar is not: it needs an API
key GigaForge does not have, and a document about a codebase does not need
academic citations.  The search is the harness's own ``web_search`` tool, which
is already bounded, cached and honest about returning nothing.

**The rule that makes it a gate.**  Evidence is a list of *sourced* items: a
claim and the URL it came from.  A section written from an empty evidence list
is a section written from the model's memory, and the pipeline marks it as such
rather than pretending the search succeeded.  That distinction is what lets the
acceptance gate say "this section has no evidence" instead of "this section is
short".
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence

__all__ = [
    "Evidence",
    "EvidenceSet",
    "parse_search_results",
    "gather",
    "evidence_prompt_block",
]


@dataclass(frozen=True)
class Evidence:
    """One sourced fact: what it says, and where it came from.

    ``source`` is required.  An evidence item with no source is the model's
    memory wearing a fact's clothes, and the whole point of this module is that
    the two are distinguishable.
    """

    claim: str
    source: str = ""
    title: str = ""

    @property
    def sourced(self) -> bool:
        return bool(self.source.strip())

    def to_dict(self) -> dict[str, str]:
        return {"claim": self.claim, "source": self.source, "title": self.title}

    def render(self) -> str:
        if self.source:
            return f"- {self.claim} (source: {self.source})"
        return f"- {self.claim} (unsourced)"


@dataclass
class EvidenceSet:
    """What a section may assert, and whether any of it was actually found."""

    items: list[Evidence] = field(default_factory=list)
    query: str = ""
    error: str = ""

    @property
    def sourced(self) -> list[Evidence]:
        return [item for item in self.items if item.sourced]

    @property
    def grounded(self) -> bool:
        """True when at least one item came from a real source.

        A section with no grounded evidence is not "a section with weak
        evidence" — it is a section the model will write from memory, and the
        caller needs to know that before it spends a call on it.
        """
        return bool(self.sourced)

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "items": [item.to_dict() for item in self.items],
            "grounded": self.grounded,
            "error": self.error,
        }


#: The shape ``format_results`` in ``web_search_tool`` produces:
#:
#:     1. Some title
#:        https://example.com/page
#:        A snippet of the page.
#:
#: Parsed rather than re-fetched: the tool already returned this text, and a
#: second request for the same query is a second cost for the same answer.
_RESULT_RE = re.compile(
    r"^\s*(\d+)\.\s+(?P<title>.+?)\s*$\n\s*(?P<url>https?://\S+)\s*$"
    r"(?:\n\s+(?P<snippet>.+?)\s*$)?",
    re.MULTILINE,
)


def parse_search_results(text: str) -> list[Evidence]:
    """Turn a ``web_search`` observation into sourced evidence items.

    A result with no snippet still yields an item: the title and URL are a
    source, and the writer can be told to read it.  A result with no URL is
    dropped — it cannot be cited, and an uncitable result is exactly the
    "fact" this module exists to keep out of the document.
    """
    out: list[Evidence] = []
    for match in _RESULT_RE.finditer(text or ""):
        url = (match.group("url") or "").strip()
        if not url:
            continue
        title = (match.group("title") or "").strip()
        snippet = (match.group("snippet") or "").strip()
        claim = snippet or title
        if not claim:
            continue
        out.append(Evidence(claim=claim, source=url, title=title))
    return out


def gather(
    query: str,
    search: Callable[[str], str],
    *,
    limit: int = 6,
) -> EvidenceSet:
    """Run one search and return what it actually found.

    **A failed search is an empty set, not an exception.**  The document must
    still be written; what changes is that the section is marked ungrounded, so
    the acceptance gate can say so.  Raising here would turn "the search was
    down" into "the document failed", which is a different and worse claim.
    """
    query = (query or "").strip()
    if not query:
        return EvidenceSet(query="", error="no query")
    try:
        raw = search(query)
    except Exception as exc:  # noqa: BLE001 - a failed search is empty evidence
        return EvidenceSet(query=query, error=f"{type(exc).__name__}: {exc}")
    items = parse_search_results(str(raw or ""))
    if not items:
        # The tool's own "no results" text is not evidence, and treating it as
        # one would let a section claim grounding it does not have.
        return EvidenceSet(query=query, error="the search returned no results")
    return EvidenceSet(items=items[:max(1, limit)], query=query)


def evidence_prompt_block(evidence: EvidenceSet, *, limit: int = 8) -> str:
    """The block a section prompt carries: the facts, and their sources.

    **Why the sources are in the prompt.**  A writer told "use only these facts"
    and given no sources will still invent a citation when the document asks for
    one.  Giving it the URLs makes the honest answer the easy one.
    """
    if not evidence.grounded:
        return (
            "No sources were found for this section. Write only what you can "
            "state without a citation, and do not invent a source."
        )
    lines = ["Use only these facts, and cite them by their source URL:"]
    for item in evidence.sourced[:max(1, limit)]:
        lines.append(item.render())
    return "\n".join(lines)
