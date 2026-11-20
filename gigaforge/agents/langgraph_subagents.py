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
* **Isolation is unchanged.**  Each child still receives only its own
  assignment and still runs in its own worktree; the graph only decides *when*
  it runs.

**Two runner shapes, one entry point.**  The project has two sub-agent runners
and they take different task types:

* :class:`gigaforge.core.agents.SubAgentRunner` — the production runner.  Its
  ``spawn_one(spec, parent_ctx=…)`` owns worktree creation, context isolation
  and cleanup, and it takes a :class:`~gigaforge.core.contracts.SubAgentSpec`.
* :class:`gigaforge.agents.subagents.SubAgentRunner` — the A2A runner.  Its
  ``run_one(spawn, factory)`` takes a ``SubAgentSpawn``.

This module used to assume the second one unconditionally.  Measured with the
default ``GIGAFORGE_SUBAGENT_ENGINE=langgraph``: every fan-out raised
``'SubAgentSpec' object has no attribute 'payload'``, ``_subagent_diff``'s
``except Exception`` swallowed it, and ``--subagents`` ran with no sub-agents
while reporting a normal run.  The graph now normalises whatever it is handed
and dispatches to whichever runner it was given.

**The return type is the production type.**  The graph used to return plain
dicts while the classic path returned :class:`SubAgentResult` objects, so a
caller doing ``getattr(item, "diff", "")`` — which is what ``_subagent_diff``
does — saw an empty diff and ``ok=False`` for every child even when the fan-out
had worked.  Both engines now return ``list[SubAgentResult]``.
"""
from __future__ import annotations

from typing import Annotated, Any, Callable, Mapping, Sequence, TypedDict

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
    spec: dict[str, Any]


def normalise_spawn(item: Any) -> dict[str, Any]:
    """Flatten either task type into one JSON-safe dict.

    Accepts a :class:`~gigaforge.core.contracts.SubAgentSpec` (the production
    type), a :class:`~gigaforge.agents.subagents.SubAgentSpawn` (the A2A type),
    or an already-flat dict.  Every field the graph needs is present in the
    result, so a node never has to guess which type it was handed.
    """
    if isinstance(item, Mapping):
        data = dict(item)
    else:
        data = {}
        for field in (
            "name", "goal", "phase", "focus_paths", "symbols", "budget_tokens",
            "use_cache", "dry_run", "max_fix_iterations", "payload", "worktree",
            "resource_files", "metadata",
        ):
            if hasattr(item, field):
                data[field] = getattr(item, field)
    # ``payload`` is the A2A carrier for the assignment; a spec carries it as
    # ``goal``.  Normalising here means the child node has one shape to read.
    payload = data.get("payload")
    if not isinstance(payload, Mapping):
        payload = {}
    payload = dict(payload)
    if data.get("goal") and "goal" not in payload:
        payload["goal"] = str(data.get("goal"))
    if data.get("name") and "name" not in payload:
        payload["name"] = str(data.get("name"))
    focus = data.get("focus_paths") or ()
    if focus and "focus_paths" not in payload:
        payload["focus_paths"] = list(focus)
    return {
        "name": str(data.get("name") or payload.get("name") or "agent"),
        "goal": str(data.get("goal") or payload.get("goal") or ""),
        "phase": str(data.get("phase") or "code"),
        "focus_paths": [str(p) for p in (focus or ())],
        "symbols": [str(s) for s in (data.get("symbols") or ())],
        "budget_tokens": int(data.get("budget_tokens") or 0),
        "use_cache": bool(data.get("use_cache", True)),
        "dry_run": bool(data.get("dry_run", False)),
        "max_fix_iterations": data.get("max_fix_iterations"),
        "payload": payload,
        "worktree": str(data.get("worktree") or ""),
        "resource_files": [str(p) for p in (data.get("resource_files") or ())],
    }


def _spec_from(flat: Mapping[str, Any]) -> Any:
    """Rebuild the production task type from a flattened spawn."""
    from gigaforge.core.contracts import SubAgentSpec

    return SubAgentSpec(
        name=str(flat.get("name") or "agent"),
        goal=str(flat.get("goal") or ""),
        phase=str(flat.get("phase") or "code"),
        focus_paths=tuple(flat.get("focus_paths") or ()),
        symbols=tuple(flat.get("symbols") or ()),
        budget_tokens=int(flat.get("budget_tokens") or 0),
        use_cache=bool(flat.get("use_cache", True)),
        dry_run=bool(flat.get("dry_run", False)),
        max_fix_iterations=flat.get("max_fix_iterations"),
    )


def _spawn_from(flat: Mapping[str, Any]) -> SubAgentSpawn:
    """Rebuild the A2A task type from a flattened spawn."""
    from pathlib import Path

    return SubAgentSpawn(
        payload=dict(flat.get("payload") or {}),
        budget_tokens=int(flat.get("budget_tokens") or 0),
        worktree=Path(str(flat.get("worktree") or ".")),
        resource_files=list(flat.get("resource_files") or []),
    )


def build_subagent_graph(
    runner: SubAgentRunner,
    agent_factory: Callable[[dict[str, Any], Any, int], Any],
    *,
    checkpointer: Any = None,
    parent_ctx: Any = None,
) -> Any:
    """Compile the fan-out graph.

    ``agent_factory`` is the same callable ``SubAgentRunner`` takes — the graph
    does not know which agent runs, only how many and in what order.

    ``parent_ctx`` is held in the closure rather than in the graph state: a
    ``RunContext`` is not serialisable, and putting it in state would break the
    checkpointer for every branch.  The graph is compiled per call, so a closure
    is the correct place for it.
    """
    from langgraph.graph import END, START, StateGraph

    def fan_out(state: SubAgentGraphState) -> list:
        """One branch per spawn, carrying only that spawn's assignment."""
        sends: list = []
        for index, spawn in enumerate(state.get("spawns") or []):
            sends.append(
                Send(
                    "child",
                    {
                        "index": index,
                        "spec": dict(spawn),
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
        from gigaforge.core.sync_adapter import await_blocking

        flat = dict(state.get("spec") or {})
        index = int(state.get("index") or 0)
        if hasattr(runner, "spawn_one"):
            # Production runner: it owns the worktree, so it takes the spec.
            result = runner.spawn_one(
                _spec_from(flat), parent_ctx=parent_ctx, index=index
            )
            entry = _carry(result)
        else:
            result = await_blocking(runner.run_one(_spawn_from(flat), agent_factory))
            entry = dict(result)
        entry["spawn_index"] = index
        return {"results": [entry]}

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


def _carry(result: Any) -> dict[str, Any]:
    """Flatten a result for graph state **without dropping the diff**.

    ``SubAgentResult.to_dict()`` is a telemetry view: it reports ``diff_chars``
    and omits the diff itself, which is right for a ledger and wrong for a
    graph whose whole job is to hand the diff to the merge.  Measured: the
    fan-out ran, both children returned ``ok: True``, and the merge reported
    "child produced no diff" for both — the diff had been dropped at the
    serialisation boundary, and the run silently fell back to a single agent.

    The graph carries the payload it is responsible for.  ``to_dict()`` is
    still used for everything else, so the ledger stays small.
    """
    if isinstance(result, Mapping):
        return dict(result)
    data: dict[str, Any] = {}
    for field in (
        "name", "spawn_index", "ok", "diff", "summary", "usage", "worktree",
        "branch", "validate", "error", "metrics",
    ):
        if hasattr(result, field):
            data[field] = getattr(result, field)
    return data


def _to_result(entry: Mapping[str, Any], *, as_object: bool) -> Any:
    """Rebuild a result in the runner's own type.

    The graph must return **what the runner returns**, in spawn order.  The
    production runner returns :class:`SubAgentResult` and its only caller reads
    ``item.diff`` / ``item.ok``; the A2A runner returns dicts and its callers
    index them.  Returning one type for both broke whichever caller did not
    match — measured: the production path saw ``ok=False`` for every child
    because a dict has no ``.diff``, and the A2A path raised
    ``'SubAgentResult' object is not subscriptable``.

    ``as_object`` is decided by which runner was handed in, not by the data.
    """
    if not as_object:
        return dict(entry)
    from gigaforge.core.contracts import SubAgentResult

    known = {
        "name", "spawn_index", "ok", "diff", "summary", "usage", "worktree",
        "branch", "validate", "error", "metrics",
    }
    data = {k: v for k, v in dict(entry).items() if k in known}
    data.setdefault("name", "agent")
    data.setdefault("spawn_index", 0)
    data.setdefault("ok", False)
    try:
        return SubAgentResult(**data)
    except TypeError:  # pragma: no cover - a future field must not break a run
        return SubAgentResult(
            name=str(data.get("name") or "agent"),
            spawn_index=int(data.get("spawn_index") or 0),
            ok=bool(data.get("ok")),
            diff=str(data.get("diff") or ""),
            error=str(data.get("error") or ""),
        )


def run_subagents_graph(
    runner: SubAgentRunner,
    spawns: Sequence[Any],
    agent_factory: Callable[[dict[str, Any], Any, int], Any],
    *,
    checkpointer: Any = None,
    thread_id: str = "subagents",
    parent_ctx: Any = None,
) -> list[Any]:
    """Run every spawn and return results in spawn order.

    Returns ``list[SubAgentResult]`` — the same type the classic path returns,
    so the engine dispatcher is transparent to its caller.
    """
    from gigaforge.core.langgraph_runtime import thread_config

    if not spawns:
        return []
    graph = build_subagent_graph(
        runner, agent_factory, checkpointer=checkpointer, parent_ctx=parent_ctx
    )
    config = thread_config(thread_id)
    initial: SubAgentGraphState = {
        "spawns": [normalise_spawn(spawn) for spawn in spawns],
        "results": [],
    }
    for _chunk in graph.stream(initial, config, stream_mode="updates"):
        pass
    values: Mapping[str, Any] = graph.get_state(config).values or {}
    # The production runner returns objects; the A2A runner returns dicts.
    # Returning one type for both broke whichever caller did not match.
    as_object = hasattr(runner, "spawn_one")
    return [
        _to_result(entry, as_object=as_object)
        for entry in (values.get("results") or [])
    ]
