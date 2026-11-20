"""Web search as a harness tool — budgeted, cached, and honest when offline.

**Why it is a harness tool and not a model feature.**  A model asked to "look it
up" will either hallucinate a URL or spend a step on a tool that does not exist.
The harness owns the call: it knows the budget, it can cache, and it can say
"the network is unavailable" instead of inventing an answer.

**Three properties, each of which is a defect if missing.**

1. **Budget.**  A search is the most expensive observation in the loop — a page
   of results is thousands of tokens.  ``max_calls`` bounds it per run (default
   3), and the refusal is an *observation*, not an exception: the model learns
   the rule and continues with what it has.
2. **Cache.**  The same query is asked repeatedly across a run and across the
   three modes (goal / agent / subagents) that a single task may run.  Results
   are cached process-wide with a TTL, so the second mode pays nothing.  The
   cache key is the normalised query, and the TTL exists because a stale search
   result is a correctness bug, not a saving.
3. **Honest degradation.**  No network, a non-200, a timeout, an unparseable
   page — each returns a short, explicit observation.  The tool never raises
   into the loop and never returns an empty string that reads like "no results
   exist".  A model told "search unavailable" can fall back to the repository;
   a model told "" will conclude the world is empty.

**Provider.**  The default provider is DuckDuckGo's HTML endpoint, which needs
no key.  ``GIGAFORGE_SEARCH_URL`` overrides it (``{query}`` is substituted), so
a deployment can point at its own search service without a code change.  The
parser is deliberately tolerant: it reads result links and titles out of the
HTML and gives up quietly rather than guessing.
"""
from __future__ import annotations

import html as _html
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

__all__ = ["WebSearchTool", "SearchBudget", "SearchCache", "SEARCH_CACHE"]

#: Default endpoint.  ``{query}`` is replaced with the URL-encoded query.
DEFAULT_SEARCH_URL = "https://html.duckduckgo.com/html/?q={query}"

#: How many results are kept.  More than this is tokens the model will not read.
MAX_RESULTS = 5

#: Characters of a snippet kept per result.
MAX_SNIPPET = 240

#: Seconds a cached result stays valid.  A search result is a fact about the
#: world; an hour is short enough that a stale page is unlikely and long enough
#: that the three modes of one task share it.
CACHE_TTL_SECONDS = 3600.0

#: Hard cap on the observation, in characters.  The runner compresses further;
#: this stops a pathological page from reaching the compressor at all.
MAX_OBSERVATION_CHARS = 4000


@dataclass
class SearchBudget:
    """Per-run call budget.  A refusal is an observation, never an exception."""

    max_calls: int = 3
    used: int = 0

    @property
    def remaining(self) -> int:
        return max(0, self.max_calls - self.used)

    def take(self) -> bool:
        if self.used >= self.max_calls:
            return False
        self.used += 1
        return True

    def describe(self) -> dict[str, Any]:
        return {"max_calls": self.max_calls, "used": self.used,
                "remaining": self.remaining}


@dataclass
class SearchCache:
    """Process-wide cache of search results, keyed by the normalised query.

    Process-wide on purpose: one task may run goal, then agent, then subagents,
    and each of those builds its own ``ReActContext``.  A per-run cache would
    make the second mode pay for the same query again — which is exactly the
    "constant retrieval with token spend" the harness is supposed to remove.
    """

    ttl: float = CACHE_TTL_SECONDS
    _entries: dict[str, tuple[float, str]] = field(default_factory=dict)
    hits: int = 0
    misses: int = 0

    @staticmethod
    def key(query: str) -> str:
        return " ".join((query or "").lower().split())

    def get(self, query: str) -> str | None:
        entry = self._entries.get(self.key(query))
        if entry is None:
            self.misses += 1
            return None
        stamp, value = entry
        if time.monotonic() - stamp > self.ttl:
            self._entries.pop(self.key(query), None)
            self.misses += 1
            return None
        self.hits += 1
        return value

    def put(self, query: str, value: str) -> None:
        self._entries[self.key(query)] = (time.monotonic(), value)

    def clear(self) -> None:
        self._entries.clear()

    def describe(self) -> dict[str, Any]:
        return {"entries": len(self._entries), "hits": self.hits,
                "misses": self.misses, "ttl": self.ttl}


#: The cache the tool uses unless a caller injects its own.
SEARCH_CACHE = SearchCache()


def _endpoint(query: str) -> str:
    template = os.environ.get("GIGAFORGE_SEARCH_URL", "") or DEFAULT_SEARCH_URL
    return template.replace("{query}", urllib.parse.quote_plus(query))


_RESULT_RE = re.compile(
    r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
    re.S | re.I,
)
_SNIPPET_RE = re.compile(
    r'<a[^>]+class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</a>', re.S | re.I
)
_TAG_RE = re.compile(r"<[^>]+>")


def _clean(fragment: str) -> str:
    text = _TAG_RE.sub(" ", fragment or "")
    text = _html.unescape(text)
    return " ".join(text.split())


def parse_results(page: str, *, limit: int = MAX_RESULTS) -> list[tuple[str, str, str]]:
    """``[(title, url, snippet), ...]`` from a results page.

    Tolerant by design: a page whose markup changed yields fewer results, not an
    exception.  The caller reports "no results" only when the page really had
    none.
    """
    titles = _RESULT_RE.findall(page or "")
    snippets = [_clean(s) for s in _SNIPPET_RE.findall(page or "")]
    out: list[tuple[str, str, str]] = []
    for index, (href, raw_title) in enumerate(titles[:limit]):
        url = _html.unescape(href)
        # DuckDuckGo wraps links in a redirect; unwrap when possible.
        if "uddg=" in url:
            try:
                url = urllib.parse.unquote(
                    urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["uddg"][0]
                )
            except (KeyError, IndexError, ValueError):
                pass
        snippet = snippets[index][:MAX_SNIPPET] if index < len(snippets) else ""
        out.append((_clean(raw_title)[:160], url, snippet))
    return out


def format_results(query: str, results: list[tuple[str, str, str]]) -> str:
    """The observation text the model reads."""
    if not results:
        return f"No results for {query!r}."
    lines = [f"Search results for {query!r}:"]
    for index, (title, url, snippet) in enumerate(results, start=1):
        lines.append(f"{index}. {title}\n   {url}")
        if snippet:
            lines.append(f"   {snippet}")
    text = "\n".join(lines)
    if len(text) > MAX_OBSERVATION_CHARS:
        text = text[:MAX_OBSERVATION_CHARS] + "\n[truncated]"
    return text


class WebSearchTool:
    """``web_search`` — one bounded, cached, honest search per call."""

    name = "web_search"

    def __init__(
        self,
        *,
        budget: SearchBudget | None = None,
        cache: SearchCache | None = None,
        timeout: float = 8.0,
        opener: Any = None,
    ) -> None:
        self.budget = budget or SearchBudget()
        self.cache = cache if cache is not None else SEARCH_CACHE
        self.timeout = timeout
        # Injectable for tests: a callable ``(url, timeout) -> str``.
        self._opener = opener or self._http_get

    # -- transport -----------------------------------------------------------
    def _http_get(self, url: str, timeout: float) -> str:
        request = urllib.request.Request(
            url,
            headers={
                # A default urllib agent is refused by most search endpoints.
                "User-Agent": "Mozilla/5.0 (compatible; GigaForge/1.0)",
                "Accept-Language": "en;q=0.9",
            },
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            charset = response.headers.get_content_charset() or "utf-8"
            return response.read().decode(charset, errors="replace")

    # -- tool protocol -------------------------------------------------------
    async def run(self, action_input: str, *, ctx: Any = None) -> str:
        query = (action_input or "").strip()
        if not query:
            return "ERROR: web_search needs a query"

        cached = self.cache.get(query)
        if cached is not None:
            return f"{cached}\n[cached: this query was already searched in this session]"

        if not self.budget.take():
            return (
                f"ERROR: web_search budget exhausted ({self.budget.max_calls} calls "
                "per run). Work from the repository and what you already have; do "
                "not ask again."
            )

        try:
            page = self._opener(_endpoint(query), self.timeout)
        except urllib.error.HTTPError as exc:
            return f"ERROR: web_search unavailable (HTTP {exc.code}); continue without it."
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return (
                "ERROR: web_search unavailable (network: "
                f"{str(exc)[:120]}); continue without it."
            )
        except Exception as exc:  # noqa: BLE001 - a tool must not kill the loop
            return f"ERROR: web_search failed ({str(exc)[:120]}); continue without it."

        results = parse_results(page)
        text = format_results(query, results)
        self.cache.put(query, text)
        return text

    def describe(self) -> dict[str, Any]:
        return {"budget": self.budget.describe(), "cache": self.cache.describe()}
