# Agent layer: LangGraph + deepagents

This document records the second stage of the GigaChat merge: moving the
**agent runtime** onto LangGraph while keeping the **deterministic core** in
GigaForge. It is the reference for anyone touching `core/langgraph_*.py`,
`agents/langgraph_*.py` or the goal/chat entry points.

## The two levels

| Level | Owns | Modules | Runtime |
|---|---|---|---|
| **L1 — deterministic core** | contracts, slicing, context firewall, budgets, routing, benchmarking | `core/dag.py`, `core/contracts.py`, `l0/`, `firewall/`, `core/router.py`, `core/budgets.py`, `core/breaker.py`, `bench/` | plain Python, no framework |
| **L2 — agent runtime** | goal loop, chat, ReAct, sub-agent fan-out | `core/langgraph_goal.py`, `core/langgraph_runtime.py`, `agents/langgraph_react.py`, `agents/langgraph_subagents.py` | LangGraph `StateGraph` |

The rule that keeps the split honest: **L2 never re-implements an L1 concern.**
A graph node calls `pipeline.run(...)`; it does not build its own DAG, its own
budget check or its own diff firewall. If a node needs a capability, it calls
the L1 module that already has it.

## What moved, and what it replaced

### Goal mode — `core/langgraph_goal.py`

Replaces the `for` loop in `GoalRunner.run` with a four-node graph:

```
START → author → verify → approve → publish → END
           ↑         │        │
           └─ retry ─┴────────┘
```

* `author` — one implementation cycle; calls `pipeline.run(...)` unchanged.
* `verify` — the same `Verifier` the classic path uses.
* `approve` — **`interrupt()`**. The thread suspends, the caller shows the
  diff, `Command(resume=...)` carries the decision back into the same node.
  This replaces `ApprovalGate` and its callback plumbing.
* `publish` — atomic publish through `GoalWorkspace`, or a recorded refusal.

Checkpointing is a `SqliteSaver` at `.gigaforge/goals/<goal_id>.graph.sqlite3`,
so a goal resumes at the node it stopped on rather than at "the last cycle we
wrote to disk". `run_goal_graph` is signature-compatible with
`GoalRunner.run`, which is what lets `runtime.run_goal` switch engines without
touching a caller.

### ReAct — `agents/langgraph_react.py`

The loop becomes `reason → act → observe → reason`, with `finish` as the
terminal node. The runner's own collaborators (client, router, budgets, tracer,
registry, settings) and its own parsers are reused — only the control flow is
new. Two ported components are finally wired in:

* `ContextWindowGuard` runs **before** every model call, so a long trajectory
  is compacted proactively instead of after a 422.
* `LoopBreaker` runs **after** every observation. It reads a tool-shaped view
  rebuilt from the recorded steps (`_breaker_view`), because the wire
  trajectory is plain role/content while the breaker's signatures expect
  assistant `tool_calls` + `tool` results.
* `write_file` may create a new path directly, but replacing an existing file
  requires the complete `read_file` result to reach the model verbatim in the
  current run; otherwise use `edit_file`.
  After a full-file write, the agent must read the new version before replacing
  it again.

Every nudge is a **HumanMessage** (`LoopSignal.message()` →
`providers.messages.nudge`). GigaChat rejects a system message that is not
first with a hard 400, so a nudge appended as `role: "system"` would break the
next request.

### Sub-agents — `agents/langgraph_subagents.py`

`Send()` fans each spawn out as its own branch; `collect` sorts by
`spawn_index`. Completion order must never leak into the output — that is what
makes a run reproducible. Isolation is unchanged: each child still gets only
its payload and its own worktree.

## State reducers (the part that bites)

LangGraph merges node returns into state with a reducer per key. Three keys
needed a custom one, and getting them wrong is silent corruption rather than an
error:

| Key | Reducer | Why |
|---|---|---|
| `GoalState.cycles` | `_merge_cycles` | `verify` folds its report into the cycle `author` just appended; a plain append would duplicate the cycle |
| `ReActState.steps` | `_merge_steps` | `reason` writes the thought/action, `act` writes the observation for the *same* step |
| `SubAgentGraphState.results` | `_merge_results` | `collect` re-emits the whole ordered list; a plain append would double every result |

`ReActState.trajectory` uses plain append, so **nodes must return only the
delta** — returning the whole trajectory duplicates the history on every step.

## Migration risks and how they are handled

### 1. Double retry

Two retry layers would multiply: a 3-attempt provider retry inside a 3-attempt
graph retry is 9 requests for one logical call, and the budget accounting sees
only one.

**Owner: the provider.** `GigaChatSDKProvider` is constructed with
`max_retries=0` (`retries="gigaforge"`), and the graph nodes add **no**
`retry_policy`. `DagExecutor` retries only nodes marked `idempotent=True`, and
that is the only retry above the provider. If a graph node ever needs its own
retry, it must first disable the provider's.

### 2. `httpx` vs the SDK's `httpx2` pin

The official `gigachat` SDK pins its own HTTP stack. GigaForge's legacy client
uses `httpx>=0.27`. Installing both can resolve to two incompatible `httpx`
majors in one environment.

**Handling:** the SDK is an **optional extra** (`gigaforge[gigachat]`), never a
base dependency, and `GigaChatSDKProvider` is only imported when that extra is
installed. The legacy `httpx` client stays the default, so a base install has
exactly one HTTP stack. Do not add `gigachat` to the base dependencies.

## Required runtime

LangGraph and both SQLite checkpoint adapters are base dependencies, not extras.
`build_runtime()` verifies the imports and attempts a bounded installation when
the environment is incomplete; if installation fails, startup stops with an
actionable error. `start.bat` checks the same runtime before opening the desktop.

Goal, ReAct, chat Goal mode, and sub-agent entry points all require LangGraph.
They do not route to classic loops when an import, graph run, or engine
selection fails. `GIGAFORGE_*_ENGINE=classic` is rejected; use `langgraph` or
leave the setting unset. The project supports Python 3.11 and newer.

## Tests

`tests/test_merge_agent_layer.py` covers the seam: checkpoint durability,
goal retry/limit/blocked paths, `interrupt()` apply and reject, resume,
ReAct finish/max-steps/parse-repair/unknown-tool, the breaker's HumanMessage
rule, sub-agent spawn-order and crash isolation, and — importantly — that the
L1 modules still load and still enforce their contracts.
