"""Deterministic exploration primitives — used by the LocalizerAgent (L1).

Two public functions, both 0 LLM calls by construction (the module imports
ONLY :mod:`gigaforge.l0.index`, :mod:`gigaforge.l0.repo_map`, and stdlib;
no ``gigaforge.client.*``):

- :func:`grep_relevant_files` — runs ripgrep over the repo (via subprocess),
  scores files by distinct-query-term overlap, returns a ranked list. Output
  is the FILE-level rollup — never a raw file body. The localizer decides
  whether to feed ripgrep's compressed line-level output to an LLM (and if
  so, it goes through the Firewall first).
- :func:`symbol_suggestions` — scores symbols from the L0 index by
  query-term overlap (qname + signature + path), boosted by PageRank over
  the caller→callee graph. Returns ranked candidates.

Both functions are DETERMINISTIC (same inputs → same output, no LLM call).
They are the L0 backbone of the localizer's deterministic path: when the
top candidate is unambiguously above the runner-up, the localizer returns
WITHOUT any LLM call (L0 doctrine).

Word-boundary note: we treat ``_`` as a SEPARATOR (not a word character)
for matching purposes. Python identifiers commonly use ``_`` as a separator
(``validate_email``), so a query for ``validate`` SHOULD match
``validate_email``. We use the look-around pattern ``(?<![A-Za-z0-9])term``
``(?![A-Za-z0-9])`` instead of ``\\bterm\\b`` (which treats ``_`` as a word
char and would NOT match ``validate`` in ``validate_email``). ripgrep is
invoked with ``--pcre2`` to enable look-around (it falls back to the
default Rust regex engine if PCRE2 is unavailable).
"""
from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from gigaforge.l0.index import L0Index
from gigaforge.l0.repo_map import pagerank_scores

__all__ = [
    "RankedFile",
    "RankedSymbol",
    "grep_relevant_files",
    "run_ripgrep",
    "symbol_suggestions",
    "tokenize_query",
]

log = logging.getLogger(__name__)

# Terms shorter than this are ignored (too common, noise). Underscore-split
# means `validate_email` produces `validate` and `email` (both >= 7 chars),
# while `add` (3 chars) is kept — short names ARE the target in coding tasks.
_MIN_TERM_LEN = 3
# Maximum number of files returned by grep_relevant_files.
_DEFAULT_TOP_N_FILES = 10
# Maximum number of symbols returned by symbol_suggestions.
_DEFAULT_TOP_N_SYMBOLS = 20
# ripgrep subprocess timeout (seconds). Failures are non-fatal — we fall back
# to symbol-only scoring.
_RIPGREP_TIMEOUT_S = 15.0


@dataclass(frozen=True)
class RankedFile:
    """A file ranked by query-term overlap.

    `score` is the number of DISTINCT query terms found in the file (via
    ripgrep). `matches` is the total number of matching lines (a secondary
    signal — many matches in one file may indicate a hot spot).
    """

    path: str  # repo-relative path
    score: int  # distinct query terms found
    matches: int  # total matching lines (across all terms)
    terms_found: tuple[str, ...] = ()  # which query terms matched


@dataclass(frozen=True)
class RankedSymbol:
    """A symbol ranked by query-term overlap (signature + qname + path) +
    PageRank boost."""

    qname: str
    path: str
    kind: str
    signature: str
    score: float  # weighted total: term_overlap + pagerank + refs
    term_overlap: int  # raw query-term overlap count
    pagerank: float
    refs: int


@dataclass
class _SymbolScoreAccumulator:
    """Mutable accumulator for ranking symbols during a single call."""

    qname: str
    path: str
    kind: str
    signature: str
    pagerank: float
    refs: int
    term_overlap: int = 0
    matched_files: set[str] = field(default_factory=set)


# --- query tokenisation + matching patterns ---------------------------------


def tokenize_query(query: str) -> list[str]:
    """Split a natural-language query into ranked search terms.

    Rules:
    - Split on whitespace + non-alphanumeric (so `validate_email` →
      ``validate``, ``email``; ``x@y.z`` → ``x`` (dropped), ``y``, ``z``).
    - Lowercase all terms.
    - Drop terms shorter than ``_MIN_TERM_LEN`` chars.
    - Drop pure-stopword terms (a small built-in set + Python keywords).
    - Deduplicate (preserve first-seen order).
    """
    if not query:
        return []
    # Split on anything that isn't [a-zA-Z0-9_]. This keeps `validate_email`
    # as one token; we ALSO split snake_case into parts below so the matching
    # is more forgiving (a query for "validate" should match `validate_email`).
    raw_tokens = re.split(r"[^A-Za-z0-9_]+", query)
    tokens: list[str] = []
    seen: set[str] = set()
    for tok in raw_tokens:
        if not tok:
            continue
        # Snake-case split: `validate_email` → `validate`, `email`.
        for part in tok.split("_"):
            if not part or len(part) < _MIN_TERM_LEN:
                continue
            part_lower = part.lower()
            if part_lower in _STOPWORDS:
                continue
            if part_lower in seen:
                continue
            seen.add(part_lower)
            tokens.append(part_lower)
    return tokens


def _term_boundary_regex(term: str) -> re.Pattern[str]:
    """Build a regex that matches `term` with non-alphanumeric boundaries.

    Treats ``_`` as a SEPARATOR (so ``validate`` matches inside
    ``validate_email``). Uses look-around to express the boundary.
    """
    return re.compile(rf"(?<![A-Za-z0-9]){re.escape(term)}(?![A-Za-z0-9])")


def _term_boundary_pattern_str(term: str) -> str:
    """Build the ripgrep pattern string for a term (with non-alphanumeric
    boundaries, treating ``_`` as a separator)."""
    return rf"(?<![A-Za-z0-9]){re.escape(term)}(?![A-Za-z0-9])"


_STOPWORDS = frozenset({
    "the", "and", "for", "with", "that", "this", "from", "into", "fix",
    "make", "update", "change", "function", "method", "class", "def",
    "return", "should", "must", "need", "implement", "add", "remove",
    "where", "when", "what", "which", "their", "there", "have", "been",
    "they", "them", "are", "was", "were", "but", "not", "all", "any",
    "can", "may", "will", "would", "could", "shall", "task", "code",
    "file", "line", "test", "tests", "get", "set", "use", "using",
    "onto", "over", "under", "above", "below",
})


# --- grep_relevant_files -----------------------------------------------------


def _run_ripgrep(repo_root: Path, terms: list[str]) -> str:
    """Run ripgrep over the repo for any of the terms; return raw output.

    The output is the default ``path:line:content`` form (we DO NOT pass
    ``--no-filename`` because we need the file path per match to score files).
    We pass ``--pcre2`` to enable look-around so the non-alphanumeric boundary
    pattern works (treats ``_`` as a separator, matching ``validate`` inside
    ``validate_email``). If PCRE2 is unavailable, we fall back to plain
    alternation with ``\\b`` (which will miss underscore-joined matches but
    still works for word-boundary matches).

    Returns "" on any error (ripgrep not installed, timeout, non-zero exit on
    no-match — rg returns 1 when no matches, which is fine). Never raises.
    """
    if not terms:
        return ""
    # Build a regex alternation: term1|term2|... with non-alphanumeric
    # boundaries (treats `_` as a separator). We need PCRE2 for look-around.
    pattern = "|".join(_term_boundary_pattern_str(t) for t in terms)
    cmd_pcre2 = [
        "rg",
        "--line-number",
        "--no-heading",
        "--color=never",
        "--pcre2",
        "-i",
        pattern,
        str(repo_root),
        "--type", "py",
    ]
    cmd_fallback = [
        "rg",
        "--line-number",
        "--no-heading",
        "--color=never",
        "-i",
        "|".join(rf"\b{re.escape(t)}\b" for t in terms),
        str(repo_root),
        "--type", "py",
    ]
    for cmd in (cmd_pcre2, cmd_fallback):
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=_RIPGREP_TIMEOUT_S,
                check=False,
            )
        except FileNotFoundError:
            log.warning("exploration: rg not found; skipping ripgrep pass")
            return ""
        except subprocess.TimeoutExpired:
            log.warning("exploration: rg timed out after %ss", _RIPGREP_TIMEOUT_S)
            return ""
        # rg returns 0 on match, 1 on no-match, 2 on error.
        if proc.returncode == 2:
            # Maybe PCRE2 isn't available; try the fallback.
            stderr = (proc.stderr or "").lower()
            if "pcre2" in stderr or "look-around" in stderr or cmd is cmd_pcre2:
                continue
            log.warning(
                "exploration: rg returned %d: stderr=%s",
                proc.returncode,
                (proc.stderr or "").strip()[:200],
            )
            return ""
        if proc.returncode not in (0, 1):
            log.warning(
                "exploration: rg returned %d: stderr=%s",
                proc.returncode,
                (proc.stderr or "").strip()[:200],
            )
            return ""
        return proc.stdout or ""
    return ""


def grep_relevant_files(
    query: str,
    index: L0Index,
    repo_root: Path | None = None,
    *,
    top_n: int = _DEFAULT_TOP_N_FILES,
) -> list[RankedFile]:
    """Score repo files by distinct-query-term overlap (via ripgrep).

    The output is a FILE-LEVEL rollup (path → distinct terms found + total
    match count). Raw file bodies are NEVER returned — only the file paths
    and aggregate match counts. The localizer can later request the
    line-level ripgrep output (also Firewall-compressible via the grep
    compressor) if it needs to feed matches to the LLM.

    If ``repo_root`` is None or ripgrep is unavailable, the function
    falls back to a symbol-based file ranking: it scores each file by the
    number of distinct query terms appearing in any of its symbols'
    signatures (using the L0 index alone, no filesystem access).
    """
    terms = tokenize_query(query)
    if not terms:
        return []

    # Path 1: ripgrep over the repo (preferred — finds string literals + comments
    # + actual call sites, not just symbol definitions).
    if repo_root is not None:
        raw = _run_ripgrep(Path(repo_root), terms)
        if raw:
            return _parse_ripgrep_output(raw, terms, top_n)

    # Path 2: fall back to L0 symbol signatures (no filesystem access).
    return _score_files_via_symbols(index, terms, top_n)


def run_ripgrep(
    query: str,
    repo_root: Path,
    *,
    limit_chars: int = 0,
) -> str:
    """Run ripgrep over the repo for the query terms; return the raw output.

    Public wrapper around the internal ``_run_ripgrep`` so the localizer can
    fetch line-level matches to feed the LLM (after Firewall compression).
    Returns the raw ``path:line:content`` text. If ``limit_chars`` > 0,
    truncates the output to that many chars (defensive — keeps the payload
    bounded before the Firewall runs).

    Never raises: returns "" on ripgrep-missing, timeout, or no matches.
    """
    terms = tokenize_query(query)
    if not terms:
        return ""
    raw = _run_ripgrep(Path(repo_root), terms)
    if not raw:
        return ""
    if limit_chars > 0 and len(raw) > limit_chars:
        # Truncate at a line boundary if possible (don't split mid-line).
        cut = raw.rfind("\n", 0, limit_chars)
        if cut > 0:
            raw = raw[:cut]
        else:
            raw = raw[:limit_chars]
    return raw


def _parse_ripgrep_output(
    raw: str,
    terms: list[str],
    top_n: int,
) -> list[RankedFile]:
    """Parse ripgrep's ``path:line:content`` output into a ranked file list."""
    file_terms: dict[str, set[str]] = {}
    file_match_count: dict[str, int] = {}
    # Pre-compile the per-term boundary regexes (Python's re, NOT ripgrep's —
    # we re-check on the matched content because ripgrep's PCRE2 may have
    # fallen back to \b which misses underscore-joined matches).
    term_res = {t: _term_boundary_regex(t) for t in terms}

    for line in raw.splitlines():
        if not line.strip():
            continue
        m = re.match(r"^(?P<path>.+?):(?P<line>\d+):(?P<content>.*)$", line)
        if not m:
            continue
        path = m.group("path")
        content = m.group("content").lower()
        # Find which terms appear in this match line (with non-alphanumeric
        # boundaries so `validate` matches inside `validate_email`).
        for term, term_re in term_res.items():
            if term_re.search(content):
                file_terms.setdefault(path, set()).add(term)
        file_match_count[path] = file_match_count.get(path, 0) + 1

    ranked: list[RankedFile] = []
    for path, term_set in file_terms.items():
        ranked.append(
            RankedFile(
                path=path,
                score=len(term_set),
                matches=file_match_count.get(path, 0),
                terms_found=tuple(sorted(term_set)),
            )
        )
    # Sort: distinct terms desc, then total matches desc, then path (stable).
    ranked.sort(key=lambda r: (-r.score, -r.matches, r.path))
    return ranked[:top_n]


def _score_files_via_symbols(
    index: L0Index,
    terms: list[str],
    top_n: int,
) -> list[RankedFile]:
    """Fallback: score files by query-term overlap in their symbols'
    signatures + qnames (L0 index only, no filesystem access)."""
    file_terms: dict[str, set[str]] = {}
    file_match_count: dict[str, int] = {}
    term_res = {t: _term_boundary_regex(t) for t in terms}
    for sym in index.all_symbols():
        haystack = f"{sym.qname} {sym.signature} {sym.path}".lower()
        for term, term_re in term_res.items():
            if term_re.search(haystack):
                file_terms.setdefault(sym.path, set()).add(term)
                file_match_count[sym.path] = file_match_count.get(sym.path, 0) + 1
    ranked: list[RankedFile] = []
    for path, term_set in file_terms.items():
        ranked.append(
            RankedFile(
                path=path,
                score=len(term_set),
                matches=file_match_count.get(path, 0),
                terms_found=tuple(sorted(term_set)),
            )
        )
    ranked.sort(key=lambda r: (-r.score, -r.matches, r.path))
    return ranked[:top_n]


# --- symbol_suggestions ------------------------------------------------------


def symbol_suggestions(
    index: L0Index,
    query: str,
    *,
    repo_root: Path | None = None,
    top_n: int = _DEFAULT_TOP_N_SYMBOLS,
    pagerank_weight: float = 100.0,
    refs_weight: float = 0.5,
    term_weight: float = 10.0,
) -> list[RankedSymbol]:
    """Rank symbols from the L0 index by query-term overlap + PageRank boost.

    The ranking combines three signals:
    - ``term_overlap`` (× ``term_weight``): how many distinct query terms
      appear in the symbol's qname + signature + path. Matching uses
      non-alphanumeric boundaries so ``validate`` matches inside
      ``validate_email`` (Python's ``_`` is treated as a separator).
    - ``pagerank`` (× ``pagerank_weight``): PageRank over the caller→callee
      graph — symbols called by many callers (or by important callers) get
      a higher boost. We multiply by a large weight because raw PageRank
      scores sum to ~1.0 across all nodes (so individual scores are tiny).
    - ``refs`` (× ``refs_weight``): the inbound caller count — a simpler
      graph centrality signal used as a tiebreaker.

    Returns the top-N symbols sorted by total score desc. Symbols with zero
    term overlap are STILL returned (ranked lower by pagerank/refs) so the
    localizer has fallback candidates when no symbol name matches the query.
    """
    terms = tokenize_query(query)
    term_res = {t: _term_boundary_regex(t) for t in terms}

    # Compute PageRank once per call (deterministic).
    scores = pagerank_scores(index)

    # Ref counts (legacy signal, used as tiebreaker).
    cur = index.conn.cursor()
    cur.execute("SELECT callee, COUNT(*) AS c FROM calls GROUP BY callee")
    callee_counts = {r["callee"]: r["c"] for r in cur.fetchall()}

    accumulators: list[_SymbolScoreAccumulator] = []
    for sym in index.all_symbols():
        short = sym.qname.rsplit(".", 1)[-1]
        refs = callee_counts.get(sym.qname, 0) + callee_counts.get(short, 0)
        if sym.qname == short:
            refs = callee_counts.get(sym.qname, 0)
        pr = scores.get(sym.qname, 0.0)
        if pr == 0.0:
            pr = scores.get(short, 0.0)
        acc = _SymbolScoreAccumulator(
            qname=sym.qname,
            path=sym.path,
            kind=sym.kind,
            signature=sym.signature,
            pagerank=pr,
            refs=refs,
        )
        haystack = f"{sym.qname} {sym.signature} {sym.path}".lower()
        if terms:
            for _term, term_re in term_res.items():
                if term_re.search(haystack):
                    acc.term_overlap += 1
        accumulators.append(acc)

    # Optionally use ripgrep results to boost symbols in files that contain
    # the query terms (a symbol is "in" a relevant file if its path matches
    # a ripgrep-matched file).
    if repo_root is not None and terms:
        # We use _run_ripgrep here too — but only to get the matched files;
        # we don't need the line-level output for symbol scoring.
        raw = _run_ripgrep(Path(repo_root), terms)
        if raw:
            matched_files: set[str] = set()
            for line in raw.splitlines():
                m = re.match(r"^(?P<path>.+?):(?P<line>\d+):(?P<content>.*)$", line)
                if m:
                    matched_files.add(m.group("path"))
            if matched_files:
                for acc in accumulators:
                    if acc.path in matched_files:
                        acc.matched_files = matched_files  # marker

    # Compute the final score per accumulator.
    ranked: list[RankedSymbol] = []
    for acc in accumulators:
        # Bonus for being in a matched file: +1 term_overlap-equivalent.
        file_bonus = 1.0 if acc.matched_files else 0.0
        total = (
            acc.term_overlap * term_weight
            + acc.pagerank * pagerank_weight
            + acc.refs * refs_weight
            + file_bonus * term_weight
        )
        ranked.append(
            RankedSymbol(
                qname=acc.qname,
                path=acc.path,
                kind=acc.kind,
                signature=acc.signature,
                score=round(total, 6),
                term_overlap=acc.term_overlap,
                pagerank=round(acc.pagerank, 6),
                refs=acc.refs,
            )
        )

    # Sort: total score desc, then term_overlap desc, then pagerank desc,
    # then qname (stable).
    ranked.sort(
        key=lambda r: (-r.score, -r.term_overlap, -r.pagerank, r.qname)
    )
    return ranked[:top_n]
