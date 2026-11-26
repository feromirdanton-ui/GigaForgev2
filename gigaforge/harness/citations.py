"""Citation F1: did the answer cite the sources the tools actually returned?

**Why this exists.**  GigaForge already collects citations — the tool
observations carry URLs, and the answer prompt says "use only a URL from the
list above".  Nothing checked whether the answer obeyed.  A run could cite
nothing, cite a URL it invented, or cite one of twelve sources and score the
same, because the gate only asked "is there text".

That is the same class of defect as the ladder without a drive: the data was
collected and never used.  PaperOrchestra's ``citation_f1`` is the right shape
of measurement — precision and recall over the citation set — and it is
deterministic, so it belongs in the harness and not in a prompt.

**What it measures, precisely.**

* **recall** — of the sources the tools returned, how many the answer cited.
  Low recall means the answer ignored its evidence.
* **precision** — of the URLs the answer cited, how many the tools returned.
  Low precision means the answer invented a source, which is worse than citing
  nothing: it is a confident false claim.

**What it must not do.**  It must not punish an answer for citing *fewer*
sources than were available.  A question answered from one authoritative page
is a good answer, and demanding all twelve would reward padding.  So recall is
reported, not gated, and the gate is precision: every cited URL must be real.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence
from urllib.parse import urlsplit, urlunsplit

__all__ = [
    "CitationScore",
    "extract_urls",
    "normalise_url",
    "citation_f1",
    "citation_issue",
]

#: A URL in prose.  Deliberately permissive about the path and strict about the
#: scheme: ``example.com`` without a scheme is not a citation, it is a mention.
_URL_RE = re.compile(r"https?://[^\s<>\"'\)\]\}]+", re.IGNORECASE)

#: Trailing punctuation that belongs to the sentence, not the URL.
_TRAILING = ".,;:!?»”\"'"


def normalise_url(url: str) -> str:
    """A comparable form of a URL: scheme+host lowercased, fragment dropped.

    ``https://Example.com/a/?b=1#frag`` and ``https://example.com/a?b=1`` are the
    same source.  Comparing raw strings would call them different and report a
    precision failure for a correct citation.
    """
    raw = (url or "").strip().rstrip(_TRAILING)
    if not raw:
        return ""
    try:
        parts = urlsplit(raw)
    except ValueError:
        return raw.lower()
    host = (parts.hostname or "").lower()
    if not host:
        return raw.lower()
    path = parts.path or "/"
    if path != "/" and path.endswith("/"):
        path = path[:-1]
    return urlunsplit((parts.scheme.lower(), host, path, parts.query, ""))


def extract_urls(text: str) -> list[str]:
    """Every URL in ``text``, normalised, in first-seen order."""
    seen: list[str] = []
    for match in _URL_RE.findall(text or ""):
        url = normalise_url(match)
        if url and url not in seen:
            seen.append(url)
    return seen


@dataclass
class CitationScore:
    """Precision and recall of an answer's citations against its evidence."""

    cited: list[str] = field(default_factory=list)
    available: list[str] = field(default_factory=list)
    supported: list[str] = field(default_factory=list)
    invented: list[str] = field(default_factory=list)
    missed: list[str] = field(default_factory=list)

    @property
    def precision(self) -> float:
        """Of what was cited, the share that the tools actually returned."""
        if not self.cited:
            return 0.0
        return len(self.supported) / len(self.cited)

    @property
    def recall(self) -> float:
        """Of what the tools returned, the share the answer cited."""
        if not self.available:
            return 0.0
        return len(self.supported) / len(self.available)

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        if p + r == 0:
            return 0.0
        return 2 * p * r / (p + r)

    def to_dict(self) -> dict[str, Any]:
        return {
            "precision": round(self.precision, 4),
            "recall": round(self.recall, 4),
            "f1": round(self.f1, 4),
            "cited": len(self.cited),
            "available": len(self.available),
            "supported": len(self.supported),
            "invented": self.invented[:5],
            "missed": self.missed[:5],
        }


def citation_f1(answer: str, available: Iterable[str]) -> CitationScore:
    """Score ``answer``'s citations against the URLs the tools returned."""
    cited = extract_urls(answer)
    pool = []
    for url in available or ():
        norm = normalise_url(str(url))
        if norm and norm not in pool:
            pool.append(norm)
    pool_set = set(pool)
    cited_set = set(cited)
    return CitationScore(
        cited=cited,
        available=pool,
        supported=[u for u in cited if u in pool_set],
        invented=[u for u in cited if u not in pool_set],
        missed=[u for u in pool if u not in cited_set],
    )


def citation_issue(
    answer: str,
    available: Sequence[str],
    *,
    require_citation: bool = False,
) -> str:
    """The one thing wrong with the citations, or ``""`` when nothing is.

    **Precision is the gate; recall is not.**  An invented URL is a confident
    false claim and must fail.  Citing fewer sources than were available is a
    judgement the answer is allowed to make — a question answered from one
    authoritative page is a good answer, and demanding all of them would reward
    padding.

    ``require_citation`` is for the case where the goal explicitly asked for
    sources: then an answer with no citation at all is incomplete, and that is
    a different failure from citing the wrong one.
    """
    score = citation_f1(answer, available)
    if score.invented:
        return (
            "the answer cites source(s) the tools never returned: "
            + ", ".join(score.invented[:3])
        )
    if require_citation and not score.cited:
        return "the answer cites no source, and the goal asked for sources"
    return ""
