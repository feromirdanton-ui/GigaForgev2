"""Engine dispatch for the agent layer — one entry point, two implementations.

Level 2 of the merge plan has two engines per agent:

* the hand-written loop (``agents/react.py``, ``core/agents.py``), which works
  on a bare install and is the default;
* the LangGraph graph (``agents/langgraph_react.py``,
  ``agents/langgraph_subagents.py``), which adds checkpointing, ``interrupt()``
  and event streaming.

Before this module the graphs existed but nothing in the product called them:
``run_react_graph`` and ``run_subagents_graph`` were reachable only from tests,
so the whole LangGraph layer was dead weight.  This module is the single place
that decides which engine runs, so a caller never has to know.

Selection is by environment variable, with the same shape as the goal engine:

``GIGAFORGE_REACT_ENGINE``
    ``classic`` (default when LangGraph is absent) or ``langgraph``.

``GIGAFORGE_SUBAGENT_ENGINE``
    ``classic`` or ``langgraph``.

Both default to ``langgraph`` when the package is installed, and degrade to
``classic`` with a logged warning when it is missing.  (This docstring used to
claim both defaulted to ``classic``, which was the opposite of what
:func:`_choice` does — a comment that contradicts the code is how a reader
concludes a feature is off when it is on.)
"""
from __future__ import annotations

import logging
import os
from typing import Any, Callable, Optional, Sequence

__all__ = [
    "react_engine",
    "subagent_engine",
    "run_react",
    "run_subagents",
]

log = logging.getLogger(__name__)


def _choice(env: str, default: str = "langgraph") -> str:
    value = (os.environ.get(env) or "").strip().lower()
    if value in {"langgraph", "graph"}:
        return "langgraph"
    if value in {"classic", "legacy", "loop"}:
        return "classic"
    return default


def _langgraph_ok() -> bool:
    try:
        from gigaforge.core.langgraph_runtime import langgraph_available

        return bool(langgraph_available())
    except Exception:  # noqa: BLE001 - a missing optional dep means classic
        return False


def react_engine() -> str:
    """Which ReAct engine to use: ``"langgraph"`` or ``"classic"``.

    ``langgraph`` is the default when the package is installed: the graph adds
    checkpointing, ``interrupt()`` and event streaming, and the classic loop is
    kept only as the bare-install fallback.  A deployment that wants the old
    loop sets ``GIGAFORGE_REACT_ENGINE=classic``.
    """
    wanted = _choice("GIGAFORGE_REACT_ENGINE")
    if wanted == "langgraph" and not _langgraph_ok():
        log.warning("GIGAFORGE_REACT_ENGINE=langgraph but langgraph is not installed")
        return "classic"
    return wanted


def subagent_engine() -> str:
    """Which sub-agent engine to use: ``"langgraph"`` or ``"classic"``."""
    wanted = _choice("GIGAFORGE_SUBAGENT_ENGINE")
    if wanted == "langgraph" and not _langgraph_ok():
        log.warning("GIGAFORGE_SUBAGENT_ENGINE=langgraph but langgraph is not installed")
        return "classic"
    return wanted


def run_react(
    runner: Any,
    task: str,
    state: Any,
    *,
    ctx: Any,
    bootstrap_hint: Optional[str] = None,
    checkpointer: Any = None,
    thread_id: str = "",
    engine: Optional[str] = None,
) -> Any:
    """Run one ReAct episode on the selected engine.

    Both engines return the same :class:`~gigaforge.agents.react.ReActResult`,
    so the caller's code is identical either way.
    """
    chosen = (engine or react_engine()).strip().lower()
    if chosen in {"langgraph", "graph"} and _langgraph_ok():
        from gigaforge.agents.langgraph_react import run_react_graph

        return run_react_graph(
            runner,
            task,
            state,
            ctx=ctx,
            bootstrap_hint=bootstrap_hint,
            checkpointer=checkpointer,
            thread_id=thread_id,
        )
    from gigaforge.core.sync_adapter import await_blocking

    return await_blocking(
        runner.run(task, state, ctx=ctx, bootstrap_hint=bootstrap_hint)
    )


def run_subagents(
    runner: Any,
    spawns: Sequence[Any],
    agent_factory: Callable[[dict[str, Any], Any, int], Any],
    *,
    checkpointer: Any = None,
    thread_id: str = "subagents",
    engine: Optional[str] = None,
    **classic_kwargs: Any,
) -> list[Any]:
    """Run a sub-agent fan-out on the selected engine, in spawn order.

    Two runner shapes are accepted, because the project has two:

    * :class:`gigaforge.core.agents.SubAgentRunner` — the production runner,
      whose ``spawn(specs, parent_ctx=…, parallel=…)`` owns worktree creation,
      context isolation and cleanup.  This is the classic path.
    * :class:`gigaforge.agents.subagents.SubAgentRunner` — the A2A runner, whose
      ``run_parallel(spawns, factory)`` takes pre-built ``SubAgentSpawn``s.
      This is what the LangGraph fan-out graph drives.
    """
    chosen = (engine or subagent_engine()).strip().lower()
    if chosen in {"langgraph", "graph"} and _langgraph_ok():
        from gigaforge.agents.langgraph_subagents import run_subagents_graph

        # ``parent_ctx`` travels through: the production runner needs it to
        # build each child's isolated context, and the graph holds it in a
        # closure because a RunContext is not serialisable and would break the
        # checkpointer if it were put in graph state.
        return run_subagents_graph(
            runner,
            spawns,
            agent_factory,
            checkpointer=checkpointer,
            thread_id=thread_id,
            parent_ctx=classic_kwargs.get("parent_ctx"),
        )
    from gigaforge.core.sync_adapter import await_blocking

    if hasattr(runner, "spawn"):
        # Production runner: it owns the worktrees, so it takes the specs.
        return list(runner.spawn(list(spawns), **classic_kwargs))
    return await_blocking(runner.run_parallel(list(spawns), agent_factory))
