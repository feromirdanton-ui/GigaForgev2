# Changelog

## [4.1.0] — 2026-10-18 — one chat window, attachments, web-file editing

### Added
- **Drag & drop**: dropping a file *or folder* on the transcript/composer attaches it
  (`tkinterdnd2`, optional — the 📎 button and pasted paths work without it).
  Attachments appear as chips, as `📎 name` in the transcript, and are appended to
  the message so the agent's file-context extractor sees the exact paths.
- **Focused creation pass**: if a goal names a file that does not exist and the diff
  does not create it, one create-only call per file follows. Models merge several
  files into one `diff --git` block and the parser cannot know which file a stray
  creation hunk belonged to, so this closes the gap deterministically.
- `DirectChatBackend` — the *chat* verb: questions go to the model with a bounded
  conversation window and never touch the repository.
- Settings dialog (providers, model, think, max tokens, doctor); `ctrl+,`.

### Fixed
- **HTML/CSS edits were always rejected**: ruff ran on every file, reported
  `invalid-syntax` for HTML and the validator treated that as fatal. Linting is now
  Python-only.
- **Diff headers were not repaired**: `--- index1.html` / `+++ index1_new.html` was
  read as a rename, and a bare `---`/`+++` pair lost its old path (an edit looked
  like a creation). The parser keeps the old path, treats all-insertion bodies as
  creations, drops creation hunks merged into another file's block, and repairs the
  target to the file that exists — with warnings fed back into the repair loop.
- **Empty slice for web projects**: the repo map is built from indexed symbols, so an
  HTML-only repository had no seed files. The slice fallback now walks the filesystem
  when the index has nothing to rank.
- **File context ignored absolute paths and siblings**: `M:\YESTER\index.html` and its
  `index1/index2` siblings are now included (outside-repo files as read-only
  context) — what a "create index2, fix index1" request needs.
- **Atomic apply discarded good edits**: one hallucinated hunk in a test file
  reverted the module edit beside it. Files apply independently (`apply_partial`);
  the diff fails only when nothing applied.

## [3.0.0] — 2026-10-18 — Synthesis + desktop application

The 3.0 line makes the harness *runnable* again and turns the interface into a
program: a standalone desktop window is the primary surface, and the browser
control room is a feature started from inside it.

### Added — desktop application
- `gigaforge/desktop/` — the GigaForge window (Tkinter, stdlib only): dark
  control-room theme, seven pages (Dashboard, Chat, Runs, Diff, Index, System,
  Browser UI), a serial background worker that keeps model calls off the UI
  thread, `new run` dialog, toasts, keyboard map (`ctrl+1…7`, `ctrl+r`, `F5`,
  `ctrl+q`), DPI awareness and a generated icon.
- `gigaforge desktop` / `gigaforge app` / `gigaforge-desktop` /
  `python -m gigaforge.desktop` — four equivalent entry points; `--view`,
  `--repo`, `--offline`, `--require-provider`.
- `start.bat` now launches the window (windowed `pythonw`, no console left
  behind); the 15-mode terminal menu moved to `menu.bat`.
- `install-shortcut.bat` — Desktop + Start Menu shortcut with the app icon.
- `scripts/make_icon.py` — regenerates `assets/gigaforge.ico|png` from the UI
  palette (no opaque binary to maintain).
- The window opens in **read-only mode** with no credential: telemetry, traces,
  the index, chat commands and the browser panel all work; only runs need a key.

### Added — synthesis (the modules the merge never wired)
- `gigaforge/core/sync_adapter.py` — `SyncChatClient`: the single blocking bridge
  between the synchronous DAG and the async transports, on one long-lived
  event loop (so the connection pool survives dozens of calls per run).
- `gigaforge/core/strategies.py` — pluggable **plan** and **review** stages:
  - `LinearPlan` (default) and `TreeOfThoughtPlan` — the K-branch search of
    `agents/tot` (varied temperatures, zero-token heuristic scoring, optional
    judge) driving the DAG's own router, budget and trace;
  - `NoReview` (default), `ConstitutionalReview` (the principle set of
    `agents/constitutional`: deterministic blockers + one soft-principle call)
    and `LLMReview`.
  - Selected by constructor argument or `GIGAFORGE_PLAN_STRATEGY=linear|tot`,
    `GIGAFORGE_REVIEW_STRATEGY=none|constitutional|llm`.
- New `review` DAG node between `fix` and `settle`; `settle` applies the veto, so
  a reviewer can narrow an acceptance but never revive a rejected diff.
- `gigaforge/runtime.py` rebuilt as the only composition root (client, providers,
  L0, firewall, pipeline, stores, strategies) — shared by the CLI, the desktop
  window and the chat backend.
- Providers are live: `build_runtime` attaches the GigaChat client to the
  registry and honours `GIGAFORGE_PROVIDER=openai`; `gigaforge providers` and the
  System page report what is registered and what is configured.
- `gigaforge/agents/__init__.py` exports all 14 agents (3 eager, 11 lazily) plus
  `AGENT_CATALOG`, rendered by `gigaforge agents` and the window's agent panel.

### Fixed
- `runtime.build_runtime()` crashed on `TokenProvider(settings=…)`; the CLI built
  a `GigaClient` with no token provider and a pipeline with no client at all, so
  every run failed before its first node.
- `ModelRouter` called `client.complete()`, which no transport implemented — the
  missing piece was the sync/async bridge above.
- `GigaClientAdapter` forwarded to the same non-existent method; it now *is*
  `SyncChatClient`.
- L0 SQLite connections were thread-affine, so the DAG's worker threads could not
  read the index (`SQLite objects created in a thread…`).
- `firewall/slicer.py` called `estimate_tokens(chars, chars_per_token)` on the
  one-argument helper — every non-empty slice raised `TypeError`.
- `repo_map` cache dropped the `ranked` list, so the second caller of an
  unchanged repository saw an empty ranking.
- `FirewallConfig.repo_root` stayed `"."`, so diffs were validated against the
  process directory instead of the target repository ("did not apply cleanly").
- `runs.input` was never written; `runs.accepted` had no source, so every run
  rendered as "not accepted".
- Router/DAG/pipeline metrics carried no `run_id`, so per-run KPIs silently
  summed every run ever.
- `gigaforge/nexus/server.py` shipped an embedded demo credential; removed
  (credentials come from the environment/`.env` only) and a missing key now
  produces an explicit warning instead of impersonation.
- Vague goals ("improve error handling") dead-ended on `EmptySliceError`; the
  slice node now retries against the highest-ranked files and always computes a
  cache key, so such a run is cacheable.
- `firewall/diff.py` emitted a `SyntaxWarning` for an invalid escape.

### Changed
- `pyproject.toml`: version 3.0.0, `gigaforge-desktop` script, tree-sitter
  grammars moved to the optional `languages` extra, test configuration removed.
- Every `.bat` launcher calls `python -m …` instead of a `gigaforge` script that
  may not be on `PATH`; `webstack.bat` starts both browser surfaces.
- `gigaforge doctor` output is reusable as data (`Context.doctor_checks`).
- The test suite and its fixtures were removed: correctness is now verified by
  running the harness (`doctor`, `run --dry-run`, `bench`) rather than by a
  parallel test-only path.

## [2.0.0] — 2026-10-12 — V2 backend + interface layer

### Added (interface layer)
- `gigaforge/ui/store.py` — read-only access to the sinks, with metric-name
  normalization (`core.router.calls` is stored as `counter.core.router.calls`;
  matching only the canonical spelling returns zeros silently).
- `gigaforge/ui/analytics.py` — pure KPI maths (rates, interpolated percentiles,
  integer micro-USD costs, diff accounting); no I/O, so it is testable alone.
- `gigaforge/ui/snapshot.py` — the single UI-ready snapshot dict every surface
  renders; degrades report → journal → trace, and renders an honest empty state.
- `gigaforge/ui/renderers.py` — console / Markdown / JSON, plus the exit codes.
- `gigaforge/ui/tui.py` — terminal dashboard (rich `Live`, plain fallback).
- `gigaforge/ui/webui.py` — self-contained HTML control room + stdlib HTTP server
  (`/api/snapshot`, `/api/events` SSE, `/api/health`, `POST /api/chat`).
- `gigaforge/ui/chat.py` — agentic chat: durable sessions, scripted/pipeline
  backends, slash commands, streamed step events.
- `gigaforge/ui/diffview.py` — unified-diff parsing and per-line styling.
- `gigaforge/ui/cache_ops.py` — the only mutating path: purge / invalidate /
  vacuum, with a dry run that truly writes nothing.
- `gigaforge/cli/app.py` — typer entry point: `run`, `report`, `trace`, `steps`,
  `diff`, `cost`, `cache`, `paths`, `ui`, `web`, `chat`.
- `examples/smoke_ui.py` — offline smoke: real telemetry → TUI + panel + chat.
- `tests/test_ui.py` — 28 tests over maths, sinks, renderers, panel and CLI.
- `docs/UI_V2.md` — the UI layer's design decisions and its gotchas.

### Added (V2 backend)
- `gigaforge/core/db.py` — shared SQLite connection factory (WAL journal,
  `busy_timeout`, `synchronous=NORMAL`, process-local registry). Replaces three
  independent connections competing for the same write lock.
- `gigaforge/core/parallel.py` — level-based **wave scheduler** for the DAG:
  bounded concurrency inside a level, deterministic (declared-order) result
  aggregation, cycle detection.
- `gigaforge/core/cost.py` — model price table (env-overridable via
  `GF_CORE__PRICES__*`) and an integer micro-USD `CostAccumulator`.
- `gigaforge/core/telemetry.py` — `RunTelemetry` aggregates a `PipelineResult`
  or a JSONL trace into one UI-ready payload: tokens, cost, cache hit rate,
  reject rate, per-phase/per-model cost, step timeline, diff size.
- `gigaforge/core/optimize.py` — deterministic context dedupe and whole-block
  budget trimming.
- `tests/test_v2_backend.py` — offline tests for every V2 module.
- `docs/V2_BACKEND_AUDIT.md` — baseline, defects and V2 changes.

### Changed
- `gigaforge/core/__init__.py` re-exports the V2 surface.
- `pyproject.toml` declares the `gigaforge` console script
  (`gigaforge.cli:main`).

### Fixed
- `RunTelemetry.reject_rate` is rejections over proposals (rejections are a
  subset of proposals), not over their sum.

## [1.0.0] — M1–M5

- M1 `client`: GigaChat OAuth with proactive refresh, SSE streaming, retries,
  token-bucket rate limiting, metrics.
- M2 `l0`: tree-sitter symbol index, AST edit tools, repo map, incremental
  indexing, ruff-gated lint.
- M3 `firewall`: tool-output compression, context slicing, diff validation.
- M4–M5 `core`: DAG executor with checkpoints/DLQ, model router with per-phase
  budgets, task cache, bounded fix loop, circuit breaker, sub-agents, metrics.
