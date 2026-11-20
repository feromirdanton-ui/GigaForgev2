# Synthesis: what the merge brought in, and what it took to make it run

> Historical merge and verification record. Its test-suite and Nexus references
> describe earlier revisions; the current architecture and cleanup status are in
> [`ARCHITECTURE_REVIEW.md`](ARCHITECTURE_REVIEW.md).

Three GigaForge lineages were merged:

| lineage | what it was good at | what it lacked |
|---|---|---|
| `GigaForge-main` | the V2 core: shared SQLite, wave DAG, micro-USD cost, the UI layer | agents beyond the core trio, MCP, multi-provider, and a broken composition root |
| `GigaForge-old-optimised` | 15 agent types, MCP server, providers, a large test suite | no web UI, a linear V1 core, SQLite lock contention |
| `Gigaforge-old-v2` | dogfooding logs | not runnable |

The strategy was: **keep main's core and take the missing capabilities from
old-optimised**, then wire them properly instead of copying directories.

---

## 1. What was already there, and dead

`gigaforge/mcp/`, `gigaforge/providers/`, `gigaforge/agents/tot.py` and
`gigaforge/agents/constitutional.py` had been copied into main — and nothing used
them:

- `providers.attach_giga_client()` was documented as "called by `build_runtime()`";
  `build_runtime()` could not run at all.
- `agents/tot.py` and `agents/constitutional.py` were written against the *old*
  router contract (`router.route("plan") -> str`, `client.chat(...)`) and the old
  `PlanStep`/`BudgetTracker` shapes, so they raised against the V2 core.
- `agents/__init__.py` exported three of the fourteen agents, so the rest were
  invisible to every front end.

## 2. The missing piece: one bridge, one root

The V2 core is synchronous (DAG nodes are plain callables); every transport is async.
Nothing bridged the two, and the two attempts at it were stubs:

- `ModelRouter` called `client.complete(...)`, which no transport implemented;
- `GigaClientAdapter.complete` forwarded to that same non-existent method.

`gigaforge/core/sync_adapter.SyncChatClient` is that bridge: a blocking `complete()`
over any `Provider`, driven by **one long-lived event loop in a daemon thread** so
the HTTP connection pool survives a whole run (dozens of calls) and the adapter is
safe to call from a thread that already has a loop.

`gigaforge/runtime.py` is now the only constructor of the object graph:

```
Settings ──► ClientConfig ──► TokenProvider ──► GigaClient (async)
                                              │
                              metrics sink ◄──┘
                                              ▼
                                     Provider registry ──► SyncChatClient
                                                                 │
L0Config ──► L0Store ──► Indexer ──► Git             TaskPipeline ◄┘
                └─────► ContextFirewall (slice + validate)
                                     │
                            UiStore ──► snapshot ──► desktop window / web / TUI / CLI
```

`build_runtime(require_provider=False)` builds everything **except** the client, which
is what lets the desktop window open on a machine with no credential.

## 3. ToT and Constitutional, as DAG stages

The valuable algorithms were kept; the plumbing was replaced with the DAG's own.

`core/strategies.py`:

- `LinearPlan` — one call, the previous behaviour.
- `TreeOfThoughtPlan` — K candidates at a temperature ladder, each parsed and scored
  by a **zero-token** heuristic (`score_steps`: specificity, verification, length
  fit, novelty), with an optional cheap-model judge that only runs when the top two
  are within `judge_margin`.
- `NoReview` — accept; the validator stays the only gate (default).
- `ConstitutionalReview` — the principle set from `agents/constitutional`:
  deterministic blockers run as pure functions over the diff (zero tokens), then one
  cheap call judges the soft principles. A parse failure is a **rejection**.
- `LLMReview` — "does this diff actually solve the goal?".

Every strategy talks to the model through `ask(phase, prompt, temperature=None)`,
supplied by the pipeline. A strategy therefore **cannot** bypass the router, the
budget tracker, the circuit breaker or the trace — the properties the DAG exists to
guarantee.

The DAG gained one node:

```
… validate → fix → review → settle → summarize …
```

- `review` requires the winning diff (`fixed_diff` or the proposal) and provides
  `review_verdict`; it is `critical=False`, so a broken reviewer degrades to
  "no veto" rather than failing a run.
- `settle` applies `accepted = validated and not review_rejected`. A reviewer can
  narrow an acceptance; it can never revive a diff the deterministic gate rejected.
- A cache hit short-circuits review (the verdict was stored with the diff).

Selection: constructor argument, or the environment —
`GIGAFORGE_PLAN_STRATEGY=linear|tot`, `GIGAFORGE_REVIEW_STRATEGY=none|constitutional|llm`,
`GIGAFORGE_TOT_MAX_BRANCHES`, `GIGAFORGE_TOT_JUDGE`, `GIGAFORGE_CONSTITUTION_LLM`.

## 4. Providers, finally live

- `build_provider()` attaches the live `GigaClient` to the registry and returns the
  env-selected provider (`GIGAFORGE_PROVIDER`), falling back to GigaChat with a
  warning rather than failing the process.
- The router, the DAG and the agents only ever see `SyncChatClient`, so GigaChat and
  an OpenAI-compatible endpoint are interchangeable.
- `gigaforge providers` and the System page report both the live selection and every
  registered provider with its capabilities.

## 5. MCP

`gigaforge/mcp/` is a real stdio MCP server (`gigaforge mcp`): tools are declared in
`mcp/tools.py` and dispatched in `mcp/server.py`, with JSON-RPC framing, notification
handling and `isError` results. Wiring it cost nothing beyond keeping it exported.

## 6. Defects found while wiring (and fixed)

| symptom | cause |
|---|---|
| every run failed before node one | `TokenProvider(settings=…)`; CLI built a client with no token auth |
| `'GigaClient' object has no attribute 'complete'` | the router's contract had no implementation |
| `SQLite objects created in a thread can only be used in that same thread` | L0 connections were thread-affine while the DAG runs nodes on workers |
| every non-empty context slice raised `TypeError` | `slicer.py` called `estimate_tokens(chars, chars_per_token)` on the one-argument helper |
| second caller of an unchanged repo saw an empty ranking | the repo-map cache stored the text but not the `ranked` list |
| "the diff did not apply cleanly" for good diffs | `FirewallConfig.repo_root` stayed `"."` |
| runs table had no goal and every run was "not accepted" | `runs.input` never written; `accepted` had no source |
| per-run KPIs summed every run ever | router/DAG mutable metrics carried no `run_id` |
| a demo credential was compiled into `nexus/server.py` | it was never needed; removed |
| a vague goal dead-ended the slice node | `EmptySliceError` with no fallback and no cache key |

## 7. What was deliberately removed

**The dead V1 pipeline.** `core/agent_pipeline.py` was a second, async
`Pipeline` that referenced `PlannerAgent` and friends it never imported, so every
call raised `NameError` — the exact failure the benchmark reported. It was
replaced by `Pipeline = TaskPipeline` (an alias, not a second implementation), and
`gigaforge bench` was ported onto the real DAG so the benchmark measures the
product instead of a parallel path.

**The shared test suite** (≈670 cases) came from the old-optimised lineage and
tested its V1 API (`build(…, index)`, `TaskState.add_learning`,
`TokenProvider(settings=…)`, `RateLimiter.acquire(...)`), not the V2 surface that
replaced it. Keeping it would have meant either freezing the V2 refactor or
maintaining a second, test-only API.

Verification now runs the product instead of a parallel code path:

```bash
gigaforge doctor                     # environment / config / deps / storage / budgets
gigaforge bench                      # offline benchmark over toy tasks
gigaforge run "<goal>" --dry-run     # the full DAG, nothing written to the workspace
```

Current state of those checks on this checkout:

| check | result |
|---|---|
| `python -m compileall` over every module | 0 failures |
| import of all 150 modules | 0 failures |
| `gigaforge bench` (offline, 4 toy tasks) | 4/4 accepted, 4/4 tests pass after re-applying the diff, 12 model calls, 1.7 s |
| desktop window | opens with **no credential** (read-only), 7 pages render, browser panel serves |

