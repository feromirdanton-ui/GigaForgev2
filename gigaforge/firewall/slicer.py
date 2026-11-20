"""Context Slicer — build the smallest useful context from the L0 index.

Given a task description and an :class:`~gigaforge.l0.store.L0Store`, the slicer
assembles a *slice*: the handful of symbols, call edges, blast-radius callers and
candidate tests that a model would actually need to do the work.  It never calls
a model — every decision is a ranking over index facts:

1. **extract** — lexical keywords, paths and symbols from the task text
   (:mod:`gigaforge.firewall.slicer_keywords`);
2. **seed** — search the index for each term: FTS hits, exact symbol matches and
   explicitly named files become seeds;
3. **expand** — walk the call graph outwards (callers/callees) one hop at a time,
   decaying the score each hop;
4. **rank** — combine keyword weight, inbound-degree centrality, kind and
   visibility into one relevance score;
5. **budget** — render sections in priority order until the token budget is
   spent, recording exactly what was dropped.

The result is deterministic for a given index revision, which is what makes it
testable and cheap to cache.
"""

from __future__ import annotations

import fnmatch
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Sequence

import tree_sitter_python as tspython
from pydantic import BaseModel, Field
from tree_sitter import Language, Parser

from gigaforge.firewall.compress.base import estimate_tokens as estimate_tokens_from_chars
from gigaforge.firewall.tokens import estimate_tokens
from gigaforge.l0.index import L0Index, SymbolInfo
from gigaforge.l0.repo_map import RepoMapBuilder
from gigaforge.l0.store import L0Store

from .config import FirewallConfig
from .errors import EmptySliceError, SliceError
from .slicer_keywords import Extraction, Extractor

# tree-sitter setup
PY_LANG = Language(tspython.language())
_PARSER = Parser(PY_LANG)


def _workspace_relative_path(raw: str, root: Path) -> str:
    """Resolve a file seed only when it stays inside the indexed project."""
    value = str(raw or "").strip()
    if not value:
        return ""
    candidate = Path(value)
    if re.match(r"^[A-Za-z]:[\\/]", value) and not candidate.is_absolute():
        return ""
    try:
        base = root.resolve()
        resolved = (candidate if candidate.is_absolute() else base / candidate).resolve()
        return resolved.relative_to(base).as_posix()
    except (OSError, ValueError):
        return ""


def _alias_ref_re(alias: str) -> re.Pattern[str]:
    return re.compile(rf"\b{re.escape(alias)}\b")


class TargetFunction(BaseModel):
    """The function the Coder is asked to edit."""

    name: str
    signature: str
    body: str
    dependencies: list[str] = Field(default_factory=list)
    callers: list[str] = Field(default_factory=list)


class ImportItem(BaseModel):
    """An import line that the target function actually references in its body."""

    module: str
    alias: str


class RelatedSymbol(BaseModel):
    """A caller or callee of the target. Signatures only — bodies are absent."""

    qname: str
    kind: str
    signature: str
    caller_count: int = 0


#: Relevance of each symbol kind when everything else is equal.
KIND_BONUS: dict[str, float] = {
    "class": 1.6,
    "interface": 1.5,
    "function": 1.4,
    "method": 1.3,
    "type": 1.2,
    "variable": 1.0,
    "const": 1.0,
}

#: How the score decays per expansion hop.
HOP_DECAY = 0.55


@dataclass
class SliceSymbol:
    """One symbol included in the slice, with the reason it was chosen."""

    symbol_id: str
    name: str
    qualified_name: str
    kind: str
    path: str
    signature: str = ""
    docstring: str = ""
    start_line: int = 0
    end_line: int = 0
    is_public: bool = True
    score: float = 0.0
    hop: int = 0
    reasons: list[str] = field(default_factory=list)
    excerpt: str = ""

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "symbol_id": self.symbol_id,
            "name": self.name,
            "qualified_name": self.qualified_name,
            "kind": self.kind,
            "path": self.path,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "signature": self.signature,
            "is_public": self.is_public,
            "score": round(self.score, 4),
            "hop": self.hop,
            "reasons": list(self.reasons),
        }
        if self.docstring:
            out["docstring"] = self.docstring[:200]
        if self.excerpt:
            out["excerpt"] = self.excerpt
        return out


class ContextSlice(BaseModel):
    """The assembled slice plus its budget accounting and prompt formatting."""

    # Target function slice fields (old-optimised / Coder agent)
    target_function: Optional[TargetFunction] = None
    relevant_imports: list[ImportItem] = Field(default_factory=list)
    related_symbols: list[RelatedSymbol] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    test_signals: dict[str, Any] = Field(default_factory=dict)
    file_path: Optional[str] = None
    target_file: Optional[str] = None

    # Task slice fields (main)
    task: str = ""
    keywords: list[str] = Field(default_factory=list)
    paths: list[str] = Field(default_factory=list)
    symbols: list[Any] = Field(default_factory=list)
    call_graph: dict[str, Any] = Field(default_factory=dict)
    blast_radius: list[dict[str, Any]] = Field(default_factory=list)
    tests: list[str] = Field(default_factory=list)
    files: list[str] = Field(default_factory=list)
    repo_map: str = ""
    budget: int = 0
    tokens: int = 0
    truncated: bool = False
    dropped: dict[str, int] = Field(default_factory=dict)

    model_config = {"arbitrary_types_allowed": True, "extra": "allow"}

    def to_prompt_dict(self) -> dict[str, Any]:
        """The JSON the Coder receives. Bodies of non-target symbols are absent."""
        data = self.model_dump(mode="json")
        data.pop("file_path", None)
        return data

    def token_budget_used(self) -> int:
        """Cheap token estimate of the serialized slice."""
        return estimate_tokens(json.dumps(self.to_prompt_dict(), ensure_ascii=False))

    # ── rendering ────────────────────────────────────────────────────────
    def render(self) -> str:
        """Human/model-readable rendering, most important sections first."""
        parts: list[str] = []
        if self.task:
            parts.append(f"# Task\n{self.task.strip()}")
        if self.keywords:
            parts.append("# Keywords\n" + ", ".join(self.keywords))
        if self.repo_map:
            parts.append(f"# Repository map\n{self.repo_map}")
        if self.symbols:
            blocks = []
            for sym in self.symbols:
                s_path = getattr(sym, "path", sym.get("path", "") if isinstance(sym, dict) else "")
                s_start = getattr(sym, "start_line", sym.get("start_line", 0) if isinstance(sym, dict) else 0)
                s_end = getattr(sym, "end_line", sym.get("end_line", 0) if isinstance(sym, dict) else 0)
                s_kind = getattr(sym, "kind", sym.get("kind", "") if isinstance(sym, dict) else "")
                s_qname = getattr(sym, "qualified_name", sym.get("qualified_name", "") if isinstance(sym, dict) else "")
                head = f"{s_path}:{s_start}-{s_end} {s_kind} {s_qname}"
                body = [head]
                sig = getattr(sym, "signature", sym.get("signature", "") if isinstance(sym, dict) else "")
                doc = getattr(sym, "docstring", sym.get("docstring", "") if isinstance(sym, dict) else "")
                exc = getattr(sym, "excerpt", sym.get("excerpt", "") if isinstance(sym, dict) else "")
                if sig:
                    body.append(f"  {sig}")
                elif doc:
                    body.append(f"  # {doc.splitlines()[0][:120]}")
                if exc:
                    body.append("\n".join(f"      {ln}" for ln in exc.splitlines()))
                blocks.append("\n".join(body))
            parts.append("# Relevant symbols\n" + "\n\n".join(blocks))
        if self.call_graph:
            lines = []
            for name, edges in self.call_graph.items():
                callers = ", ".join(c["name"] for c in edges.get("callers", [])[:6]) if isinstance(edges, dict) and "callers" in edges else "-"
                callees = ", ".join(c["name"] for c in edges.get("callees", [])[:6]) if isinstance(edges, dict) and "callees" in edges else "-"
                lines.append(f"{name}\n  <- called by: {callers}\n  -> calls: {callees}")
            parts.append("# Call graph\n" + "\n".join(lines))
        if self.blast_radius:
            lines = [
                f"{b.get('path', '')}:{b.get('line', 0)} {b.get('name', '')}" for b in self.blast_radius
            ]
            parts.append("# Blast radius (callers of touched symbols)\n" + "\n".join(lines))
        if self.tests:
            parts.append("# Candidate tests\n" + "\n".join(self.tests))
        if self.dropped:
            dropped = ", ".join(f"{k}:{v}" for k, v in sorted(self.dropped.items()))
            parts.append(f"# Omitted (budget)\n{dropped} [{self.budget} tokens]")
        return "\n\n".join(parts)

    def to_dict(self) -> dict[str, Any]:
        data = self.model_dump(mode="json")
        if self.symbols:
            data["symbols"] = [s.to_dict() if hasattr(s, "to_dict") else s for s in self.symbols]
        return data

    @property
    def text(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"))



class ContextSlicer:
    """Builds a :class:`ContextSlice` from the L0 index. Zero LLM tokens."""

    def __init__(
        self,
        store: Optional[L0Store] = None,
        *,
        config: Optional[FirewallConfig] = None,
        extractor: Optional[Extractor] = None,
        max_related_tokens: int = 1500,
    ) -> None:
        self.store = store
        self.config = config or FirewallConfig()
        self.extractor = extractor or Extractor(self.config)
        self.root = Path(self.config.repo_root).resolve()
        self.max_related_tokens = max_related_tokens

    # ── public API ───────────────────────────────────────────────────────
    def build(
        self,
        target_or_index_or_task: Any = None,
        *args: Any,
        target_function: Optional[str] = None,
        file: Optional[Path] = None,
        target_file_rel: Optional[str] = None,
        constraints: Optional[list[str]] = None,
        test_signals: Optional[dict] = None,
        files: Sequence[str] = (),
        symbols: Sequence[str] = (),
        budget_tokens: Optional[int] = None,
        focus_paths: Sequence[str] = (),
        include_repo_map: Optional[bool] = None,
        **kwargs: Any,
    ) -> ContextSlice:
        """Assemble a context slice.

        Supports both:
        1. Target function slicing: ``build(index, target_function=...)``
        2. Task keyword slicing: ``build(task, files=..., symbols=...)``
        """
        if isinstance(target_or_index_or_task, L0Index) or target_function is not None:
            index = target_or_index_or_task if isinstance(target_or_index_or_task, L0Index) else None
            tf = target_function or (args[0] if args else "")
            return self._build_target_slice(
                index,
                target_function=tf,
                file=file,
                target_file_rel=target_file_rel,
                constraints=constraints,
                test_signals=test_signals,
            )
        task = target_or_index_or_task if isinstance(target_or_index_or_task, str) else ""
        return self._build_task_slice(
            task,
            files=files,
            symbols=symbols,
            budget_tokens=budget_tokens,
            focus_paths=focus_paths,
            include_repo_map=include_repo_map,
        )

    def _build_task_slice(
        self,
        task: str,
        *,
        files: Sequence[str] = (),
        symbols: Sequence[str] = (),
        budget_tokens: Optional[int] = None,
        focus_paths: Sequence[str] = (),
        include_repo_map: Optional[bool] = None,
    ) -> ContextSlice:
        if self.store is None:
            raise SliceError(
                "context slicing needs an L0Store",
                detail={"hint": "pass store=... to ContextSlicer or ContextFirewall"},
            )
        cfg = self.config
        budget = int(budget_tokens or cfg.slice_budget_tokens)
        workspace_files = tuple(
            path for raw in files
            if (path := _workspace_relative_path(raw, self.root))
        )
        extraction = self.extractor.extract(task, extra=[*workspace_files, *symbols])
        # Absolute paths outside this project can still be useful as read-only
        # prompt context, but they are never symbol-index seeds or diff targets.
        extraction.paths = [
            path for raw in extraction.paths
            if (path := _workspace_relative_path(raw, self.root))
        ]

        seeds = self._seed(extraction, workspace_files, symbols)
        if not seeds:
            # Nothing matched: a slice would be empty and misleading.
            raise EmptySliceError(
                "no indexed symbol matched the task",
                detail={"keywords": extraction.keywords, "files": list(workspace_files)},
            )

        expanded = self._expand(seeds)
        ranked = self._rank(expanded, extraction)
        selected, dropped_symbols = self._fit(ranked, budget, include_repo_map)

        graph = self._call_graph(selected)
        blast = self._blast_radius(selected)
        tests = self._tests(
            selected,
            tuple(path for raw in focus_paths
                  if (path := _workspace_relative_path(raw, self.root))),
        )
        repo_map = self._repo_map(selected, extraction, include_repo_map)

        files_touched = sorted({s.path for s in selected})
        dropped: dict[str, int] = {}
        if dropped_symbols:
            dropped["symbols"] = dropped_symbols

        slice_ = ContextSlice(
            task=task,
            keywords=extraction.keywords,
            paths=extraction.paths,
            symbols=selected,
            call_graph=graph,
            blast_radius=blast,
            tests=tests,
            files=files_touched,
            repo_map=repo_map,
            budget=budget,
            truncated=bool(dropped),
            dropped=dropped,
        )
        slice_.tokens = estimate_tokens_from_chars(len(slice_.render()), cfg.chars_per_token)
        slice_.truncated = slice_.tokens > budget or bool(dropped)
        return slice_

    def _build_target_slice(
        self,
        index: Optional[L0Index],
        *,
        target_function: str,
        file: Path | None = None,
        target_file_rel: str | None = None,
        constraints: list[str] | None = None,
        test_signals: dict | None = None,
    ) -> ContextSlice:
        if index is None:
            index = L0Index()
        sym = index.get_symbol(target_function)
        if sym is None:
            raise ValueError(
                f"target function not found in L0 index: {target_function!r}"
            )

        target_file = Path(file) if file is not None else Path(sym.path)
        if not target_file.exists():
            raise FileNotFoundError(f"target file not found: {target_file}")

        body = _extract_body(sym, target_file)
        callers_raw = list(index.get_callers(target_function))
        callers = [c for c in callers_raw if c and c != "<module>"]
        deps = list(index.dependencies_of(target_function))

        all_imports = index.get_imports(str(target_file))
        search_text = sym.signature + "\n" + body
        relevant_imports: list[ImportItem] = []
        for imp in all_imports:
            alias = imp.alias or imp.module.rsplit(".", 1)[-1]
            if not alias or alias == "*":
                continue
            if _alias_ref_re(alias).search(search_text):
                relevant_imports.append(ImportItem(module=imp.module, alias=imp.alias))

        related = self._collect_related_symbols(index, sym, callers, body)

        return ContextSlice(
            target_function=TargetFunction(
                name=sym.name,
                signature=sym.signature,
                body=body,
                dependencies=deps,
                callers=callers,
            ),
            relevant_imports=relevant_imports,
            related_symbols=related,
            constraints=list(constraints or []),
            test_signals=dict(test_signals or {}),
            file_path=str(target_file),
            target_file=target_file_rel,
        )

    def _collect_related_symbols(
        self,
        index: L0Index,
        target: SymbolInfo,
        callers: list[str],
        body: str,
    ) -> list[RelatedSymbol]:
        qnames: set[str] = set()

        for c in callers:
            if c and c != "<module>":
                qnames.add(c)

        callee_names: set[str] = set()
        for m in re.finditer(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(", body):
            callee_names.add(m.group(1))
        for m in re.finditer(r"\.([A-Za-z_][A-Za-z0-9_]*)\s*\(", body):
            callee_names.add(m.group(1))
        callee_names.discard(target.name)
        for name in callee_names:
            s = index.get_symbol(name)
            if s is not None:
                qnames.add(s.qname)

        related: list[RelatedSymbol] = []
        for qname in qnames:
            s = index.get_symbol(qname)
            if s is None:
                continue
            cc = len(index.get_callers(qname))
            related.append(
                RelatedSymbol(
                    qname=s.qname,
                    kind=s.kind,
                    signature=s.signature,
                    caller_count=cc,
                )
            )

        related.sort(key=lambda r: (-r.caller_count, r.qname))

        capped: list[RelatedSymbol] = []
        used = 0
        for r in related:
            item_cost = estimate_tokens(r.signature) + estimate_tokens(r.qname) + 4
            if used + item_cost > self.max_related_tokens:
                continue
            capped.append(r)
            used += item_cost
        return capped


    # ── 1. seeding ───────────────────────────────────────────────────────
    def _seed(
        self,
        extraction: Extraction,
        files: Sequence[str],
        symbols: Sequence[str],
    ) -> dict[str, dict[str, Any]]:
        """Collect candidate symbols keyed by symbol_id, with a base score."""
        cfg = self.config
        out: dict[str, dict[str, Any]] = {}

        # explicit file hints: every public symbol in the file is a seed
        for path in [*files, *extraction.paths]:
            for row in self.store.symbols_in_file(path):
                self._bump(out, row, score=6.0, reason=f"file:{path}", hop=0)

        # explicit symbol hints
        for name in symbols:
            for row in self.store.find_symbol(name, exact=False):
                self._bump(out, row, score=7.0, reason=f"symbol:{name}", hop=0)

        # keyword search: FTS + exact match
        for term in extraction.terms[: cfg.slice_max_seeds * 2]:
            for row in self.store.find_symbol(term.value, exact=True):
                self._bump(out, row, score=4.0 + term.score, reason=f"exact:{term.value}", hop=0)
            for hit in self.store.search(term.value, limit=cfg.slice_max_seeds):
                sid = hit["symbol_id"]
                row = self.store.symbol_by_id(sid)
                if row is None:
                    continue
                self._bump(
                    out,
                    row,
                    score=1.5 + term.weight * float(hit.get("score", 1.0)),
                    reason=f"search:{term.value}",
                    hop=0,
                )

        return out

    @staticmethod
    def _bump(
        acc: dict[str, dict[str, Any]],
        row: Any,
        *,
        score: float,
        reason: str,
        hop: int,
    ) -> None:
        data = dict(row)
        sid = str(data.get("symbol_id") or "")
        if not sid:
            return
        entry = acc.get(sid)
        if entry is None:
            acc[sid] = {
                "row": data,
                "score": float(score),
                "hop": hop,
                "reasons": [reason],
            }
            return
        entry["score"] = max(entry["score"], float(score)) + 0.35 * float(score)
        entry["hop"] = min(entry["hop"], hop)
        if reason not in entry["reasons"]:
            entry["reasons"].append(reason)

    # ── 2. expansion ─────────────────────────────────────────────────────
    def _expand(self, seeds: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """Walk the call graph outwards from the seeds, decaying each hop."""
        cfg = self.config
        acc = dict(seeds)
        frontier = list(seeds.items())
        for hop in range(1, cfg.slice_expand_depth + 1):
            next_frontier: list[tuple[str, dict[str, Any]]] = []
            for sid, entry in frontier:
                base = entry["score"] * (HOP_DECAY**hop)
                if base < 0.4:
                    continue
                for neighbour, reason in self._neighbours(sid):
                    nsid = str(neighbour.get("symbol_id") or "")
                    if not nsid:
                        continue
                    before = sid in acc
                    self._bump(acc, neighbour, score=base, reason=reason, hop=hop)
                    if not before or acc.get(nsid, {}).get("hop", hop) == hop:
                        next_frontier.append((nsid, acc[nsid]))
                    if len(acc) >= cfg.slice_max_symbols * 3:
                        return acc
            frontier = next_frontier
            if not frontier:
                break
        return acc

    def _neighbours(self, symbol_id: str) -> list[tuple[Any, str]]:
        """Direct neighbours in the call graph, with the edge's reason label.

        Every row is materialised into a plain ``dict`` here: ``sqlite3.Row``
        supports ``row["key"]`` but not ``.get()``, and the expansion path reads
        columns defensively.
        """
        cfg = self.config
        out: list[tuple[Any, str]] = []
        for row in self.store.callers_of(symbol_id, limit=cfg.slice_max_callers):
            out.append((dict(row), "caller"))
        for row in self.store.callees_of(symbol_id, limit=cfg.slice_max_callees):
            dst = row["dst"]
            if dst:
                sub = self.store.symbol_by_id(str(dst))
                if sub is not None:
                    out.append((dict(sub), "callee"))
        return out

    # ── 3. ranking ───────────────────────────────────────────────────────
    def _rank(
        self, candidates: dict[str, dict[str, Any]], extraction: Extraction
    ) -> list[SliceSymbol]:
        cfg = self.config
        degrees = self.store.in_degrees()
        max_degree = max(degrees.values(), default=1) or 1
        keyword_set = {t.value.lower() for t in extraction.terms}
        path_set = {p.lower() for p in extraction.paths}

        ranked: list[SliceSymbol] = []
        for sid, entry in candidates.items():
            row = entry["row"]
            score = float(entry["score"])
            name = str(row.get("name") or "")
            qualified = str(row.get("qualified_name") or name)
            kind = str(row.get("kind") or "")
            path = str(row.get("path") or "")

            score += KIND_BONUS.get(kind, 1.0)
            score += 1.4 * (degrees.get(sid, 0) / max_degree)
            if int(row.get("is_public") or 0):
                score += 0.8
            else:
                score -= 0.7
            if name.lower() in keyword_set or qualified.lower() in keyword_set:
                score += 2.0
            if path.lower() in path_set or any(path.lower().endswith(p) for p in path_set):
                score += 2.5
            if entry["hop"]:
                score -= 0.9 * entry["hop"]

            ranked.append(
                SliceSymbol(
                    symbol_id=sid,
                    name=name,
                    qualified_name=qualified,
                    kind=kind,
                    path=path,
                    signature=str(row.get("signature") or ""),
                    docstring=str(row.get("docstring") or ""),
                    start_line=int(row.get("start_line") or 0),
                    end_line=int(row.get("end_line") or 0),
                    is_public=bool(int(row.get("is_public") or 0)),
                    score=score,
                    hop=int(entry["hop"]),
                    reasons=list(entry["reasons"]),
                )
            )
        ranked.sort(key=lambda s: (-s.score, s.path, s.start_line, s.name))
        return ranked[: cfg.slice_max_symbols * 2]

    # ── 4. budget fitting ────────────────────────────────────────────────
    def _fit(
        self,
        ranked: list[SliceSymbol],
        budget: int,
        include_repo_map: Optional[bool],
    ) -> tuple[list[SliceSymbol], int]:
        """Take symbols until the budget would be exceeded."""
        cfg = self.config
        reserve = 0
        want_map = cfg.slice_include_repo_map if include_repo_map is None else include_repo_map
        if want_map:
            reserve = cfg.slice_repo_map_tokens

        remaining = max(200, budget - reserve)
        selected: list[SliceSymbol] = []
        used = 0
        for sym in ranked:
            if len(selected) >= cfg.slice_max_symbols:
                break
            sym.excerpt = self._excerpt(sym)
            cost = estimate_tokens_from_chars(len(sym.signature) + len(sym.excerpt) + 90, cfg.chars_per_token)
            if used + cost > remaining and selected:
                break
            selected.append(sym)
            used += cost
        dropped = max(0, len(ranked) - len(selected))
        return selected, dropped

    def _excerpt(self, sym: SliceSymbol) -> str:
        """Read the symbol's body from disk, clipped to the snippet limit."""
        cfg = self.config
        if not sym.path or not sym.end_line:
            return ""
        path = self.root / sym.path
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        lines = text.splitlines()
        start = max(0, sym.start_line - 1)
        end = min(len(lines), sym.end_line)
        body = "\n".join(lines[start:end]).strip()
        if len(body) > cfg.slice_symbol_snippet_chars:
            return body[: cfg.slice_symbol_snippet_chars] + f" {cfg.truncation_marker}"
        return body

    # ── 5. sections ──────────────────────────────────────────────────────
    def _call_graph(self, symbols: Sequence[SliceSymbol]) -> dict[str, dict[str, list[dict[str, Any]]]]:
        cfg = self.config
        out: dict[str, dict[str, list[dict[str, Any]]]] = {}
        for sym in symbols[:20]:
            callers = [
                {
                    "name": str(r["name"]),
                    "path": str(r["path"]),
                    "line": int(r["call_line"] or 0),
                }
                for r in self.store.callers_of(sym.symbol_id, limit=cfg.slice_max_callers)
            ]
            callees: list[dict[str, Any]] = []
            for r in self.store.callees_of(sym.symbol_id, limit=cfg.slice_max_callees):
                callees.append(
                    {"name": str(r["name"] or r["dst"]), "path": str(r["path"] or ""), "line": int(r["line"] or 0)}
                )
            if callers or callees:
                out[sym.qualified_name] = {"callers": callers, "callees": callees}
        return out

    def _blast_radius(self, symbols: Sequence[SliceSymbol]) -> list[dict[str, Any]]:
        """Every distinct caller of the selected symbols — what a change can break."""
        cfg = self.config
        seen: set[tuple[str, str]] = set()
        out: list[dict[str, Any]] = []
        for sym in symbols:
            for row in self.store.callers_of(sym.symbol_id, limit=cfg.slice_max_blast):
                key = (str(row["symbol_id"]), str(row["path"]))
                if key in seen:
                    continue
                seen.add(key)
                out.append(
                    {
                        "symbol_id": str(row["symbol_id"]),
                        "name": str(row["name"]),
                        "path": str(row["path"]),
                        "line": int(row["call_line"] or 0),
                        "calls": sym.qualified_name,
                    }
                )
                if len(out) >= cfg.slice_max_blast:
                    return out
        return out

    def _tests(self, symbols: Sequence[SliceSymbol], focus_paths: Sequence[str]) -> list[str]:
        """Indexed files that look like tests covering the touched modules."""
        cfg = self.config
        stems = {Path(s.path).stem for s in symbols if s.path}
        stems |= {Path(p).stem for p in focus_paths}
        out: list[str] = []
        for row in self.store.all_files():
            data = dict(row)
            path = str(data.get("path") or "")
            if not path or not self._is_test_path(path):
                continue
            stem = Path(path).stem
            related = any(stem == f"test_{s}" or stem.startswith(f"test_{s}_") or s in stem for s in stems)
            if related or not stems:
                if path not in out:
                    out.append(path)
            if len(out) >= cfg.slice_max_tests:
                break
        return out

    def _is_test_path(self, path: str) -> bool:
        name = path.rsplit("/", 1)[-1]
        for pattern in self.config.slice_test_globs:
            if fnmatch.fnmatchcase(path, pattern) or fnmatch.fnmatchcase(name, pattern.split("/")[-1]):
                return True
            if "/tests/" in f"/{path}" or path.startswith("tests/"):
                return True
        return False

    def _repo_map(
        self,
        symbols: Sequence[SliceSymbol],
        extraction: Extraction,
        include_repo_map: Optional[bool],
    ) -> str:
        cfg = self.config
        want = cfg.slice_include_repo_map if include_repo_map is None else include_repo_map
        if not want:
            return ""
        focus = [s.qualified_name for s in symbols[:8]] or extraction.keywords[:8]
        try:
            builder = RepoMapBuilder(self.store, config=_l0_config(self.store, cfg))
            result = builder.build(
                budget_tokens=cfg.slice_repo_map_tokens,
                focus=focus,
                use_cache=True,
            )
        except Exception:  # pragma: no cover - a missing index must not break the slice
            return ""
        return result.text


# ── helpers ──────────────────────────────────────────────────────────────
def _l0_config(store: L0Store, cfg: FirewallConfig) -> Any:
    """Reuse the store's own L0Config, retargeting the repo map budget."""
    base = getattr(store, "config", None)
    if base is None:  # pragma: no cover - a store always carries a config
        return None
    return base.with_(
        repo_map_budget_tokens=cfg.slice_repo_map_tokens,
        chars_per_token=cfg.chars_per_token,
    )


# --- tree-sitter body extraction --------------------------------------------


def _extract_body(sym: SymbolInfo, target_file: Path) -> str:
    """Return the body block text of ``sym`` from ``target_file``."""
    source = target_file.read_text(encoding="utf-8")
    root = _PARSER.parse(bytes(source, "utf8")).root_node
    found = _find_node_by_qname(root, sym.qname)
    if found is None:
        lines = source.split("\n")
        return "\n".join(lines[sym.start_line : sym.end_line])
    block = found.child_by_field_name("body")
    if block is None:
        return ""
    return block.text.decode("utf-8")


def _find_node_by_qname(root: Any, qname: str) -> Any:
    """Locate a function/class node by qname (supports ``Class.method``)."""
    parts = qname.split(".")
    return _find_in_node(root, parts)


def _find_in_node(node: Any, parts: list[str]) -> Any:
    if not parts:
        return None
    head = parts[0]
    for child in node.children:
        if child.type == "decorated_definition":
            inner = child.child_by_field_name("definition") or next(
                (
                    c
                    for c in child.children
                    if c.type in ("function_definition", "class_definition")
                ),
                None,
            )
            if inner is not None and _node_name(inner) == head:
                if len(parts) == 1:
                    return inner
                if inner.type == "class_definition":
                    res = _find_in_class_body(inner, parts[1:])
                    if res is not None:
                        return res
        elif child.type in ("function_definition", "class_definition"):
            if _node_name(child) == head:
                if len(parts) == 1:
                    return child
                if child.type == "class_definition":
                    res = _find_in_class_body(child, parts[1:])
                    if res is not None:
                        return res
    return None


def _find_in_class_body(class_node: Any, parts: list[str]) -> Any:
    block = class_node.child_by_field_name("body")
    if block is None:
        return None
    return _find_in_node(block, parts)


def _node_name(node: Any) -> str | None:
    for ch in node.children:
        if ch.type == "identifier":
            return ch.text.decode("utf-8")
    return None


__all__ = [
    "ContextSlicer",
    "ContextSlice",
    "SliceSymbol",
    "TargetFunction",
    "ImportItem",
    "RelatedSymbol",
    "KIND_BONUS",
    "HOP_DECAY",
]

