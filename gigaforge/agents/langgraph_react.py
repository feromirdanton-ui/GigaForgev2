"""ReAct on LangGraph — the same loop, with checkpointing and real streaming.

``ReActRunner`` (``agents/react.py``) is a hand-written ``for`` loop: reason →
act → observe, bounded by ``max_steps``.  It works and its prompt/tool contract
is battle-tested, so this module does **not** re-implement the reasoning: it
reuses the runner's own collaborators (registry, router, budgets, tracer,
client, settings) and its own parsers, and expresses the loop as a graph.

What the graph adds over the loop:

* **Checkpointing** — a ReAct run can be inspected and resumed mid-trajectory.
* **Streaming** — each step is written with ``get_stream_writer()``, so the UI
  sees the thought/action/observation as they happen instead of after the run.
* **The ported middleware finally runs.**  ``ContextWindowGuard`` and
  ``LoopBreaker`` were ported in the previous iteration as standalone
  components; here they are wired into the loop they were written for — the
  guard before every model call, the breaker after every observation.

Every nudge the breaker emits is a **HumanMessage** (``providers.messages.nudge``),
because GigaChat rejects a system message that is not first with a hard 400.

Naming note: the graph state is called ``gstate`` inside the nodes so the
``TaskState`` the runner needs (``state.task_id``, ``state.serialize_prefix()``)
keeps its own name.  Conflating the two is the easiest way to break this file.
"""
from __future__ import annotations

import json
import time
from typing import Annotated, Any, Callable, Mapping, Optional, Sequence, TypedDict

from gigaforge.core.context_window_guard import ContextOverflowError, ContextWindowGuard
from gigaforge.core.loop_breaker import LoopBreaker
from gigaforge.core.sync_adapter import await_blocking
from gigaforge.core.task_state import PlanStep
from gigaforge.firewall import compress
from gigaforge.firewall.tokens import estimate_tokens
from gigaforge.providers.messages import nudge

from .react import FINISH_ACTION, ReActContext, ReActResult, ReActRunner, _kind_for_tool

__all__ = ["ReActState", "build_react_graph", "run_react_graph"]

_OBS_TOKEN_BUDGET = 1500


def _add(left: list, right: list) -> list:
    return list(left or []) + list(right or [])


def _merge_steps(left: list, right: list) -> list:
    """Merge step records by ``step_idx``.

    ``reason`` writes the thought/action and ``act`` later writes the
    observation for the *same* step, so a plain append would produce two
    half-filled records per step.
    """
    merged = [dict(item) for item in (left or [])]
    for item in right or []:
        entry = dict(item)
        index = entry.get("step_idx")
        for position, existing in enumerate(merged):
            if index is not None and existing.get("step_idx") == index:
                merged[position] = {**existing, **entry}
                break
        else:
            merged.append(entry)
    return merged


class ReActState(TypedDict, total=False):
    """Trajectory + bookkeeping.  JSON-safe so a checkpointer can store it."""

    task: str
    bootstrap_hint: str
    step_idx: int
    max_steps: int
    #: ``[{"role": ..., "content": ...}]`` — the wire shape the client takes.
    trajectory: Annotated[list[dict[str, str]], _add]
    steps: Annotated[list[dict[str, Any]], _merge_steps]
    thought: str
    action: str
    action_input: str
    observation: str
    finished: bool
    finish_reason: str
    plan: list[dict[str, Any]]
    localization: dict[str, str]
    llm_calls: int
    tokens_in: int
    tokens_out: int
    nudges: Annotated[list[str], _add]


def _writer() -> Callable[[dict[str, Any]], None]:
    try:
        from langgraph.config import get_stream_writer

        return get_stream_writer()
    except Exception:  # noqa: BLE001 - nodes must run outside a stream too
        return lambda _payload: None


def _breaker_view(
    steps: list[dict[str, Any]],
    nudges: Optional[list[str]] = None,
) -> list[dict[str, Any]]:
    """Rebuild the tool-shaped history the LoopBreaker inspects.

    Each successful step becomes an assistant turn with one ``tool_calls`` entry
    plus the matching ``tool`` result.  Failed parses are skipped: they are not
    tool rounds and would only dilute the signatures.

    ``nudges`` are the markers already injected this run.  They are replayed as
    real nudge messages **in the position they were emitted**, because
    :meth:`LoopBreaker.inspect` bounds every signature with
    ``count_nudges(messages, marker)``.  Without them the counter is always 0,
    every signature fires on every turn, and ``max_injections`` never applies —
    the exact self-loop the module's own docstring warns about.
    """
    view: list[dict[str, Any]] = []
    pending = list(nudges or [])
    for step in steps:
        if not step.get("ok"):
            continue
        action = str(step.get("action") or "")
        if not action or action == FINISH_ACTION:
            continue
        call_id = f"call_{step.get('step_idx')}"
        view.append({
            "role": "assistant",
            "content": str(step.get("thought") or ""),
            "tool_calls": [{
                "id": call_id,
                "name": action,
                "args": str(step.get("action_input") or ""),
            }],
        })
        view.append({
            "role": "tool",
            "name": action,
            "tool_call_id": call_id,
            "content": str(step.get("observation") or ""),
        })
        # A nudge emitted after this round belongs here, before the next one.
        if pending:
            view.append(nudge("", marker=pending.pop(0)))
    for marker in pending:
        view.append(nudge("", marker=marker))
    return view


def _server_token_counter(runner: Any) -> Optional[Callable[[Sequence[Any], Any], Optional[int]]]:
    """A guard counter backed by the provider's server-side ``count_tokens``.

    The context guard used to estimate every request at ``len(text)/4`` even
    when the transport could ask GigaChat for the real number.  This adapter
    closes that gap: it calls ``Provider.count_tokens`` through the runner's
    client, and returns ``None`` (so the guard keeps its estimate) when the
    transport cannot count — the legacy client, or a failed call.

    ``SyncChatClient`` is synchronous, so the counter is too: the guard runs
    inside a graph node, and blocking there is exactly what the rest of the
    node already does.
    """
    client = getattr(runner, "_client", None)
    if client is None:
        return None
    counter = getattr(client, "count_tokens", None)
    if not callable(counter):
        return None

    def _count(messages: Sequence[Any], model: Any) -> Optional[int]:
        try:
            return counter(list(messages), model=str(model or ""))
        except Exception:  # noqa: BLE001 - counting is an optimisation
            return None

    return _count


def build_react_graph(
    runner: ReActRunner,
    *,
    ctx: ReActContext,
    state: Any,
    checkpointer: Any = None,
    guard: Optional[ContextWindowGuard] = None,
    breaker: Optional[LoopBreaker] = None,
) -> Any:
    """Compile the ReAct graph around an existing :class:`ReActRunner`.

    ``runner`` supplies the model client, router, budgets, tracer, tool registry
    and settings — nothing is duplicated.  ``ctx``/``state`` are the same
    objects the classic loop receives.
    """
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph import END, START, StateGraph

    if not runner.registry.names():
        runner.registry.install_defaults(sandbox=ctx.sandbox)
    window_guard = guard or ContextWindowGuard(
        token_counter=_server_token_counter(runner)
    )
    loop_breaker = breaker or LoopBreaker()
    # The same four ported guards the classic loop uses, so both engines behave
    # identically: tool contract, shell safety, specification audit, think tool.
    guards = runner.guards
    guards.install(runner.registry)
    settings = runner._s  # noqa: SLF001 - the runner owns these
    task_state = state

    def _emit(event: str, **payload: Any) -> None:
        _writer()({"event": event, **payload})

    async def reason(gstate: ReActState) -> dict[str, Any]:
        """One model call: the thought/action/action_input JSON object."""
        step_idx = int(gstate.get("step_idx") or 0)
        trajectory = list(gstate.get("trajectory") or [])
        if not trajectory:
            trajectory = [{
                "role": "user",
                "content": runner._compose_initial_prompt(  # noqa: SLF001
                    str(gstate.get("task") or ""),
                    task_state,
                    gstate.get("bootstrap_hint") or None,
                ),
            }]
            # ToolContract: injected once, with the opening prompt.
            trajectory.extend(guards.opening_messages(trajectory))
            seed = trajectory
        else:
            seed = []

        # ContextWindowGuard: compact before the request, not after a 422.
        try:
            window_guard.check(trajectory, model=runner._router.route("plan"))  # noqa: SLF001
        except ContextOverflowError as exc:
            _emit("react.context_overflow", step_idx=step_idx, detail=str(exc)[:400])
            return {
                "finished": True,
                "finish_reason": "context_overflow",
                "trajectory": seed,
            }

        est_in = sum(len(m.get("content") or "") for m in trajectory) // 4
        est_out = max(int(settings.budget_plan_out) // 3, 200)
        runner._budgets.check("plan", est_in, est_out, task_id=task_state.task_id)  # noqa: SLF001

        started = time.monotonic()
        chat_result = await runner._client.chat(  # noqa: SLF001
            runner._router.route("plan"),  # noqa: SLF001
            trajectory,
            temperature=0.1,
            max_tokens=int(settings.budget_plan_out),
            task_id=task_state.task_id,
            phase="plan",
        )
        latency_ms = int((time.monotonic() - started) * 1000)
        text = chat_result.content or ""
        usage = chat_result.usage or {}
        tokens_in = int(usage.get("prompt_tokens", 0) or 0)
        tokens_out = int(usage.get("completion_tokens", 0) or 0)
        try:
            runner._budgets.commit_task(  # noqa: SLF001
                task_state.task_id, "plan", tokens_in, tokens_out
            )
            runner._budgets.check_overshoot("plan", task_id=task_state.task_id)  # noqa: SLF001
        except Exception:  # noqa: BLE001 - accounting is best effort here
            pass
        runner._tracer.trace(  # noqa: SLF001
            task_state.task_id,
            step=f"react_step_{step_idx}",
            kind="llm",
            detail={
                "step_idx": step_idx,
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "raw_head": text[:300],
            },
            tokens=tokens_in + tokens_out,
            latency_ms=latency_ms,
        )

        parsed = runner._parse_step(text)  # noqa: SLF001
        if parsed is None:
            _emit("react.parse_failed", step_idx=step_idx, raw=text[:300])
            return {
                "step_idx": step_idx + 1,
                # Only the DELTA: the reducer appends, so returning the whole
                # trajectory would duplicate the history on every step.
                "trajectory": [
                    {"role": "assistant", "content": text},
                    {"role": "user", "content": (
                        "Твой прошлый ответ не является JSON вида "
                        "{thought,action,action_input}. Верни ТОЛЬКО JSON."
                    )},
                ],
                "steps": [{"step_idx": step_idx, "raw": text, "ok": False,
                           "error": "parse_failed"}],
                "llm_calls": int(gstate.get("llm_calls") or 0) + 1,
                "tokens_in": int(gstate.get("tokens_in") or 0) + tokens_in,
                "tokens_out": int(gstate.get("tokens_out") or 0) + tokens_out,
            }

        thought, action, action_input = parsed
        _emit("react.thought", step_idx=step_idx, thought=thought, action=action)
        return {
            "step_idx": step_idx + 1,
            "trajectory": seed + [{"role": "assistant", "content": text}],
            "steps": [{"step_idx": step_idx, "thought": thought, "action": action,
                       "action_input": action_input, "ok": True}],
            "thought": thought,
            "action": action,
            "action_input": action_input,
            "llm_calls": int(gstate.get("llm_calls") or 0) + 1,
            "tokens_in": int(gstate.get("tokens_in") or 0) + tokens_in,
            "tokens_out": int(gstate.get("tokens_out") or 0) + tokens_out,
        }

    async def act(gstate: ReActState) -> dict[str, Any]:
        """Invoke exactly one tool, then compress its output through the firewall."""
        action = str(gstate.get("action") or "")
        action_input = str(gstate.get("action_input") or "")
        tool = runner.registry.get(action)
        if tool is None:
            available = runner.registry.names() + [FINISH_ACTION]
            return {
                "observation": "",
                "trajectory": [{
                    "role": "user",
                    "content": f"Инструмент {action!r} неизвестен. Доступны: {available}.",
                }],
            }
        # ShellSafety: screen the call before it reaches the sandbox.
        blocked = guards.screen_tool_call(action, action_input)
        if blocked is not None:
            kind = _kind_for_tool(action)
            compressed = compress(kind, blocked, limit_tokens=_OBS_TOKEN_BUDGET)
            observation = json.dumps(compressed, ensure_ascii=False)
            _emit("react.shell_blocked", action=action, detail=blocked[:200])
            return {
                "observation": observation,
                "steps": [{"step_idx": int(gstate.get("step_idx") or 1) - 1,
                           "observation": observation, "blocked": True}],
                "trajectory": [
                    {"role": "user", "content": f"Observation ({kind}):\n{observation}"}
                ],
            }
        try:
            raw = await tool.run(action_input, ctx=ctx)
        except Exception as exc:  # noqa: BLE001 - a tool failure is an observation
            raw = f"ERROR: tool {action!r} raised: {exc}"
        kind = _kind_for_tool(action)
        compressed = compress(kind, raw, limit_tokens=_OBS_TOKEN_BUDGET)
        observation = json.dumps(compressed, ensure_ascii=False)
        if estimate_tokens(observation) > _OBS_TOKEN_BUDGET:
            observation = observation[: _OBS_TOKEN_BUDGET * 4] + "\n[truncated]"
        _emit("react.observation", action=action, kind=kind, chars=len(observation))
        delta: list[dict[str, str]] = [
            {"role": "user", "content": f"Observation ({kind}):\n{observation}"}
        ]
        # SpecificationAudit: nudge once after the model inspects a new file.
        audit = guards.after_observation(
            list(gstate.get("trajectory") or []) + delta
        )
        if audit is not None:
            delta.append(audit)
        return {
            "observation": observation,
            "steps": [{"step_idx": int(gstate.get("step_idx") or 1) - 1,
                       "observation": observation}],
            "trajectory": delta,
        }

    def observe(gstate: ReActState) -> dict[str, Any]:
        """LoopBreaker: at most one nudge per turn, always a HumanMessage.

        The breaker reads a *tool-shaped* history (assistant turns carrying
        ``tool_calls`` followed by ``tool`` results), while the wire trajectory
        is plain role/content.  :func:`_breaker_view` rebuilds that shape from
        the recorded steps **and the nudges already injected**, so the breaker
        sees exactly what it was written for — including its own markers, which
        are what bound each signature to ``max_injections``.
        """
        view = _breaker_view(
            list(gstate.get("steps") or []), list(gstate.get("nudges") or [])
        )
        signal = loop_breaker.inspect(view)
        if signal is None:
            return {}
        _emit("react.loop_breaker", kind=signal.kind, detail=signal.detail)
        return {"trajectory": [signal.message()], "nudges": [signal.marker]}

    def finish(gstate: ReActState) -> dict[str, Any]:
        """Terminal node: parse the plan out of the finish payload."""
        plan = runner._parse_finish_plan(str(gstate.get("action_input") or ""))  # noqa: SLF001
        localization = runner._extract_localization(  # noqa: SLF001
            plan, str(gstate.get("thought") or "")
        )
        _emit("react.finish", plan_size=len(plan), localization=localization)
        return {
            "finished": True,
            "finish_reason": "finish_action",
            "plan": [
                step.model_dump() if hasattr(step, "model_dump") else dict(step)
                for step in plan
            ],
            "localization": localization,
        }

    def route(gstate: ReActState) -> str:
        if gstate.get("finished"):
            return "end"
        if int(gstate.get("step_idx") or 0) >= int(gstate.get("max_steps") or 5):
            return "end"
        if str(gstate.get("action") or "") == FINISH_ACTION:
            return "finish"
        return "act"

    graph = StateGraph(ReActState)
    graph.add_node("reason", reason)
    graph.add_node("act", act)
    graph.add_node("observe", observe)
    graph.add_node("finish", finish)
    graph.add_edge(START, "reason")
    graph.add_conditional_edges(
        "reason", route, {"act": "act", "finish": "finish", "end": END}
    )
    graph.add_edge("act", "observe")
    graph.add_edge("observe", "reason")
    graph.add_edge("finish", END)
    return graph.compile(checkpointer=checkpointer or InMemorySaver())


def _initial_state(
    runner: ReActRunner, task: str, bootstrap_hint: Optional[str]
) -> ReActState:
    """The state a fresh ReAct episode starts from."""
    return {
        "task": task,
        "bootstrap_hint": bootstrap_hint or "",
        "step_idx": 0,
        "max_steps": int(runner._max_steps),  # noqa: SLF001
        "trajectory": [],
        "steps": [],
        "finished": False,
        "finish_reason": "",
        "plan": [],
        "localization": {},
        "llm_calls": 0,
        "tokens_in": 0,
        "tokens_out": 0,
        "nudges": [],
    }


def _to_result(values: Mapping[str, Any]) -> ReActResult:
    """Map the graph's final state onto the shape the DAG expects."""
    result = ReActResult()
    result.llm_calls = int(values.get("llm_calls") or 0)
    result.steps = list(values.get("steps") or [])
    result.finished = bool(values.get("finished"))
    result.finish_reason = str(values.get("finish_reason") or "max_steps")
    result.localization = dict(values.get("localization") or {})
    result.plan = [PlanStep(**item) for item in (values.get("plan") or [])]
    return result


async def run_react_graph_async(
    runner: ReActRunner,
    task: str,
    state: Any,
    *,
    ctx: ReActContext,
    bootstrap_hint: Optional[str] = None,
    checkpointer: Any = None,
    thread_id: str = "",
    guard: Optional[ContextWindowGuard] = None,
    breaker: Optional[LoopBreaker] = None,
) -> ReActResult:
    """Async entry point: drive the graph on the **caller's** loop.

    This is the variant an async caller must use.  :func:`run_react_graph`
    hops to the process-wide adapter loop, which is correct for the synchronous
    DAG but wrong here: an ``AsyncSqliteSaver``'s ``aiosqlite`` connection
    belongs to the loop that opened it, so driving the graph from another loop
    deadlocks instead of checkpointing.  A real multi-step run with a durable
    checkpointer hung on exactly that.
    """
    from gigaforge.core.langgraph_runtime import thread_config

    graph = build_react_graph(
        runner, ctx=ctx, state=state, checkpointer=checkpointer, guard=guard, breaker=breaker
    )
    config = thread_config(thread_id or f"react-{state.task_id}")
    initial = _initial_state(runner, task, bootstrap_hint)
    async for _chunk in graph.astream(initial, config, stream_mode="custom"):
        pass
    # ``aget_state``, not ``get_state``: an ``AsyncSqliteSaver`` refuses the
    # synchronous accessor from the main thread ("Synchronous calls to
    # AsyncSqliteSaver are only allowed from a different thread").
    snapshot = await graph.aget_state(config)
    return _to_result(snapshot.values or {})


def run_react_graph(
    runner: ReActRunner,
    task: str,
    state: Any,
    *,
    ctx: ReActContext,
    bootstrap_hint: Optional[str] = None,
    checkpointer: Any = None,
    thread_id: str = "",
    guard: Optional[ContextWindowGuard] = None,
    breaker: Optional[LoopBreaker] = None,
) -> ReActResult:
    """Run the ReAct graph and return the same :class:`ReActResult` as the loop.

    Synchronous entry point for the DAG.  An async caller should use
    :func:`run_react_graph_async` instead \u2014 see its docstring for why.
    """
    from gigaforge.core.langgraph_runtime import thread_config

    graph = build_react_graph(
        runner, ctx=ctx, state=state, checkpointer=checkpointer, guard=guard, breaker=breaker
    )
    config = thread_config(thread_id or f"react-{state.task_id}")
    initial = _initial_state(runner, task, bootstrap_hint)

    async def _drive() -> Mapping[str, Any]:
        async for _chunk in graph.astream(initial, config, stream_mode="custom"):
            pass
        return graph.get_state(config).values or {}

    return _to_result(await_blocking(_drive()))
