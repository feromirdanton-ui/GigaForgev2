# GigaForge V2 — UI layer (CLI · TUI · Web · Agentic chat)

> Historical UI design record. Verification commands below refer to an earlier
> checkout whose test files are no longer present. For current architecture and
> cleanup status, see [`ARCHITECTURE_REVIEW.md`](ARCHITECTURE_REVIEW.md).

This document describes the front-end layer added on top of the V2 backend
(`client` → `l0` → `firewall` → `core`). It exists so the next person does not
have to reverse-engineer the design decisions from the code — each section says
*what* the layer does and *why* it is built that way.

## 1. The one rule everything follows: the UI reads the sinks

A dashboard handed a live in-memory result object only works inside the process
that produced it. One that reads the **persisted sinks** opens tomorrow, from
another process, and shows the same numbers. Every surface below — the terminal
TUI, the web panel, the chat commands — renders the *same* snapshot dict, built
by reading:

| sink | written by | read for |
|---|---|---|
| `metrics_points` (SQLite) | `MetricsCollector` | counters/gauges: calls, tokens, cache hit/miss, latency, rejections |
| `runs`, `node_runs`, `node_checkpoints`, `dlq` | `CheckpointStore` (DAG journal) | run status, per-node outcomes, retries, checkpoints |
| `cache_entries` | `CacheManager` | cache contents, namespaces, sizes (never the blob) |
| `*.jsonl` trace | `TraceWriter` | the ordered event stream: `dag.node_start`, `router.call`, `cache.hit`, … |

Nothing above the store layer opens a database, and **nothing in the read path
writes**. The single mutation (cache purge) lives in `ui/cache_ops.py`, behind
`gigaforge cache --purge --yes`.

## 2. Layer map

```
commands (typer)        gigaforge/cli/app.py     argument parsing, exit codes
  presentation          ui/tui.py · ui/webui.py · ui/chat.py     rendering only
  renderers             ui/renderers.py          console / markdown / json
  snapshot              ui/snapshot.py           ONE UI-ready dict, built once
  analytics             ui/analytics.py          pure KPI maths, no I/O
  store (READ-ONLY)     ui/store.py              the only door to SQLite + JSONL
  diff parsing          ui/diffview.py           unified-diff parsing + styling
  cache mutations       ui/cache_ops.py          purge / invalidate / vacuum
```

The layering is enforced by construction: `analytics.py` has no imports beyond
the standard library, so the KPI maths is unit-testable without a database;
`store.py` opens every connection `mode=ro`, so a dashboard can never block the
pipeline's writer.

## 3. Stored metric names carry a kind prefix

This is the single most expensive detail in the whole layer. `SqliteMetricsSink`
stores a point as `f"{kind}.{name}"`, so a component that records
`core.router.calls` lands in the table as **`counter.core.router.calls`**. A
reader that matches only the canonical spelling finds nothing and returns
**zeros, silently** — which is worse than an error, because the dashboard simply
looks empty and correct.

`store.stored_variants(name)` returns every spelling the sink may have used and
every lookup queries `metric IN (...)`. If a number is ever zero when it should
not be, start here: compare `gigaforge paths` output with
`SELECT DISTINCT metric FROM metrics_points`.

## 4. Interfaces

### Terminal TUI — `gigaforge ui [--watch]`

```
gigaforge ui                 # render the dashboard once
gigaforge ui --watch         # live repaint while a run proceeds
gigaforge ui --plain         # no colour, no rich — safe over a pipe
```

Panels: pipeline progress against the pipeline's declared node order, a KPI grid
(tokens, cost, cache hit rate, reject rate, calls, p95, duration), the step
timeline with retries, per-phase cost, a colour-coded diff, error signals and the
data-source notes. `--plain` and the no-`rich` fallback produce the same content
as boxed text, so the dashboard also works in a minimal deployment.

### Web control room — `gigaforge web`

```
gigaforge web --out panel.html     # one portable, self-contained file
gigaforge web --serve --port 8787  # live server: /api/snapshot, /api/events (SSE), /api/chat
```

Self-contained means self-contained: no CDN, no external font, no network
request. The snapshot rides in a `<script type="application/json">` block and
every `</` is escaped to `<\/`, so a goal string containing `</script>` cannot
close the tag early and blank the page (there is a test for exactly that).

Tabs: **Overview** (KPI cards, cache/reject meters, token trend, per-phase and
per-model tables, L0 index), **Steps**, **Trace** (filterable), **Diff**.

Polling is enabled only over `http(s)`: on a `file://` page the snapshot
endpoint does not exist, so the fetch would log a console error — instead the
embedded snapshot stays authoritative and the console stays clean.

### Agentic chat — `gigaforge chat`

```
gigaforge chat                          # REPL
gigaforge chat --message "/cost"        # one command, then exit
gigaforge chat --message "harden the diff validator"   # a goal → pipeline run
gigaforge chat --offline --message "/status"           # scripted backend, no model
```

Slash commands (`/help`, `/run`, `/status`, `/cost`, `/cache`, `/trace`,
`/steps`, `/diff`, `/models`, `/errors`, `/context`, `/export`, `/clear`) answer
from the read-only store; anything else is a **goal** and goes to the agent
backend. `PipelineBackend` runs the real `TaskPipeline` on a worker thread while
the chat polls the *trace file* for new `dag.node_*` events — so progress streams
during the run, and the same mechanism works for a run started by a different
process. The transcript is persisted as JSONL, one file per session, so a chat
survives a terminal restart.

`ChatEvent` (`say` / `step` / `tool` / `snapshot` / `error` / `command`) is the
only thing a renderer sees, which is why the same engine serves the REPL, the
`--json` line stream and the web panel's `POST /api/chat`.

### Read-only views

```
gigaforge report [--format console|markdown|json] [--out FILE]
gigaforge trace  [--filter TEXT] [--limit N] [--json]
gigaforge steps  [--json]
gigaforge diff   [--files-only] [--json]
gigaforge cost   [--json]
gigaforge cache  [--purge --yes [--namespace N] [--older-than 7d] [--vacuum]]
gigaforge paths  [--json]
```

## 5. Exit codes are part of the contract

| code | meaning |
|---|---|
| 0 | success |
| 1 | the run finished but was not accepted / failed |
| 2 | there is no data to show (no run recorded, empty sink) |
| 3 | usage or configuration error (including an unconfirmed purge) |

Machine output (`--json`) is written **straight to stdout, bypassing `rich`**:
`rich` soft-wraps at the terminal width and would insert newlines mid-token,
so pretty-printed JSON piped into `jq` would no longer parse.

## 6. Detailed gotchas the code encodes

Each of these produced wrong output before it was handled explicitly.

**A diff string ends with a newline.** Splitting yields a trailing empty element
that renders as a blank *context* line, so every hunk appears to gain a line.
`diffview.split_lines()` drops it; `analytics.diff_stats()` counts
`lines == 6`, not 7, for a 6-line diff.

**`/dev/null` is on both sides.** It is the old side of a creation *and* the new
side of a deletion, so a shared path classifier labels a new file as a deletion.
`file_action()` decides per side.

**`a/x.py` vs `b/x.py` is the same file.** Comparing the raw git paths calls
every entry a *rename*; `file_action()` compares the unprefixed paths.

**A timeline merges a node's retries.** `fix` appears twice in the journal (a
failed attempt then a success). The right thing to show is one entry with
`attempts = 2` and the summed duration, not two rows that make progress look
non-monotonic — and a test asserts `len(NODES) - len(steps) == 1`.

**Progress needs an external denominator.** `done/total` computed from the rows
seen so far is always 100%. Progress is measured against the pipeline's declared
`NODE_ORDER`, which is why `intake … persist` is a constant in `snapshot.py`.

**Ordering needs a tiebreaker.** Rows written in the same second share
`started_at`; `ORDER BY started_at, rowid` keeps the timeline stable across
reads (`ORDER BY` without `rowid` flipped the order between queries).

**`MetricsCollector` needs an explicit `db_path`.** A `CoreConfig` alone does not
attach the SQLite sink, and without it no `metrics_points` table is ever created
— a silent zero for every counter. `TaskPipeline` takes **keyword** arguments.

**`rich` `Text.assemble` does not parse markup.** `[green]✓[/]` prints
literally, and the same string leaks into HTML if reused. All dynamic spans use
`Text.append(..., style=...)`.

**`print` resolves to an instance method.** In `CliRunner` tests, a bare
`print(...)` inside a typer command wrote to the captured stdout; formatting is
routed through `renderers.write_machine()` so the machine path is unambiguous.

**A module without a `__main__` guard is an empty file.** `python -m
gigaforge.cli.app --help` imported the module, ran nothing and exited **0** — the
most confusing possible outcome (no usage, no error, success). `cli/app.py` now
has the guard and `cli/__main__.py` covers `python -m gigaforge.cli`. The test
asserts on *stdout*, not the exit code: the exit code was already 0 while the
command did nothing, which is exactly why an exit-code-only smoke check passed
over the bug.

## 7. Verification

```bash
python3 -m pytest tests/ -q            # 48 tests (29 UI + 19 backend), offline, deterministic
python3 -m compileall -q gigaforge examples tests
python3 examples/smoke_ui.py --out panel.html   # seeds real telemetry, renders both UIs
gigaforge --help && gigaforge ui --help && gigaforge web --help   # each command exits 0
```

Then, on the rendered panel: tag balance for every element type, `node --check`
on the concatenated inline JS, the embedded JSON snapshot parsed back out and its
shape asserted (so the page provably carries real data), a headless-browser load
asserting **zero console errors and zero failed requests**, and a screenshot read
back visually for layout defects.

`tests/test_ui.py` asserts on evidence the pipeline actually produced — token
counts, cache hit/miss counters, reject rates written through the production
`MetricsCollector`/`CheckpointStore` and read back through `UiStore` — never on
values the test itself set. The CLI tests drive `typer.testing.CliRunner` so
argument parsing, exit codes and output are exercised together.

## 8. Files

| file | purpose |
|---|---|
| `gigaforge/ui/store.py` | read-only access to the sinks + metric-name normalization |
| `gigaforge/ui/analytics.py` | pure KPI maths (rates, percentiles, cost, diff stats) |
| `gigaforge/ui/snapshot.py` | the single UI-ready snapshot dict |
| `gigaforge/ui/renderers.py` | console / markdown / JSON rendering + exit codes |
| `gigaforge/ui/tui.py` | terminal dashboard (rich `Live`, plain fallback) |
| `gigaforge/ui/webui.py` | self-contained HTML panel + stdlib HTTP server |
| `gigaforge/ui/chat.py` | agentic chat: sessions, backends, slash commands, REPL |
| `gigaforge/ui/diffview.py` | unified-diff parsing and per-line styling |
| `gigaforge/ui/cache_ops.py` | the only mutating path: purge / invalidate / vacuum |
| `gigaforge/cli/app.py` | typer application (`run`, `report`, `trace`, `steps`, `diff`, `cost`, `cache`, `paths`, `ui`, `web`, `chat`) |
| `examples/smoke_ui.py` | offline smoke: real telemetry → TUI + panel + chat |
| `tests/test_ui.py` | 29 tests over maths, sinks, renderers, panel and CLI |
| `docs/ui/dashboard.html` | a generated reference panel — not committed; produce one with `examples/smoke_ui.py --out docs/ui/dashboard.html` |

## 9. Known gaps / next steps

- The web panel has **no authentication**. `web --serve` binds `127.0.0.1` by
  default; expose it only behind a reverse proxy that adds auth.
- `core.router.tokens` records a total without the prompt/completion split, so
  per-phase cost prices the whole total at the completion rate. The report's
  recorded `prompt_tokens` is used when present; a labelled split in the metric
  would make this exact.
- `certifi` is imported eagerly by the client layer, so an offline deployment
  needs a system CA bundle. The UI layer itself never touches the network.
- The TUI's `watch` mode polls the sinks; a push-based stream (SSE from the
  server, or a filesystem watch) would cut the repaint latency and is the
  natural next optimisation.
