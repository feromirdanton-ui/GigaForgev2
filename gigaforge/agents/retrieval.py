"""Retriever — L0 retrieval-augmented context (0 LLM calls).

The Aider-style repo retrieval, but DETERMINISTIC: no embedding model, no LLM.
Given a task string, the Retriever pulls the top-K most relevant code SNIPPETS
from the L0 index by:

1. **Symbol-name similarity** — exact / substring / token-overlap match
   between task tokens and symbol qnames.
2. **Caller proximity** — symbols that call (or are called by) the
   symbol-name candidates are boosted.
3. **Tiny BM25** over docstrings + signatures — classic sparse retrieval with
   no learned parameters (just term frequencies in the L0 index's
   ``string_literals`` table for docstrings + ``symbols.signature``).

Returns a compressed "context bag" — a list of :class:`Snippet` objects —
added to the Context Slice's ``related_symbols`` (capped to the slice's token
budget). This is the Aider/SWE-agent repo-map idea, but token-free.

Doctrine:
- Context is budget. Retrieval is L0 (0 LLM calls, 0 tokens spent on the
  retrieval itself — the snippets it returns add to the slice, capped by
  the slice's existing budget).
- Diff is contract. Retrieval enriches the Coder's input; it does not change
  the diff contract.
- Determinism. Same task + same L0 index → same retrieval output, byte-for-byte.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from gigaforge.firewall.tokens import estimate_tokens
from gigaforge.l0.index import L0Index, SymbolInfo

__all__ = ["Retriever", "Snippet", "ContextBag"]

# Top-K cap (matches Aider's repo-map default). After this many candidates
# we stop scoring and just take the top.
_DEFAULT_TOP_K = 12

# Hard per-snippet body token cap. Even if a symbol's body is 5000 tokens, the
# snippet we return is bounded — the Coder doesn't need the WHOLE body of a
# related symbol, just the gist.
_SNIPPET_BODY_TOKENS = 200

# Token budget for the whole context bag (a fraction of the Lite model's
# input cap; the slice already has its own cap, but we enforce a default
# here too as defence-in-depth).
_DEFAULT_BAG_TOKENS = 1500

# Stopwords for the BM25 / token-overlap scorer. Tiny list — we don't need a
# full NLP pipeline; the goal is to drop "the", "a", "fix", "make" from
# task strings so the scoring isn't dominated by boilerplate verbs.
_STOPWORDS = frozenset(
    {
        "the", "a", "an", "is", "are", "to", "in", "on", "of", "and", "or",
        "for", "with", "by", "this", "that", "it", "as", "be", "at", "from",
        "fix", "make", "update", "change", "modify", "refactor", "add", "remove",
        "function", "method", "class", "module", "file", "code", "implement",
    }
)

_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]+")


def _tokenize(text: str) -> list[str]:
    """Split on non-word chars; lowercase; drop stopwords + 1-char tokens."""
    if not text:
        return []
    return [
        t.lower()
        for t in _TOKEN_RE.findall(text)
        if len(t) > 1 and t.lower() not in _STOPWORDS
    ]


def _split_camel(qname: str) -> list[str]:
    """Split CamelCase + snake_case qnames into lowercase tokens."""
    # snake_case split.
    parts = qname.split("_")
    # CamelCase split.
    out: list[str] = []
    for p in parts:
        out.extend(re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?=[A-Z]|$)|\d+", p))
    return [t.lower() for t in out if t and len(t) > 1]


@dataclass(frozen=True)
class Snippet:
    """One retrieved code snippet — signature + (optionally) a body gist.

    Bodies are read via the L0's tree-sitter (deterministic, 0 LLM calls).
    Bounded to ``_SNIPPET_BODY_TOKENS`` so the bag stays small.
    """

    qname: str
    kind: str
    signature: str
    path: str
    body_gist: str = ""  # first N tokens of the body (or "")
    score: float = 0.0  # retrieval score (for debugging / ranking)
    caller_count: int = 0  # ranking signal from L0

    def to_prompt_dict(self) -> dict[str, Any]:
        return {
            "qname": self.qname,
            "kind": self.kind,
            "signature": self.signature,
            "path": self.path,
            "body_gist": self.body_gist,
            "caller_count": self.caller_count,
            "score": round(self.score, 3),
        }


@dataclass
class ContextBag:
    """A collection of :class:`Snippet` objects, bounded by a token budget."""

    snippets: list[Snippet] = field(default_factory=list)
    tokens_used: int = 0
    tokens_cap: int = _DEFAULT_BAG_TOKENS

    def to_prompt_list(self) -> list[dict[str, Any]]:
        return [s.to_prompt_dict() for s in self.snippets]


class Retriever:
    """L0 retrieval — deterministic, 0 LLM calls.

    Usage::

        retriever = Retriever(index)
        bag: ContextBag = retriever.retrieve(
            task="Fix validate_email to accept short TLDs",
            target_function="validate_email",  # optional anchor
            top_k=12,
            tokens_cap=1500,
        )
        # bag.to_prompt_list() -> list of dicts to append to the slice's
        # related_symbols (or use directly as a "retrieved context" field).
    """

    def __init__(self, index: L0Index) -> None:
        self._index = index

    def retrieve(
        self,
        task: str,
        *,
        target_function: str | None = None,
        top_k: int = _DEFAULT_TOP_K,
        tokens_cap: int = _DEFAULT_BAG_TOKENS,
        include_bodies: bool = False,
    ) -> ContextBag:
        """Pull the top-K relevant snippets from the L0 index.

        ``target_function`` (optional) anchors the retrieval — symbols that
        call (or are called by) the target get a proximity boost.

        ``include_bodies=True`` reads a short body gist per snippet (bounded
        by ``_SNIPPET_BODY_TOKENS``). This is L0 (deterministic) — 0 LLM calls.

        Returns a :class:`ContextBag` capped by ``tokens_cap``.
        """
        task_tokens = _tokenize(task)
        all_symbols = self._index.all_symbols()

        # Pre-compute caller graph for proximity boost.
        caller_sets: dict[str, set[str]] = {}
        for sym in all_symbols:
            callers = set(self._index.get_callers(sym.qname))
            caller_sets[sym.qname] = callers

        # Anchor: callers + callees of the target_function (if any).
        anchor_qnames: set[str] = set()
        if target_function:
            anchor_qnames.update(caller_sets.get(target_function, set()))
            # Callees: scan the target body for `name(` patterns.
            sym = self._index.get_symbol(target_function)
            if sym is not None and sym.path:
                try:
                    source = Path(sym.path).read_text(encoding="utf-8")
                    body_lines = source.split("\n")[sym.start_line - 1 : sym.end_line]
                    body_text = "\n".join(body_lines)
                    for m in re.finditer(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(", body_text):
                        callee = m.group(1)
                        if callee != target_function:
                            anchor_qnames.add(callee)
                except OSError:
                    pass

        # Score each symbol.
        scored: list[tuple[float, SymbolInfo]] = []
        for sym in all_symbols:
            score = self._score_symbol(sym, task_tokens, anchor_qnames, caller_sets)
            if score > 0.0:
                scored.append((score, sym))

        # Sort by score desc, then by qname for stable order.
        scored.sort(key=lambda x: (-x[0], x[1].qname))

        # Take top-K candidates.
        top = scored[:top_k]

        # Build snippets, capped by tokens_cap.
        snippets: list[Snippet] = []
        used = 0
        for score, sym in top:
            caller_count = len(caller_sets.get(sym.qname, set()))
            body_gist = ""
            if include_bodies:
                body_gist = self._read_body_gist(sym)
            snip = Snippet(
                qname=sym.qname,
                kind=sym.kind,
                signature=sym.signature,
                path=sym.path,
                body_gist=body_gist,
                score=score,
                caller_count=caller_count,
            )
            item_tokens = estimate_tokens(snip.signature) + estimate_tokens(snip.qname)
            if body_gist:
                item_tokens += estimate_tokens(body_gist)
            if used + item_tokens > tokens_cap:
                continue
            snippets.append(snip)
            used += item_tokens

        return ContextBag(snippets=snippets, tokens_used=used, tokens_cap=tokens_cap)

    # ----------------------------------------------------------- internals

    def _score_symbol(
        self,
        sym: SymbolInfo,
        task_tokens: list[str],
        anchor_qnames: set[str],
        caller_sets: dict[str, set[str]],
    ) -> float:
        """Score a symbol by token-overlap + caller proximity.

        Three components:
        1. Symbol-name token overlap with task tokens (substring + CamelCase split).
        2. Caller proximity — boost if the symbol is in the anchor set
           (calls / is called by the target_function).
        3. Tiny BM25 over the signature — rare task tokens in the signature
           boost the score.

        Returns a non-negative float. 0.0 means no signal.
        """
        if not task_tokens:
            return 0.0

        # Component 1: token overlap between symbol name + task.
        sym_tokens = set(_split_camel(sym.name) + _split_camel(sym.qname))
        # Also include the signature tokens (cheap signal).
        sig_tokens = set(_tokenize(sym.signature))
        sym_tokens |= sig_tokens
        task_set = set(task_tokens)
        overlap = sym_tokens & task_set
        # Jaccard-style score: overlap / union (bounded 0..1).
        union = sym_tokens | task_set
        token_score = (len(overlap) / len(union)) if union else 0.0

        # Component 2: caller proximity boost.
        proximity_boost = 0.0
        if sym.qname in anchor_qnames:
            proximity_boost = 0.5
        # Also: any of the symbol's callers are in the anchor set.
        callers = caller_sets.get(sym.qname, set())
        if callers & anchor_qnames:
            proximity_boost = max(proximity_boost, 0.25)

        # Component 3: tiny BM25 over the signature tokens.
        # For each task token, score = idf * (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * dl/avgdl)).
        # Simplified: idf = log(N / df); tf = count in signature; dl = signature length.
        sig_lower = sym.signature.lower()
        bm25 = 0.0
        for term in task_set:
            tf = sig_lower.count(term)
            if tf == 0:
                continue
            # df = 1 (the term appears in at least this signature); idf approx 1.0
            # since we don't pre-compute global df across the repo (would be O(N*M)).
            # We use a simpler idf: log(1 + N_unique_symbols / (1 + symbols_with_term)).
            # For determinism + cheapness, fall back to a constant idf=1.0 + tf saturation.
            bm25 += 1.0 * (tf * 1.5) / (tf + 0.5 + 0.25 * len(sig_lower) / 50.0)

        return token_score + proximity_boost + bm25 * 0.1

    def _read_body_gist(self, sym: SymbolInfo) -> str:
        """Read the first ``_SNIPPET_BODY_TOKENS`` tokens of ``sym``'s body.

        Uses the same tree-sitter mechanism as the Slicer — 0 LLM calls.
        """
        try:
            source = Path(sym.path).read_text(encoding="utf-8")
        except OSError:
            return ""
        lines = source.split("\n")
        body_lines = lines[sym.start_line : sym.end_line + 1]
        body = "\n".join(body_lines)
        # Cap by token estimate.
        char_limit = _SNIPPET_BODY_TOKENS * 4
        if len(body) <= char_limit:
            return body
        return body[:char_limit] + "\n    # [truncated]"


def combine_with_slice(
    slice_related: list,  # list[RelatedSymbol]
    bag: ContextBag,
    *,
    max_tokens: int,
    estimate: Any = estimate_tokens,
) -> list:
    """Merge retrieved snippets into the slice's existing related_symbols.

    The existing related_symbols (caller/callee signatures) take priority —
    retrieval adds NEW qnames not already in the slice, capped by
    ``max_tokens``.

    Returns a NEW list (does not mutate the input).
    """
    from gigaforge.firewall.slicer import RelatedSymbol  # local import to avoid cycle

    existing_qnames = {r.qname for r in slice_related}
    merged: list[RelatedSymbol] = list(slice_related)
    used = sum(estimate(r.signature) + estimate(r.qname) + 4 for r in slice_related)
    for snip in bag.snippets:
        if snip.qname in existing_qnames:
            continue
        item_tokens = estimate(snip.signature) + estimate(snip.qname) + 4
        if used + item_tokens > max_tokens:
            continue
        merged.append(
            RelatedSymbol(
                qname=snip.qname,
                kind=snip.kind,
                signature=snip.signature,
                caller_count=snip.caller_count,
            )
        )
        used += item_tokens
        existing_qnames.add(snip.qname)
    return merged
