"""Sub-agents on LangGraph — fan-out with ``Send()``, spawn-order aggregation.

``SubAgentRunner`` (``agents/subagents.py``) already enforces the four A2A
isolation rules and aggregates in spawn order.  What it does *not* have is a
scheduler: it builds ``asyncio`` tasks itself.  LangGraph's ``Send()`` is that
scheduler, and it brings checkpointing and per-branch streaming with it.

Design notes that matter:

* **One node per spawn.**  ``Send("child", {...})`` dispatches each spawn as its
  own graph branch, so a slow child does not delay the others and each branch
  is checkpointed separately.
* **Spawn order is restored explicitly.**  Branches finish in any order; the
  results carry their ``index`` and the collector sorts on it.  Completion
  order must never leak into the output — that is what makes a run
  reproducible.
* **Isolation is unchanged.**  Each child still receives only its ``payload``
  and still runs in its own worktree; the graph only decides *when* it runs.
"""
from __future__ import annotations

import asyncio
from typing import Annotated, Any, Callable, Mapping, Optional, Sequence, TypedDict

from .subagents import SubAgentRunner, SubAgentSpawn

try:  # pragma: no cover - optional dependency
    from langgraph.types import Send
except Exception:  # noqa: BLE001
    Send = None  # type: ignore[assignment]

__all__ = ["SubAgentGraphState", "build_subagent_graph", "run_subagents_graph"]


def _add(left: list, right: list) -> list:
    return list(left or []) + list(right or [])


def _merge_results(left: list, right: list) -> list:
    """Merge child results by ``spawn_index``.

    ``collect`` re-emits the whole ordered list, so a plain append would double
    every result.  Merging by index makes the collector idempotent.
    """
    merged = [dict(item) for item in (left or [])]
    for item in right or []:
        entry = dict(item)
        index = entry.get("spawn_index")
        for position, existing in enumerate(merged):
            if index is not None and existing.get("spawn_index") == index:
                merged[position] = {**existing, **entry}
                break
        else:
            merged.append(entry)
    return merged


class SubAgentGraphState(TypedDict, total=False):
    """Spawns in, results out.  Both are JSON-safe."""

    spawns: list[dict[str, Any]]
    results: Annotated[list[dict[str, Any]], _merge_results]
    index: int
    payload: dict[str, Any]
    budget_tokens: int
    worktree: str
    resource_files: list[str]


def build_subagent_graph(
    runner: SubAgentRunner,
    agent_factory: Callable[[dict[str, Any], Any, int], Any],
    *,
    checkpointer: Any = None,
) -> Any:
    """Compile the fan-out graph.

    ``agent_factory`` is the same callable ``SubAgentRunner`` takes — the graph
    does not know which agent runs, only how many and in what order.
    """
    from langgraph.graph import END, START, StateGraph

    def fan_out(state: SubAgentGraphState) -> list:
        """One branch per spawn, carrying only that spawn's payload."""
        sends: list = []
        for index, spawn in enumerate(state.get("spawns") or []):
            sends.append(
                Send(
                    "child",
                    {
                        "index": index,
                        "payload": dict(spawn.get("payload") or {}),
                        "budget_tokens": int(spawn.get("budget_tokens") or 0),
                        "worktree": str(spawn.get("worktree") or ""),
                        "resource_files": list(spawn.get("resource_files") or []),
                    },
                )
            )
        return sends

    def child(state: SubAgentGraphState) -> dict[str, Any]:
        """Run one sub-agent through the runner's isolation rules."""
        from pathlib import Path

        spawn = SubAgentSpawn(
            payload=dict(state.get("payload") or {}),
            budget_tokens=int(state.get("budget_tokens") or 0),
            worktree=Path(str(state.get("worktree") or ".")),
            resource_files=list(state.get("resource_files") or []),
        )
        result = asyncio.run(runner.run_one(spawn, agent_factory))
        return {"results": [{**dict(result), "spawn_index": int(state.get("index") or 0)}]}

    def collect(state: SubAgentGraphState) -> dict[str, Any]:
        """Sort by spawn index — completion order must not leak out."""
        ordered = sorted(
            state.get("results") or [], key=lambda item: int(item.get("spawn_index") or 0)
        )
        return {"results": ordered}

    graph = StateGraph(SubAgentGraphState)
    graph.add_node("child", child)
    graph.add_node("collect", collect)
    graph.add_conditional_edges(START, fan_out, ["child"])
    graph.add_edge("child", "collect")
    graph.add_edge("collect", END)
    if checkpointer is None:
        from langgraph.checkpoint.memory import InMemorySaver

        checkpointer = InMemorySaver()
    return graph.compile(checkpointer=checkpointer)


def run_subagents_graph(
    runner: SubAgentRunner,
    spawns: Sequence[SubAgentSpawn],
    agent_factory: Callable[[dict[str, Any], Any, int], Any],
    *,
    checkpointer: Any = None,
    thread_id: str = "subagents",
) -> list[dict[str, Any]]:
    """Run every spawn and return results in spawn order."""
    from gigaforge.core.langgraph_runtime import thread_config

    if not spawns:
        return []
    graph = build_subagent_graph(runner, agent_factory, checkpointer=checkpointer)
    config = thread_config(thread_id)
    initial: SubAgentGraphState = {
        "spawns": [
            {
                "payload": dict(spawn.payload),
                "budget_tokens": int(spawn.budget_tokens),
                "worktree": str(spawn.worktree),
                "resource_files": list(spawn.resource_files),
            }
            for spawn in spawns
        ],
        "results": [],
    }
    for _chunk in graph.stream(initial, config, stream_mode="updates"):
        pass
    values: Mapping[str, Any] = graph.get_state(config).values or {}
    return list(values.get("results") or [])
