# GigaForge V6.fresh

**GigaForge** is an orchestration harness for the **GigaChat** API (Sber): a DAG of
agents that plans, authors, validates and reviews repository changes under a hard
token budget, with a deterministic context pipeline and zero-token static analysis.

It ships as a **standalone desktop application**. Double-click the shortcut (or run
`start.bat`) and a window opens — no browser, no terminal, no server.

```
L0  deterministic   tree-sitter + SQLite symbol index, AST edits, repo map, lint
L1  cheap model     retrieval, summarisation, triage, review
L2  main model      planning, diff authoring, repair
```

---

## Run it

```bash
pip install -e .                      # once
start.bat                             # Windows: opens the Qt Quick workspace
gigaforge desktop                     # same primary workspace
gigaforge-desktop                     # or the installed script
python -m gigaforge.desktop           # or by module
gigaforge desktop --ui tk             # legacy Tk fallback
```

The launcher provisions Qt Quick on first start. The Tk window remains available
as an explicit fallback while the workspace continues to gain editor and agent
capabilities.

The window opens even **without a credential** — it runs in *read-only* mode:
dashboards, traces, the symbol index and every `/command` work from the local
sinks. Runs additionally need:

```bash
# .env
GIGAFORGE_AUTH_KEY=<base64(client_id:client_secret)>
```

The Settings page accepts a credential and endpoint changes without a restart and
saves them in the local `.env`. TLS verification is off by default for the current
GigaChat API connection; the same page lets an operator enable it where the API
certificate chain is reliable.

### The window

| page | what it is for |
|---|---|
| **Обзор** | состояние задачи, команда агентов и переходы в рабочие режимы |
| **Forge** | рабочий чат и режимы `chat`, `agent` и `goal` с потоковыми этапами оркестрации |
| **Файлы и редактор** | безопасное редактирование UTF-8 файлов проекта и команды системной оболочки в корне проекта |
| **Агенты** | роли, специализации и состояние команды |
| **Мониторинг** | актуальное состояние последнего запуска и живые события |
| **Настройки** | credentials, TLS и подтверждение файловых изменений `Ask` / `Always allow` |

В рабочем пространстве можно менять ширину дерева файлов, размер консоли и её
положение снизу или справа; раскладка сохраняется между запусками. Редактор
защищает несохранённый текст и откажется перезаписывать файл, изменённый извне.

In the desktop composer, choose **📂 project** (or click the `REPO` value) to set
the folder the agent may edit. Attaching a project folder and sending a write task
in Agent or Goal mode selects that folder as the workspace; attached files are
passed as workspace-relative context. Files outside the selected workspace stay
read-only references.

Shortcuts: `ctrl+1…8` pages · `ctrl+n` new chat · `ctrl+,` settings · `ctrl+q` quit.
`install-shortcut.bat` puts a proper icon on the Desktop and in the Start Menu
(`install-shortcut.bat /remove` takes it back).

### The browser UI is a feature, not the product

The window renders telemetry straight from the local SQLite/JSONL sinks. The web
control room exists for the cases a browser genuinely wins — a second monitor, a
read-only view for a colleague, or a machine without Tk — and can be started from
the Integrations view or the CLI. The window shows the bound URL and opens it on request.

```bash
gigaforge web --serve --port 8787 --open   # same server, from a shell
web.bat          # control room only
```

---

## What runs under the window

### The task DAG

```
intake → index → slice → cache → plan → act → validate → fix → review
                                                              → settle
                                                              → summarize
                                                              → persist
```

Every node is a plain callable with declared inputs/outputs, so the executor can
checkpoint, resume, retry and time each step, and the UI can show a step timeline
that is always true.

`plan` and `review` are **policy** stages: the DAG owns their position, order and
trace, `gigaforge/core/strategies.py` owns what they do.

| stage | strategies | env |
|---|---|---|
| plan | `react` (bounded exploration, then a validated plan — **default**), `linear` (one call), `tot` (K branches, heuristic scoring, optional judge) | `GIGAFORGE_PLAN_STRATEGY` |
| review | `constitutional` (deterministic blockers — **default**), `llm`, `none` | `GIGAFORGE_REVIEW_STRATEGY` |

A reviewer can only ever *narrow* an acceptance: `settle` combines the
deterministic verdict with the review veto and never revives a rejected diff.

Every plan — from either entry point — passes `harness/plan_guard.py` before the
DAG sees it: unknown tools, untargeted edits, duplicate steps and an edit with no
verification are dropped or repaired deterministically, with no model call. If
the guard empties a plan, the episode's own observations are salvaged into one
rather than reporting zero steps.

### The harness

LangGraph is the bridge, not the harness. `gigaforge/harness/` is the layer that
decides how a run is allowed to continue:

| module | what it decides |
|---|---|
| `compaction.py` | at 85% of the window: trim stale file bodies first, then compact whole messages. The guard **restores** a run instead of killing it. |
| `ledger.py` | real token counts per phase, from the server counter when available |
| `policy.py` | retry classification — 429 / 5xx / 400 are not the same failure |
| `plan_guard.py` | deterministic plan validation (above) |
| `live_probe.py` | the six live assertions, runnable against the real API |

### Agents — 14, all reachable

`planner`, `coder`, `reviewer` (the core trio) · `tot`, `constitutional`, `react`,
`reflection`, `self_consistency`, `self_debug` (reasoning) · `localizer`,
`retrieval`, `summarizer`, `memory` (context) · `subagents` (delegation).

```bash
gigaforge agents          # catalog: name, family, stage, role
```

### Providers

GigaChat is the default. `GIGAFORGE_PROVIDER=auto` (the default) uses the
official `gigachat` SDK when it is installed and a credential is present, and
falls back to the in-repo client otherwise. An OpenAI-compatible endpoint is a
drop-in selection:

```bash
GIGAFORGE_PROVIDER=openai
GIGAFORGE_OPENAI_BASE_URL=https://api.openai.com/v1
GIGAFORGE_OPENAI_API_KEY=...
```

```bash
gigaforge providers       # what is registered, what is configured
```

**Structured output.** The live API accepts `response_format` and ignores it.
The mechanism it enforces is `functions` + `function_call`, and the answer comes
back in `function_call.arguments` with an empty `content`. Both providers
translate to that form, so a schema is honoured in the default configuration
rather than only when the SDK transport happens to be selected.

The async transports (HTTPX, SSE) sit under one blocking bridge,
`core/sync_adapter.SyncChatClient`, so the synchronous DAG never touches an event
loop and the connection pool survives a whole run.

### MCP

The harness is also an MCP server, so an external IDE can call its tools:

```bash
gigaforge mcp            # stdio transport
```

---

## CLI

```bash
gigaforge desktop                  # the window (primary)
gigaforge run "<goal>"             # one run, headless
gigaforge goal "<goal>"            # execute, verify, and improve until done
gigaforge goal --resume <goal-id>  # resume saved goal progress
gigaforge chat [-m MSG]            # agentic chat in the terminal
gigaforge ui --watch               # terminal dashboard
gigaforge web [--serve|--out F]    # browser control room
gigaforge report|trace|steps|diff|cost|cache|paths
gigaforge index|doctor|models|smoke|mcp|bench|providers|agents
```

Exit codes are part of the contract: `0` ok · `1` run not accepted · `2` no data ·
`3` usage/configuration error. `--json` bypasses `rich` so it stays pipe-safe.

`run` is the bounded one-shot authoring flow. `goal` is the long-lived task
flow: it stores progress below `.gigaforge/goals`, runs relevant checks after
each accepted change, feeds failures back into the next model cycle, and stops
only after checks pass or the cycle/verifier limit is reached. Goal mode copies
the selected project into `.gigaforge/workspaces/<goal-id>/project`, writes and
tests there, then publishes only verified changed files with atomic file
replacement. If the
destination changed while the goal was running, publication pauses with a
conflict report and keeps the workspace for resume. Successful runs remove the
large project copy while retaining the goal report. LangGraph and its SQLite
checkpointers are required base dependencies for Goal, ReAct, chat, and
sub-agents. The Windows launcher and runtime verify/install missing declared
dependencies at startup and stop with an actionable error if setup fails;
there is no legacy-engine fallback. Playwright is also a core dependency. The
application checks Chromium on startup and provisions its browser binary
automatically if it is missing:

```bash
gigaforge goal "Build a game ..." --max-cycles 4 --json
```

Use `--static-only` only when accepting syntax/structure checks without actually
launching an HTML application is intended. Add `--subagents` to delegate each
implementation cycle while retaining the same final verification gate. The
complete workflow and status meanings are in [`docs/GOAL_MODE.md`](docs/GOAL_MODE.md).
Desktop **Settings → file changes** lets you choose a persisted **Ask** mode
(one approval per complete diff) or **Always allow** mode.

Windows shortcuts: `start.bat` (window) · `menu.bat` (terminal menu) ·
`web.bat` · `tui.bat` · `chat.bat` · `doctor.bat` · `panel.bat`.

---

## Architecture

| layer | package | responsibility |
|---|---|---|
| M1 | `gigaforge/client` | OAuth, SSE streaming, retries, rate limiting, metrics |
| M2 | `gigaforge/l0` | symbol index, AST tools, repo map, incremental indexing, lint |
| M3 | `gigaforge/firewall` | context compression, context slicing, diff validation |
| M4–M5 | `gigaforge/core` | DAG executor, router, cache, fix loop, breaker, strategies, telemetry |
| M6 | `gigaforge/runtime.py` | the one composition root |
| agents | `gigaforge/agents` | the 14 reasoning units |
| providers | `gigaforge/providers` | provider protocol + registry (GigaChat, OpenAI-compatible) |
| MCP | `gigaforge/mcp` | stdio MCP server |
| UI | `gigaforge/ui` | store → analytics → snapshot → renderers (TUI, HTML, chat) |
| **app** | `gigaforge/desktop` | the window: shell, views, widgets, worker |
| web | `gigaforge/ui/webui.py` | stdlib control room, snapshot API and optional chat bridge |

One snapshot, one truth: the desktop window, the HTML panel, the TUI and
`gigaforge report` all render `ui.snapshot.build_snapshot`, so they cannot disagree.

### Hard rules

- No LangChain / LiteLLM shims — the GigaChat contract is implemented directly.
- Never send a whole file to a model; only diffs and bounded context slices.
- Every provider request has a bounded timeout and retry policy; model calls also
  pass through the router's budget and circuit breaker.
- **No credential in the source.** Secrets come from the environment or `.env`; a
  missing key degrades to read-only instead of impersonating a demo account.
- One composition root (`gigaforge/runtime.py`): the CLI, the window and the chat
  backend build the same object graph, so a wiring bug cannot hide in one of them.

## Verifying a checkout

```bash
gigaforge doctor          # environment, credentials, deps, git, storage, budgets
gigaforge bench           # offline benchmark across toy tasks
gigaforge run "<goal>" --dry-run    # full DAG, nothing written to the workspace
```

### The panel's own gates

`gigaforge wcag <page>` measures one rendered page. The control room is four
views in one page and three of them are `display:none` until a tab is opened,
so a single `wcag` run checks Overview and silently skips Steps, Trace and
Diff. These two scripts close that gap, and CI runs both:

```bash
python scripts/_render_panel.py /tmp/panel.html        # populated snapshot -> HTML
python scripts/ui_design_audit.py --label ci           # all 4 views x 1440/375/320
python scripts/ui_state_check.py /tmp/panel.html       # tablist, focus ring, aria-busy
```

`ui_design_audit.py` fails on any text pair below AA and writes a screenshot
per view, so a before/after pair is reproducible instead of anecdotal.
`ui_state_check.py` checks what a colour probe cannot see: that the tab strip
is a real ARIA tablist, that arrow keys move between tabs, that a focus ring is
painted, and that the page logs no console error.

The QML surface has its own gate, because it is the primary one:

```bash
python -m pytest tests/test_qml_tokens.py    # every token, all three themes
```

### Scripts

Every script in `scripts/` is either wired to CI or documented here. Three that
were neither have been removed: `build_proto.py` (its input directory `原型/`
does not exist in the repository), `render_check.py` (a stale copy of
`gigaforge/harness/render_check.py`, which the pipeline imports) and
`probe_summary.py` (an 11-line reader for a report nothing writes).

| Script | What it does | Wired to |
|---|---|---|
| `_render_panel.py` | writes the control-room panel to a file from a populated snapshot | CI (via the two audits) |
| `ui_design_audit.py` | all four panel views at 1440/375/320, fails on any pair below AA | CI |
| `ui_state_check.py` | tablist semantics, arrow keys, focus ring, aria-busy, console errors | CI |
| `contrast_table.py` | prints the contrast table for all three surfaces; `--markdown` feeds `qa/REPORT.md` | `tests/test_report_numbers.py` |
| `qml_render_audit.py` | renders Main.qml offscreen and reports the field colour actually painted | manual (needs PySide6) |
| `probe_p0.py` | executable probes for the P0 findings, driving the real pipeline | manual |
| `probe_gradability.py` | whether a SWE-bench instance can be graded in this sandbox | manual |
| `dump_swe_diff.py` | runs one SWE-bench instance and dumps the model's diff verbatim | manual |
| `make_icon.py` | regenerates `assets/gigaforge.ico` from the palette | manual |
| `generate_watercolor_wash.py` | regenerates the QML watercolor texture | manual |

The manual ones are diagnostics: they need a model credential, a clone, or Qt,
so they cannot run in CI. They are listed here so they are findable rather than
being 466 lines nobody knows about.

## Docs

- [`docs/DESKTOP.md`](docs/DESKTOP.md) — the window: structure, threading, theming
- [`docs/SYNTHESIS.md`](docs/SYNTHESIS.md) — what the merge brought in and how it wired
- [`docs/V2_BACKEND_AUDIT.md`](docs/V2_BACKEND_AUDIT.md) — backend audit
- [`docs/UI_V2.md`](docs/UI_V2.md) — the snapshot/UI layer
- [`docs/ARCHITECTURE_REVIEW.md`](docs/ARCHITECTURE_REVIEW.md) — current architecture audit and roadmap

## License

Proprietary — internal use.
