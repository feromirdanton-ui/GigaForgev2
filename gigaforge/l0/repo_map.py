"""Repo map: a token-budgeted, ranked outline of the repository.

The map is what an agent reads *instead of* the whole tree.  It answers
"where does anything live, and what matters most?" in a few thousand tokens.

Ranking combines six signals, each normalised to ``[0, 1]`` and combined with
the weights from :data:`L0Config.rank_weights`:

``pagerank``
    PageRank over the symbol call graph — a symbol called by many important
    symbols ranks high (this is the signal that makes the map useful).
``in_degree``
    Raw number of callers, log-scaled; rewards hubs even in a shallow graph.
``public``
    Exported / non-underscore symbols outrank private helpers.
``size``
    Larger bodies carry more information, but with diminishing returns.
``recency``
    Symbols in recently modified files get a small boost.
``kind``
    Classes / interfaces / traits outrank plain functions, which outrank
    variables and constants.

The output is rendered as an indented outline grouped by file, truncated to
the token budget.  Rendering is deterministic, so the same revision always
produces the same map (and the cache key is stable).
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional, Sequence

from .config import L0Config
from .store import L0Store

#: kind → base importance (0..1)
KIND_WEIGHT: Mapping[str, float] = {
    "class": 1.0,
    "interface": 1.0,
    "trait": 1.0,
    "struct": 0.9,
    "enum": 0.85,
    "impl": 0.8,
    "record": 0.8,
    "type": 0.7,
    "function": 0.7,
    "method": 0.6,
    "constructor": 0.6,
    "module": 0.5,
    "const": 0.4,
    "static": 0.4,
    "variable": 0.3,
    "field": 0.3,
    "macro": 0.5,
    "decorated": 0.6,
}


@dataclass
class RankedSymbol:
    symbol_id: str
    name: str
    qualified_name: str
    kind: str
    path: str
    signature: str
    docstring: str
    start_line: int
    end_line: int
    is_public: bool
    score: float
    signals: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol_id": self.symbol_id,
            "name": self.name,
            "qualified_name": self.qualified_name,
            "kind": self.kind,
            "path": self.path,
            "signature": self.signature,
            "docstring": self.docstring[:200],
            "start_line": self.start_line,
            "end_line": self.end_line,
            "is_public": self.is_public,
            "score": round(self.score, 6),
            "signals": {k: round(v, 6) for k, v in self.signals.items()},
        }


def _ranked_from_payload(payload: Any) -> list[RankedSymbol]:
    """Rebuild ranked symbols from a cached payload; tolerate older cache rows."""
    if not isinstance(payload, list):
        return []
    out: list[RankedSymbol] = []
    for row in payload:
        if not isinstance(row, dict):
            continue
        try:
            out.append(
                RankedSymbol(
                    symbol_id=str(row.get("symbol_id", "")),
                    name=str(row.get("name", "")),
                    qualified_name=str(row.get("qualified_name", "")),
                    kind=str(row.get("kind", "")),
                    path=str(row.get("path", "")),
                    signature=str(row.get("signature", "")),
                    docstring=str(row.get("docstring", "")),
                    start_line=int(row.get("start_line", 0) or 0),
                    end_line=int(row.get("end_line", 0) or 0),
                    is_public=bool(row.get("is_public", False)),
                    score=float(row.get("score", 0.0) or 0.0),
                    signals={str(k): float(v) for k, v in (row.get("signals") or {}).items()},
                )
            )
        except (TypeError, ValueError):  # pragma: no cover - corrupt cache row
            continue
    return out


@dataclass
class RepoMap:
    text: str
    tokens: int
    symbols: int
    files: int
    budget: int
    truncated: bool
    rev: str = ""
    ranked: list[RankedSymbol] = field(default_factory=list)
    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "tokens": self.tokens,
            "symbols": self.symbols,
            "files": self.files,
            "budget": self.budget,
            "truncated": self.truncated,
            "rev": self.rev,
        }


# ── PageRank ─────────────────────────────────────────────────────────────


def pagerank(
    nodes: Sequence[str],
    edges: Sequence[tuple[str, str, float]],
    *,
    damping: float = 0.85,
    iters: int = 30,
    tol: float = 1e-9,
) -> dict[str, float]:
    """Weighted PageRank with dangling-node redistribution.

    ``edges`` are ``(src, dst, weight)`` — a call from ``src`` to ``dst``
    passes rank from the caller to the callee, so heavily-called symbols
    accumulate rank.
    """
    n = len(nodes)
    if n == 0:
        return {}
    if n == 1:
        return {nodes[0]: 1.0}

    index = {node: i for i, node in enumerate(nodes)}
    out_weight = [0.0] * n
    incoming: list[list[tuple[int, float]]] = [[] for _ in range(n)]

    for src, dst, weight in edges:
        i = index.get(src)
        j = index.get(dst)
        if i is None or j is None or i == j:
            continue
        w = max(float(weight), 0.0)
        if w == 0.0:
            continue
        out_weight[i] += w
        incoming[j].append((i, w))

    rank = [1.0 / n] * n
    base = (1.0 - damping) / n

    for _ in range(iters):
        dangling = sum(rank[i] for i in range(n) if out_weight[i] == 0.0)
        dangling_share = damping * dangling / n
        new_rank = [base + dangling_share] * n

        for j in range(n):
            acc = 0.0
            for i, w in incoming[j]:
                if out_weight[i] > 0.0:
                    acc += rank[i] * (w / out_weight[i])
            new_rank[j] += damping * acc

        delta = sum(abs(new_rank[i] - rank[i]) for i in range(n))
        rank = new_rank
        if delta < tol:
            break

    total = sum(rank) or 1.0
    return {nodes[i]: rank[i] / total for i in range(n)}


# ── ranking ──────────────────────────────────────────────────────────────


def _normalise(values: Mapping[str, float]) -> dict[str, float]:
    if not values:
        return {}
    lo = min(values.values())
    hi = max(values.values())
    if hi - lo < 1e-12:
        return {k: 1.0 for k in values}
    return {k: (v - lo) / (hi - lo) for k, v in values.items()}


class RepoMapBuilder:
    """Builds a ranked, token-budgeted repo map from the L0 index."""

    def __init__(self, store: L0Store, *, config: Optional[L0Config] = None) -> None:
        self.store = store
        self.config = config or store.config

    # ── public API ───────────────────────────────────────────────────────
    def build(
        self,
        *,
        budget_tokens: Optional[int] = None,
        focus: Optional[Sequence[str]] = None,
        path_prefix: Optional[str] = None,
        use_cache: bool = True,
        rev: str = "",
    ) -> RepoMap:
        budget = int(budget_tokens or self.config.repo_map_budget_tokens)
        focus = tuple(focus or ())

        cache_key = self._cache_key(budget, focus, path_prefix, rev)
        if use_cache:
            cached = self.store.get_repo_map(cache_key)
            if cached is not None:
                payload = json.loads(cached["payload"])
                return RepoMap(
                    text=payload["text"],
                    tokens=int(cached["tokens"]),
                    symbols=int(cached["symbols"]),
                    files=int(payload.get("files", 0)),
                    budget=budget,
                    truncated=bool(payload.get("truncated", False)),
                    rev=rev,
                    # The ranked list is part of the map's value, not just its
                    # rendering: consumers pick files/symbols out of it (the
                    # pipeline's vague-goal slice seeding does exactly that).
                    # Dropping it on a cache hit made the second caller of an
                    # unchanged repository see an empty ranking — a cache that
                    # silently changes the answer is worse than no cache.
                    ranked=_ranked_from_payload(payload.get("ranked")),
                )

        ranked = self.rank(focus=focus, path_prefix=path_prefix)
        text, used, truncated = self._render(ranked, budget)
        files = len({r.path for r in ranked[: max(1, len(ranked))]})

        result = RepoMap(
            text=text,
            tokens=used,
            symbols=len(ranked),
            files=files,
            budget=budget,
            truncated=truncated,
            rev=rev,
            ranked=ranked,
        )
        if use_cache:
            self.store.put_repo_map(
                cache_key,
                json.dumps({
                    "text": text,
                    "files": files,
                    "truncated": truncated,
                    "ranked": [r.to_dict() for r in ranked],
                }),
                tokens=used,
                symbols=len(ranked),
                rev=rev,
            )
        return result

    def rank(
        self,
        *,
        focus: Optional[Sequence[str]] = None,
        path_prefix: Optional[str] = None,
    ) -> list[RankedSymbol]:
        """Score every indexed symbol and return them best-first."""
        rows = self.store.all_symbols(limit=self.config.repo_map_max_symbols * 4)
        if path_prefix:
            rows = [r for r in rows if r["path"].startswith(path_prefix)]
        if not rows:
            return []

        nodes, edges = self.store.call_graph()
        pr = pagerank(
            nodes,
            edges,
            damping=self.config.pagerank_damping,
            iters=self.config.pagerank_iters,
            tol=self.config.pagerank_tol,
        )
        in_deg = self.store.in_degrees()

        # ── raw signals ──────────────────────────────────────────────────
        pr_raw = {r["symbol_id"]: pr.get(r["symbol_id"], 0.0) for r in rows}
        indeg_raw = {r["symbol_id"]: math.log1p(in_deg.get(r["symbol_id"], 0)) for r in rows}
        size_raw = {
            r["symbol_id"]: math.log1p(max(0, (r["end_line"] or 0) - (r["start_line"] or 0)))
            for r in rows
        }
        recency_raw = self._recency_scores(rows)

        pr_n = _normalise(pr_raw)
        indeg_n = _normalise(indeg_raw)
        size_n = _normalise(size_raw)
        recency_n = _normalise(recency_raw)

        weights = dict(self.config.rank_weights)
        focus_set = {f.lower() for f in (focus or ())}

        ranked: list[RankedSymbol] = []
        for row in rows:
            sid = row["symbol_id"]
            signals = {
                "pagerank": pr_n.get(sid, 0.0),
                "in_degree": indeg_n.get(sid, 0.0),
                "public": 1.0 if row["is_public"] else 0.0,
                "size": size_n.get(sid, 0.0),
                "recency": recency_n.get(sid, 0.0),
                "kind": KIND_WEIGHT.get(row["kind"], 0.4),
            }
            score = sum(weights.get(k, 0.0) * v for k, v in signals.items())

            # Private helpers are frequently high-degree hubs (``_text``,
            # ``_now``) but are rarely what an agent needs to see first, so
            # damp them rather than letting PageRank dominate the map.
            if not row["is_public"]:
                score *= 0.45

            # focus boost: exact / prefix match on name or path
            if focus_set:
                name = (row["name"] or "").lower()
                qname = (row["qualified_name"] or "").lower()
                path = (row["path"] or "").lower()
                if any(f == name or f == qname for f in focus_set):
                    score += 2.0
                elif any(f in name or f in qname or f in path for f in focus_set):
                    score += 0.75

            ranked.append(
                RankedSymbol(
                    symbol_id=sid,
                    name=row["name"],
                    qualified_name=row["qualified_name"],
                    kind=row["kind"],
                    path=row["path"],
                    signature=row["signature"],
                    docstring=row["docstring"] or "",
                    start_line=row["start_line"],
                    end_line=row["end_line"],
                    is_public=bool(row["is_public"]),
                    score=score,
                    signals=signals,
                )
            )

        ranked.sort(key=lambda r: (-r.score, r.path, r.start_line))
        return ranked

    # ── rendering ────────────────────────────────────────────────────────
    def _render(self, ranked: Sequence[RankedSymbol], budget: int) -> tuple[str, int, bool]:
        """Render the outline, stopping when the token budget is exhausted."""
        cpt = self.config.chars_per_token or 4.0
        lines: list[str] = []
        used_chars = 0
        truncated = False

        by_file: dict[str, list[RankedSymbol]] = {}
        for sym in ranked:
            by_file.setdefault(sym.path, []).append(sym)

        # order files by their best symbol so the most important file is first
        file_order = sorted(
            by_file,
            key=lambda p: -max(s.score for s in by_file[p]),
        )

        for path in file_order:
            syms = sorted(by_file[path], key=lambda s: s.start_line)
            header = f"{path}:"
            if used_chars + len(header) + 1 > budget * cpt:
                truncated = True
                break
            lines.append(header)
            used_chars += len(header) + 1

            for sym in syms:
                indent = "  " if sym.kind in {"method", "field"} else "  "
                sig = sym.signature or f"{sym.name}()"
                line = f"{indent}{sym.kind} {sig}"
                if sym.docstring:
                    first = sym.docstring.splitlines()[0].strip()
                    if first:
                        line += f"  # {first[:80]}"
                if used_chars + len(line) + 1 > budget * cpt:
                    truncated = True
                    break
                lines.append(line)
                used_chars += len(line) + 1
            if truncated:
                break

        text = "\n".join(lines)
        tokens = int(math.ceil(len(text) / cpt))
        return text, tokens, truncated

    # ── helpers ──────────────────────────────────────────────────────────
    def _recency_scores(self, rows: Sequence[Any]) -> dict[str, float]:
        """Map symbol_id → recency in [0,1] from the file's indexed_at."""
        file_times: dict[str, float] = {}
        for row in self.store.all_files():
            ts = row["indexed_at"] or ""
            file_times[row["path"]] = _parse_ts(ts)

        if not file_times:
            return {r["symbol_id"]: 0.0 for r in rows}

        lo = min(file_times.values())
        hi = max(file_times.values())
        span = hi - lo
        out: dict[str, float] = {}
        for row in rows:
            t = file_times.get(row["path"], lo)
            out[row["symbol_id"]] = 0.0 if span <= 0 else (t - lo) / span
        return out

    def _cache_key(
        self,
        budget: int,
        focus: Sequence[str],
        path_prefix: Optional[str],
        rev: str,
    ) -> str:
        weights = ",".join(f"{k}={v}" for k, v in sorted(self.config.rank_weights.items()))
        raw = f"{self.config.root}|{rev}|{budget}|{weights}|{','.join(focus)}|{path_prefix or ''}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:24]


def _parse_ts(ts: str) -> float:
    if not ts:
        return 0.0
    try:
        cleaned = ts.replace("Z", "+00:00")
        return datetime.fromisoformat(cleaned).timestamp()
    except ValueError:
        return 0.0


def build_repo_map(
    store: L0Store,
    *,
    config: Optional[L0Config] = None,
    budget_tokens: Optional[int] = None,
    focus: Optional[Sequence[str]] = None,
    path_prefix: Optional[str] = None,
    use_cache: bool = True,
    rev: str = "",
) -> RepoMap:
    """Convenience wrapper around :class:`RepoMapBuilder`."""
    builder = RepoMapBuilder(store, config=config)
    return builder.build(
        budget_tokens=budget_tokens,
        focus=focus,
        path_prefix=path_prefix,
        use_cache=use_cache,
        rev=rev,
    )


def pagerank_scores(index: Any) -> dict[str, float]:
    """Compute PageRank scores over an L0Index (or store) for backward compatibility."""
    if hasattr(index, "conn"):
        cur = index.conn.cursor()
        cur.execute("SELECT caller, callee FROM calls")
        rows = cur.fetchall()
        nodes: set[str] = set()
        edges: list[tuple[str, str, float]] = []
        for r in rows:
            c = r["caller"] if hasattr(r, "keys") else r[0]
            ce = r["callee"] if hasattr(r, "keys") else r[1]
            if c and ce:
                nodes.add(str(c))
                nodes.add(str(ce))
                edges.append((str(c), str(ce), 1.0))
        if not nodes:
            return {}
        return pagerank(sorted(nodes), edges)
    return {}


build = build_repo_map

__all__ = [
    "RepoMap",
    "RankedSymbol",
    "RepoMapBuilder",
    "build_repo_map",
    "build",
    "pagerank",
    "pagerank_scores",
    "KIND_WEIGHT",
]
