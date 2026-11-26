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
from collections.abc import Callable, Mapping, Sequence
from typing import Annotated, Any, TypedDict

from gigaforge.agents.react import _REACT_PHASE
from gigaforge.client.errors import BudgetExceeded
from gigaforge.core.context_window_guard import ContextOverflowError, ContextWindowGuard
from gigaforge.core.loop_breaker import LoopBreaker
from gigaforge.core.sync_adapter import await_blocking
from gigaforge.core.task_state import PlanStep
from gigaforge.firewall import compress
from gigaforge.firewall.tokens import estimate_tokens
from gigaforge.providers.messages import nudge

from .react import (
    FINISH_ACTION,
    ReActContext,
    ReActResult,
    ReActRunner,
    _kind_for_tool,
    _remember_file_read,
    read_only_tool_error,
)

__all__ = ["ReActState", "build_react_graph", "run_react_graph"]

_OBS_TOKEN_BUDGET = 1500

#: How many times the goal contract may send the episode back to fix the file.
#: Two is enough for "you dropped a requirement while adding another" and small
#: enough that a model which cannot satisfy the contract does not burn the whole
#: step budget on it.
_MAX_CONTRACT_RETRIES = 2


def _check_contract(state: Any, ctx: ReActContext, written: list[str]) -> Any:
    """Check the produced artifact against the goal's explicit requirements.

    Returns ``None`` when the goal names nothing checkable, so the caller can
    skip the whole correction path.  The check is the same one the classic loop
    runs — this module must not have a second, divergent idea of what the goal
    asked for.
    """
    from gigaforge.harness.goal_contract import check_artifacts, extract_requirements

    goal = str(getattr(state, "goal", "") or "")
    requirements = extract_requirements(goal)
    if not requirements:
        return None
    return check_artifacts(requirements, ctx.worktree, files=written)


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


def _written_path(action: str, action_input: str, raw: str) -> str:
    """Return a path only when a file-mutation tool actually succeeded."""
    if action not in {"write_file", "edit_file", "create_file"}:
        return ""
    if str(raw).lstrip().startswith("ERROR:"):
        return ""
    try:
        payload = json.loads(action_input) if action_input.strip() else {}
    except (json.JSONDecodeError, AttributeError):
        return ""
    return str(payload.get("path") or payload.get("file") or "").strip()


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
    #: Files the episode wrote itself, so the goal contract can check the
    #: artifact the run actually produced rather than the whole worktree.
    written_files: Annotated[list[str], _add]
    #: How many times the contract has sent the episode back to fix the file.
    contract_retries: int
    contract_unmet: list[str]
    #: The episode's own prose when the deliverable is text, not a diff.
    #:
    #: **Why this field had to exist.**  ``ReActResult.final_text`` is what the
    #: answer path reads, and the classic loop set it (``react.py:1245``).  The
    #: LangGraph engine — the product path — never did, because ``_to_result``
    #: had no field to read it from.  So every text product produced an empty
    #: answer and the run was rejected with "the run produced no text", while
    #: the model had answered correctly.  Measured on both text fixtures: the
    #: fake returned the document as ``finish`` prose and the result was empty.
    final_text: str


def _writer() -> Callable[[dict[str, Any]], None]:
    try:
        from langgraph.config import get_stream_writer

        return get_stream_writer()
    except Exception:  # noqa: BLE001 - nodes must run outside a stream too
        return lambda _payload: None


def _breaker_view(
    steps: list[dict[str, Any]],
    nudges: list[str] | None = None,
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


def _server_token_counter(runner: Any) -> Callable[[Sequence[Any], Any], int | None] | None:
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

    def _count(messages: Sequence[Any], model: Any) -> int | None:
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
    guard: ContextWindowGuard | None = None,
    breaker: LoopBreaker | None = None,
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
    settings = runner.settings
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
                "content": runner.compose_initial_prompt(
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
            window_guard.check(trajectory, model=runner.router.route(_REACT_PHASE))
        except ContextOverflowError as exc:
            _emit("react.context_overflow", step_idx=step_idx, detail=str(exc)[:400])
            return {
                "finished": True,
                "finish_reason": "context_overflow",
                "trajectory": seed,
            }

        est_in = (sum(len(m.get("content") or "") for m in trajectory) + 2) // 3
        task_remaining = runner.budgets.task_remaining_for(task_state.task_id)
        phase_remaining = runner.budgets.remaining(_REACT_PHASE)
        max_tokens = min(
            runner._react_out(),
            max(0, task_remaining - est_in),
            phase_remaining,
        )
        if max_tokens <= 0:
            _emit("react.budget_exhausted", step_idx=step_idx,
                  task_tokens_remaining=task_remaining, estimated_input_tokens=est_in)
            return {
                "finished": True,
                "finish_reason": "budget_exceeded",
                "trajectory": trajectory,
            }
        try:
            claim = runner.budgets.reserve(
                _REACT_PHASE, est_in, max_tokens, task_id=task_state.task_id
            )
        except BudgetExceeded:
            # Another concurrent stage may spend tokens after the check above.
            # Refresh once and reserve only what is still available; if the
            # reservation still fails, stop this episode instead of falling
            # back to an unbounded linear plan.
            task_remaining = runner.budgets.task_remaining_for(task_state.task_id)
            phase_remaining = runner.budgets.remaining(_REACT_PHASE)
            adjusted_tokens = min(
                runner._react_out(),
                max(0, task_remaining - est_in),
                phase_remaining,
            )
            if adjusted_tokens <= 0 or adjusted_tokens >= max_tokens:
                _emit("react.budget_exhausted", step_idx=step_idx,
                      task_tokens_remaining=task_remaining,
                      estimated_input_tokens=est_in)
                return {
                    "finished": True,
                    "finish_reason": "budget_exceeded",
                    "trajectory": trajectory,
                }
            max_tokens = adjusted_tokens
            try:
                claim = runner.budgets.reserve(
                    _REACT_PHASE, est_in, max_tokens, task_id=task_state.task_id
                )
            except BudgetExceeded:
                _emit("react.budget_exhausted", step_idx=step_idx,
                      task_tokens_remaining=runner.budgets.task_remaining_for(
                          task_state.task_id
                      ),
                      estimated_input_tokens=est_in)
                return {
                    "finished": True,
                    "finish_reason": "budget_exceeded",
                    "trajectory": trajectory,
                }

        started = time.monotonic()
        try:
            chat_result = await runner.client.chat(
                runner.router.route(_REACT_PHASE),
                trajectory,
                temperature=0.1,
                max_tokens=max_tokens,
                task_id=task_state.task_id,
                phase=_REACT_PHASE,
            )
        except BaseException as exc:
            claim.cancel()
            # A provider failure is not a parse failure and not a bad plan.  It
            # is recorded on the state so the caller can say what happened
            # instead of "the model produced no plan" — measured live: 55
            # retried 429s surfaced as a planning defect.
            _emit("react.provider_error", step_idx=step_idx,
                  error=f"{type(exc).__name__}: {exc}"[:300])
            return {
                "finished": True,
                "finish_reason": "error",
                "error": f"{type(exc).__name__}: {exc}"[:500],
                "trajectory": seed,
            }
        latency_ms = int((time.monotonic() - started) * 1000)
        text = chat_result.content or ""
        usage = chat_result.usage or {}
        tokens_in = int(usage.get("prompt_tokens", 0) or 0)
        tokens_out = int(usage.get("completion_tokens", 0) or 0)
        try:
            claim.settle(tokens_in, tokens_out)
            runner.budgets.check_overshoot(_REACT_PHASE, task_id=task_state.task_id)
        except Exception:  # noqa: BLE001 - accounting is best effort here
            pass
        runner.tracer.trace(
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

        parsed = runner.parse_step(text)
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
        policy_error = read_only_tool_error(action, ctx)
        if policy_error is not None:
            kind = _kind_for_tool(action)
            observation = compress(
                kind, policy_error, limit_tokens=_OBS_TOKEN_BUDGET
            ).text
            _emit("react.read_only_tool_blocked", action=action)
            return {
                "observation": observation,
                "steps": [{
                    "step_idx": int(gstate.get("step_idx") or 1) - 1,
                    "observation": observation,
                    "blocked": True,
                    "ok": False,
                }],
                "trajectory": [
                    {"role": "user", "content": f"Observation ({kind}):\n{observation}"}
                ],
            }
        # ShellSafety: screen the call before it reaches the sandbox.
        blocked = guards.screen_tool_call(action, action_input)
        if blocked is not None:
            kind = _kind_for_tool(action)
            compressed = compress(kind, blocked, limit_tokens=_OBS_TOKEN_BUDGET)
            observation = compressed.text
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
        observation = compressed.text
        if estimate_tokens(observation) > _OBS_TOKEN_BUDGET:
            observation = observation[: _OBS_TOKEN_BUDGET * 4] + "\n[truncated]"
        if action == "read_file":
            _remember_file_read(ctx, action_input, raw, observation)
        _emit("react.observation", action=action, kind=kind, chars=len(observation))
        delta: list[dict[str, str]] = [
            {"role": "user", "content": f"Observation ({kind}):\n{observation}"}
        ]
        # Track the files the episode wrote.  The goal contract checks the
        # artifact the run produced; without this list it would have to fall
        # back to every text file in the worktree, and a run that wrote nothing
        # would pass on a file that was already in the repository.
        written = _written_path(action, action_input, str(raw))
        # SpecificationAudit: nudge once after the model inspects a new file.
        audit = guards.after_observation(
            list(gstate.get("trajectory") or []) + delta
        )
        if audit is not None:
            delta.append(audit)
        out: dict[str, Any] = {
            "observation": observation,
            "steps": [{"step_idx": int(gstate.get("step_idx") or 1) - 1,
                       "observation": observation}],
            "trajectory": delta,
        }
        if written:
            out["written_files"] = [written]
        return out

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
        """Terminal node: parse the plan, then check the artifact.

        The contract check lives here because this is the only place a run
        declares itself done.  It used to exist **only** in the classic loop
        (``react.py``), so the moment the LangGraph engine became the default
        the check silently disappeared from the product path — two defects
        masking each other, since the engine was unreachable at the time.
        """
        plan = runner.parse_finish_plan(str(gstate.get("action_input") or ""))
        localization = runner.extract_localization(
            plan, str(gstate.get("thought") or "")
        )
        _emit("react.finish", plan_size=len(plan), localization=localization)
        out: dict[str, Any] = {
            "finished": True,
            "finish_reason": "finish_action",
            # The prose the model finished with.  For a text product this IS the
            # deliverable; for a patch it is the plan's rationale and is ignored.
            "final_text": str(gstate.get("action_input") or "").strip(),
            "plan": [
                step.model_dump() if hasattr(step, "model_dump") else (
                    step.to_dict() if hasattr(step, "to_dict") else dict(step)
                )
                for step in plan
            ],
            "localization": localization,
        }
        verdict = _check_contract(
            task_state, ctx, list(gstate.get("written_files") or [])
        )
        if verdict is None or verdict.ok:
            return out
        retries = int(gstate.get("contract_retries") or 0)
        if retries >= _MAX_CONTRACT_RETRIES:
            _emit("react.contract_giveup", unmet=verdict.unmet, retries=retries)
            return out
        _emit("react.contract_unmet", unmet=verdict.unmet, retry=retries + 1)
        out["finished"] = False
        out["finish_reason"] = ""
        out["contract_retries"] = retries + 1
        out["contract_unmet"] = list(verdict.unmet)
        out["trajectory"] = [
            {"role": "assistant", "content": str(gstate.get("action_input") or "")},
            {"role": "user", "content": verdict.as_observation()},
        ]
        return out

    def route(gstate: ReActState) -> str:
        if gstate.get("finished"):
            return "end"
        if int(gstate.get("step_idx") or 0) >= int(gstate.get("max_steps") or 5):
            return "end"
        if str(gstate.get("action") or "") == FINISH_ACTION:
            return "finish"
        return "act"

    def route_finish(gstate: ReActState) -> str:
        """After the contract check: done, or back to the model to fix it."""
        if gstate.get("finished"):
            return "end"
        if int(gstate.get("step_idx") or 0) >= int(gstate.get("max_steps") or 5):
            return "end"
        return "reason"

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
    graph.add_conditional_edges(
        "finish", route_finish, {"reason": "reason", "end": END}
    )
    return graph.compile(checkpointer=checkpointer or InMemorySaver())


def _initial_state(
    runner: ReActRunner, task: str, bootstrap_hint: str | None
) -> ReActState:
    """The state a fresh ReAct episode starts from."""
    return {
        "task": task,
        "bootstrap_hint": bootstrap_hint or "",
        "step_idx": 0,
        "max_steps": int(runner.max_steps),
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
        "written_files": [],
        "contract_retries": 0,
        "contract_unmet": [],
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
    # The trajectory travels with the result so a caller can salvage a plan
    # from what the episode observed when the model's finish payload is
    # unusable.  Without it the guard's (correct) rejection of an untargeted
    # edit left the run reporting zero steps.
    result.trajectory = list(values.get("trajectory") or [])
    # The files the episode wrote travel with the result: the pipeline turns
    # them into a diff, and the contract checks them instead of the worktree.
    result.written_files = list(values.get("written_files") or [])
    # The episode's prose.  A text product's deliverable is this string, and the
    # classic loop set it while this engine did not — so the product path
    # returned an empty answer for every answer/document/analysis goal.
    result.final_text = str(values.get("final_text") or "")
    result.error = str(values.get("error") or "")
    return result


async def run_react_graph_async(
    runner: ReActRunner,
    task: str,
    state: Any,
    *,
    ctx: ReActContext,
    bootstrap_hint: str | None = None,
    checkpointer: Any = None,
    thread_id: str = "",
    guard: ContextWindowGuard | None = None,
    breaker: LoopBreaker | None = None,
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
    bootstrap_hint: str | None = None,
    checkpointer: Any = None,
    thread_id: str = "",
    guard: ContextWindowGuard | None = None,
    breaker: LoopBreaker | None = None,
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
