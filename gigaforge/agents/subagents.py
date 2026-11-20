"""SubAgentRunner — A2A discipline for parallel sub-agents.

The runner enforces the four A2A isolation rules (spec §11):

1. CONTEXT ISOLATION — each child receives ONLY its explicit ``payload``,
   never the parent's history. The runner passes only ``spawn.payload`` to
   the ``agent_factory``; the factory is responsible for not leaking parent
   state across the boundary.

2. RESULT ISOLATION — the parent receives ONLY the child's final structural
   JSON result (a ``dict``), never its intermediate scratchpad / chat history.
   The factory returns a dict; the runner hands it back unchanged.

3. RESOURCE ISOLATION — children touch disjoint files OR serialize through a
   per-file ``asyncio.Lock``. The pre-flight identifies shared files and
   routes both spawns through the lock for that file. Disjoint spawns run
   concurrently.

4. BUDGET ISOLATION — each child runs under its own ``budget_tokens`` cap.
   If the child raises :class:`BudgetExceeded` (the factory's responsibility
   to enforce), the runner catches it and returns
   ``{"error": "budget_exceeded", "killed": True}`` — the parent never sees
   a half-finished result.

AGGREGATION CONTRACT: :meth:`run_parallel` returns results in SPAWN ORDER
(not completion order). Each child is indexed by its position in the input
list; ``asyncio.gather`` preserves that order. A child that finishes faster
than its predecessor does NOT move up in the result list.

0 LLM calls in this module — the runner is pure asyncio orchestration. The
factory (provided by the pipeline, M4c) owns the LLM client and the budget
enforcement.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from gigaforge.client.errors import BudgetExceeded

__all__ = ["SubAgentSpawn", "SubAgentRunner", "AgentFactory"]

# A factory is a callable (payload, worktree, budget) -> Awaitable[dict].
# The pipeline provides this; the runner doesn't hardcode which agent runs.
AgentFactory = Callable[
    [dict[str, Any], Path, int],
    Awaitable[dict[str, Any]],
]

# Result returned when a child is killed for budget breach.
_KILLED_RESULT: dict[str, Any] = {"error": "budget_exceeded", "killed": True}

# Result returned when a child raises an unexpected Exception (LOW-16) — we
# never let one child's crash take down the whole ``asyncio.gather`` batch.
# The parent still receives a dict per spawn position; a crashed child is
# surfaced as a structural ``{"error": "...", "killed": True}`` so the parent
# can decide what to do (retry, skip, escalate) without losing the rest of
# the batch's results.
_CRASH_RESULT_TEMPLATE: dict[str, Any] = {"error": "child_crashed", "killed": True}


class SubAgentSpawn(BaseModel):
    """Description of a sub-agent to spawn.

    ``payload`` — the ONLY data the child receives (no parent history).
    ``budget_tokens`` — the per-child token cap. The factory enforces it
        (via :class:`BudgetTracker` or similar); the runner catches
        :class:`BudgetExceeded` and returns the killed result.
    ``worktree`` — the git worktree the child runs in (cwd isolation).
    ``resource_files`` — files the child will touch. Two children sharing a
        file are serialized through a per-file ``asyncio.Lock``; disjoint
        children run concurrently.
    """

    payload: dict[str, Any]
    budget_tokens: int
    worktree: Path
    resource_files: list[str] = Field(default_factory=list)


class SubAgentRunner:
    """Runs sub-agents in isolation, with serializing mediator + spawn-order
    aggregation.

    The runner is stateless across ``run_parallel`` invocations — each call
    builds its own per-file lock set and exits cleanly.
    """

    async def run_one(
        self,
        spawn: SubAgentSpawn,
        agent_factory: AgentFactory,
    ) -> dict[str, Any]:
        """Run a single child with ONLY its explicit payload.

        Returns the child's final JSON result dict. On :class:`BudgetExceeded`
        from the factory, returns ``_KILLED_RESULT``.

        LOW-16: on ANY other ``Exception`` from the factory, returns
        ``{"error": "child_crashed: <class: msg>", "killed": True}`` so a
        single misbehaving child cannot tear down the whole
        ``asyncio.gather`` batch in :meth:`run_parallel`. The parent still
        gets a dict at every spawn position and can decide how to handle the
        crash (retry, skip, escalate) — the alternative (let the exception
        propagate) would cancel every still-in-flight sibling.
        """
        try:
            result = await agent_factory(
                spawn.payload,
                spawn.worktree,
                spawn.budget_tokens,
            )
        except BudgetExceeded:
            # Per-child budget breach -> kill + return the killed result.
            # The parent never sees a half-finished child.
            return dict(_KILLED_RESULT)
        except Exception as exc:  # noqa: BLE001 — see docstring; isolates one child
            # One child crashed (programmer error, network blip, etc.) — surface
            # it structurally instead of letting ``gather`` cancel its siblings.
            # The class name + short message survive into the parent's view so
            # operators can see WHY the slot is empty without inspecting logs.
            msg = f"{exc.__class__.__name__}: {exc}"[:200]
            return {**_CRASH_RESULT_TEMPLATE, "error": f"child_crashed: {msg}"}
        # Result isolation: the parent receives ONLY the final dict.
        if not isinstance(result, dict):
            # Defensive: a misbehaving factory that returned non-dict gets
            # wrapped — the contract is dict-only across the A2A boundary.
            return {"result": result}
        return result

    async def run_parallel(
        self,
        spawns: list[SubAgentSpawn],
        agent_factory: AgentFactory,
    ) -> list[dict[str, Any]]:
        """Run all spawns concurrently subject to the resource-isolation rule.

        PRE-FLIGHT: build a per-file ``asyncio.Lock`` for every file mentioned
        in any spawn's ``resource_files``. Each child acquires ALL its file
        locks (in sorted order — deadlock-free) before running; releases them
        on exit. Children with disjoint resource sets run concurrently;
        children sharing a file serialize on that file's lock.

        AGGREGATION: results are returned in SPAWN ORDER (not completion
        order). ``asyncio.gather`` preserves input order; we additionally
        tag each result with its spawn index for clarity.
        """
        if not spawns:
            return []

        # Build per-file locks. One lock per unique file path string.
        file_locks: dict[str, asyncio.Lock] = {}
        for spawn in spawns:
            for f in spawn.resource_files:
                if f not in file_locks:
                    file_locks[f] = asyncio.Lock()

        async def _run_indexed(idx: int, spawn: SubAgentSpawn) -> dict[str, Any]:
            # Acquire all file locks for this spawn, in sorted order, to avoid
            # deadlock (any consistent total order works).
            sorted_files = sorted(spawn.resource_files)
            locks_to_hold = [file_locks[f] for f in sorted_files]
            async with AsyncExitStack() as stack:
                for lock in locks_to_hold:
                    await stack.enter_async_context(lock)
                result = await self.run_one(spawn, agent_factory)
            return result

        tasks = [
            asyncio.create_task(_run_indexed(i, spawn))
            for i, spawn in enumerate(spawns)
        ]
        # gather preserves input order — even if later tasks finish first.
        results = await asyncio.gather(*tasks)
        return list(results)
