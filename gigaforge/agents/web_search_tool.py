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
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any

__all__ = ["WebSearchTool", "SearchBudget", "SearchCache", "SEARCH_CACHE"]

#: Default endpoint.  ``{query}`` is replaced with the URL-encoded query.
DEFAULT_SEARCH_URL = "https://html.duckduckgo.com/html/?q={query}"
DEFAULT_LITE_SEARCH_URL = "https://lite.duckduckgo.com/lite/?q={query}"

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


def _search_endpoints(query: str) -> list[str]:
    configured = os.environ.get("GIGAFORGE_SEARCH_URL", "").strip()
    if configured:
        return [configured.replace("{query}", urllib.parse.quote_plus(query))]
    encoded = urllib.parse.quote_plus(query)
    return [
        DEFAULT_SEARCH_URL.replace("{query}", encoded),
        DEFAULT_LITE_SEARCH_URL.replace("{query}", encoded),
    ]


class _ResultParser(HTMLParser):
    _VOID_TAGS = frozenset({
        "area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr",
    })

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[tuple[str, str, str]] = []
        self._capture: str | None = None
        self._capture_depth = 0
        self._capture_parts: list[str] = []
        self._capture_href = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._capture is not None:
            if tag not in self._VOID_TAGS:
                self._capture_depth += 1
            return

        attributes = dict(attrs)
        classes = set((attributes.get("class") or "").split())
        if tag == "a" and classes.intersection({"result__a", "result-link"}):
            self._start_capture("title", attributes.get("href") or "")
        elif tag in {"a", "td", "div"} and classes.intersection(
            {"result__snippet", "result-snippet"}
        ):
            self._start_capture("snippet")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        return

    def handle_endtag(self, tag: str) -> None:
        if self._capture is None or tag in self._VOID_TAGS:
            return
        self._capture_depth -= 1
        if self._capture_depth <= 0:
            value = " ".join(" ".join(self._capture_parts).split())
            if self._capture == "title":
                self.results.append((value, self._capture_href, ""))
            elif self.results and not self.results[-1][2]:
                title, url, _ = self.results[-1]
                self.results[-1] = (title, url, value)
            self._capture = None
            self._capture_depth = 0
            self._capture_parts = []
            self._capture_href = ""

    def handle_data(self, data: str) -> None:
        if self._capture is not None:
            self._capture_parts.append(data)

    def _start_capture(self, kind: str, href: str = "") -> None:
        self._capture = kind
        self._capture_depth = 1
        self._capture_parts = []
        self._capture_href = href


def _clean(fragment: str) -> str:
    return " ".join(_html.unescape(fragment or "").split())


def parse_results(page: str, *, limit: int = MAX_RESULTS) -> list[tuple[str, str, str]]:
    """``[(title, url, snippet), ...]`` from a results page.

    Tolerant by design: a page whose markup changed yields fewer results, not an
    exception.  The caller reports "no results" only when the page really had
    none.
    """
    parser = _ResultParser()
    parser.feed(page or "")
    out: list[tuple[str, str, str]] = []
    for raw_title, href, snippet in parser.results[:limit]:
        url = _html.unescape(href)
        # DuckDuckGo wraps links in a redirect; unwrap when possible.
        if "uddg=" in url:
            try:
                url = urllib.parse.unquote(
                    urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["uddg"][0]
                )
            except (KeyError, IndexError, ValueError):
                pass
        out.append((
            _clean(raw_title)[:160],
            url,
            _clean(snippet)[:MAX_SNIPPET],
        ))
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

        pages: list[str] = []
        errors: list[Exception] = []
        for endpoint in _search_endpoints(query):
            try:
                page = self._opener(endpoint, self.timeout)
            except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as exc:
                errors.append(exc)
                continue
            except Exception as exc:  # noqa: BLE001 - a tool must not kill the loop
                return f"ERROR: web_search failed ({str(exc)[:120]}); continue without it."

            pages.append(page)
            results = parse_results(page)
            if results:
                text = format_results(query, results)
                self.cache.put(query, text)
                return text

        if pages and any(
            marker in page.lower()
            for page in pages
            for marker in ("no results found", "no results for", "class=\"no-results")
        ):
            text = format_results(query, [])
        elif errors and not pages:
            exc = errors[-1]
            if isinstance(exc, urllib.error.HTTPError):
                return f"ERROR: web_search unavailable (HTTP {exc.code}); continue without it."
            return (
                "ERROR: web_search unavailable (network: "
                f"{str(exc)[:120]}); continue without it."
            )
        else:
            return (
                "ERROR: web_search unavailable (search provider returned no parseable "
                "results); continue without it."
            )
        self.cache.put(query, text)
        return text

    def describe(self) -> dict[str, Any]:
        return {"budget": self.budget.describe(), "cache": self.cache.describe()}
