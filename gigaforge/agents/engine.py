"""Dispatch every ReAct and sub-agent run through the required LangGraph engine."""
from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from typing import Any

__all__ = [
    "react_engine",
    "subagent_engine",
    "run_react",
    "run_subagents",
]


def _require_graph(env_name: str) -> None:
    choice = (os.environ.get(env_name) or "").strip().lower()
    if choice in {"classic", "legacy", "loop"}:
        raise RuntimeError(
            f"{env_name}={choice!r} is no longer supported; LangGraph is required"
        )
    if choice and choice not in {"langgraph", "graph"}:
        raise ValueError(f"{env_name} must be 'langgraph' when set, not {choice!r}")
    from gigaforge.core.langgraph_runtime import require_langgraph

    require_langgraph()


def react_engine() -> str:
    """Validate configuration and return the only supported ReAct engine."""
    _require_graph("GIGAFORGE_REACT_ENGINE")
    return "langgraph"


def subagent_engine() -> str:
    """Validate configuration and return the only supported sub-agent engine."""
    _require_graph("GIGAFORGE_SUBAGENT_ENGINE")
    return "langgraph"


def run_react(
    runner: Any,
    task: str,
    state: Any,
    *,
    ctx: Any,
    bootstrap_hint: str | None = None,
    checkpointer: Any = None,
    thread_id: str = "",
    engine: str | None = None,
) -> Any:
    """Run one ReAct episode on LangGraph; never route to the legacy loop."""
    configured = react_engine()
    chosen = (engine or configured).strip().lower()
    if chosen not in {"langgraph", "graph"}:
        raise RuntimeError(f"unsupported ReAct engine {chosen!r}; LangGraph is required")
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


def run_subagents(
    runner: Any,
    spawns: Sequence[Any],
    agent_factory: Callable[[dict[str, Any], Any, int], Any],
    *,
    checkpointer: Any = None,
    thread_id: str = "subagents",
    engine: str | None = None,
    **classic_kwargs: Any,
) -> list[Any]:
    """Run a LangGraph sub-agent fan-out in spawn order."""
    configured = subagent_engine()
    chosen = (engine or configured).strip().lower()
    if chosen not in {"langgraph", "graph"}:
        raise RuntimeError(f"unsupported sub-agent engine {chosen!r}; LangGraph is required")
    from gigaforge.agents.langgraph_subagents import run_subagents_graph

    return run_subagents_graph(
        runner,
        spawns,
        agent_factory,
        checkpointer=checkpointer,
        thread_id=thread_id,
        parent_ctx=classic_kwargs.get("parent_ctx"),
    )
