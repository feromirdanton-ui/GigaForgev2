"""TaskState — the session is an OBJECT, NOT a message list.

Spec doctrine: "session-as-object, NOT message history".  The state carries the
goal, the current plan, a compact scratchpad, metrics, git context and a slice
hash.  It is NOT chat history — agents consult it to compose each LLM prompt
FRESH from the structured state, never replay messages.

**This module is now a re-export.**  It used to define a *second* ``TaskState``
and a *second* ``PlanStep``, incompatible with the ones in
:mod:`gigaforge.core.state`:

* ``core.state.TaskState`` had ``run_id``, ``set_plan``, ``serialize_prefix``
  was absent, and the plan was a list of human-readable lines;
* ``core.task_state.TaskState`` had ``task_id``, ``serialize_prefix``,
  ``mark_step_done``, and the plan was a list of ``{tool, args}``.

The pipeline used the first and the ReAct runner the second, so a plan produced
by the episode could not be handed to the coder — there was no object both sides
agreed on.  That is the mechanical reason the ReAct plan never reached the
executor, and it is why the two classes are now one.

The single class lives in :mod:`gigaforge.core.state`; this module keeps the
import path working for the agent layer, which has always imported from here.

0 LLM calls in this module — pure state container.
"""
from __future__ import annotations

from gigaforge.core.state import PlanStep, TaskState, render_plan_step

__all__ = ["PlanStep", "TaskState", "render_plan_step"]
