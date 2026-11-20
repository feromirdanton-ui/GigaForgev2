# GigaForge V2 — Backend Audit & Optimisation Notes

> Historical audit of the V2 snapshot. Test counts and commands below refer to
> that earlier checkout; the current repository no longer includes its test
> suite. See [`ARCHITECTURE_REVIEW.md`](ARCHITECTURE_REVIEW.md) for the current
> status and modernization priorities.

**Author:** Backend Architect  
**Date:** 2026-10-12  
**Scope:** layers M1–M6 (`client`, `l0`, `firewall`, `core`, `cli/ui`, `devops`)  
**Source of truth:** https://github.com/feromirdanton-ui/GigaForge

---

## 1. Baseline (measured, not estimated)

The repository was empty when this audit started, so the M1–M4 codebase was
recovered from the previously delivered artifacts and re-verified locally before
any change was made.

| Layer | Module | LOC | Baseline evidence |
|---|---|---|---|
| M1 | `gigaforge/client` | 3,106 | `pytest tests/` → **33 passed** in 0.02s; `examples/smoke.py` → metrics + streaming OK |
| M2 | `gigaforge/l0` | 4,317 | `examples/smoke_m2.py` → repo map, AST tools, lint rollback OK (0 LLM calls) |
| M3 | `gigaforge/firewall` | 4,889 | compress / slice / validate exercised via M4 smoke |
| M4–M5 | `gigaforge/core` | 5,902 | `examples/smoke_m4.py` → **41/41 checks passed** |
| **Total** | | **18,214** | |

Environment: Python 3.11, `pytest`, `tree-sitter` (+ python/js/ts/go/rust grammars),
`ruff`, `typer`, `rich`. All tests run **offline** against scripted transports.

---

## 2. Defects and bottlenecks found

### D1 — Three SQLite connections, one file (write-lock contention) — **high**
`MetricsCollector`, `CacheManager` and `CheckpointStore` each call
`sqlite3.connect()` on the same `db_path`, each with its own lock. V1's executor
is sequential so this only *sometimes* bites, but any concurrency (sub-agents,
wave scheduling) turns it into `database is locked`.
**Fix:** `core/db.py` — one shared connection per path, WAL journal,
`busy_timeout`, `synchronous=NORMAL`.

### D2 — Sequential DAG execution leaves the machine idle — **high**
`DagExecutor.run` walks the topological order one node at a time. `index` and
`slice` are independent; sub-agent fan-out is embarrassingly parallel.
**Fix:** `core/parallel.py` — level-based wave scheduler with bounded
concurrency and deterministic (declared-order) result aggregation.

### D3 — Tokens counted, cost never computed — **medium**
`PhaseUsage` tracks tokens but nothing converts them to money, so a run cannot
answer "what did this cost?".
**Fix:** `core/cost.py` — price table (env-overridable) + integer micro-USD
accumulator.

### D4 — No single UI-ready telemetry object — **medium**
Metrics points and the JSONL trace exist, but a dashboard must re-derive
cache-hit rate, reject rate, per-phase cost and the step timeline itself.
**Fix:** `core/telemetry.py` — `RunTelemetry` aggregates a `PipelineResult` or a
trace into one JSON payload.

### D5 — Context slices are not deduplicated or budget-trimmed — **medium**
A slice assembled from several sources repeats symbols; an over-budget slice is
sent whole.
**Fix:** `core/optimize.py` — deterministic dedupe + whole-block tail trim.

### D6 — `_call_with_timeout` abandons a thread per node — **low**
A fresh `ThreadPoolExecutor` is created for every timed node. Correct, but
wasteful under wave scheduling.
**Note:** documented; the wave scheduler reuses one pool per level.

### D7 — `estimate_tokens` duplicated in three modules — **low**
`router.py`, `firewall/compress/base.py` and `optimize.py` each define it.
**Note:** kept as-is for layer independence; `optimize.estimate_tokens` is the
canonical V2 helper.

### D8 — Multi-file diffs without `diff --git` headers were merged — **high**
`firewall/diff.py` only started a new file record on a `diff --git` line. A diff
that uses bare `--- a/x` / `+++ b/x` pairs (the common shape an LLM emits) had
every file after the first folded into the first record, so a two-file patch was
applied as one — silently corrupting the second file's hunks.
**Fix:** `_parse` now also opens a new file record on a `--- ` line that follows a
completed hunk, while still treating the `---`/`+++` pair of a *single* file as one
record.

### D9 — mypy diagnostics were mis-detected as gcc — **medium**
`firewall/compress/builderr.py` classified a build log as mypy only when a line
ended with `[error-<code>]`. Real mypy output uses bare codes (`[return-value]`,
`[assignment]`), so mypy logs fell through to the gcc parser and lost their
structured fields.
**Fix:** the detector now accepts both `[error-<code>]` and bare `[<code>]` forms.

### D10 — pytest section banner regex missed the short-summary banner — **low**
`firewall/compress/pytest.py` matched `==== FAILURES ====` but not
`==== short test summary info ====`, so the summary block was parsed as failure
detail.
**Fix:** the banner regex now accepts any `==== <text> ====` section header.

---

## 3. V2 backend changes

| Module | Change |
|---|---|
| `core/db.py` | **new** — shared SQLite connection factory (WAL, busy_timeout, registry) |
| `core/parallel.py` | **new** — wave/level DAG scheduler, bounded concurrency, deterministic order |
| `core/cost.py` | **new** — model price table + micro-USD cost accumulator |
| `core/telemetry.py` | **new** — `RunTelemetry` → UI-ready JSON (tokens, cost, cache hit rate, reject rate, steps, diff) |
| `core/optimize.py` | **new** — context dedupe + budget trim |
| `core/__init__.py` | re-exports the V2 surface |
| `firewall/diff.py` | **fixed** — multi-file parse (D8) |
| `firewall/compress/builderr.py` | **fixed** — mypy detection (D9) |
| `firewall/compress/pytest.py` | **fixed** — section banner (D10) |
| `tests/test_v2_backend.py` | **new** — offline tests for every V2 module |
| `tests/test_firewall.py` | **new** — 54 offline tests for M3 (compress / slice / diff / validate) |

### Design rules carried into V2
- **Determinism first.** Wave results are returned in declared order, never
  completion order — a trace must be reproducible.
- **Isolation.** A node only sees the artifacts it declared in `requires`; a
  level snapshot is taken before the level starts.
- **Integer money.** Costs accumulate as micro-USD so long runs cannot drift.
- **Config, not constants.** Prices and limits are env-overridable
  (`GF_CORE__PRICES__*`).

---

## 4. Verification

- `pytest tests/` — M1 client suite (33) + V2 backend suite (19) + M3 firewall
  suite (54), all offline → **106 passed**.
- `examples/smoke_m4.py` — 41/41 checks still pass after the V2 additions.
- `python -m compileall gigaforge` — clean.
- `ruff check tests/test_firewall.py` — clean.

---

## 5. Handoff

V2 backend is committed to the repository. The remaining relay:
**Frontend Developer** (TUI + web panel on `MetricsCollector` / JSONL trace) →
**DevOps Automator** (CI/CD, containerisation, allowlist, env secrets, dev/prod) →
**API Tester** (full test + acceptance) → **Code Reviewer** (correctness, security,
performance).
