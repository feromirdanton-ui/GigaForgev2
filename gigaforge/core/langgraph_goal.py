"""Goal mode on LangGraph — the level-2 replacement for the hand-rolled loop.

``GoalRunner`` in :mod:`gigaforge.core.goal` implements the author → verify →
improve cycle with a ``for`` loop, a JSON record and a JSONL trace.  That works,
but it re-implements three things LangGraph already does properly:

* **Checkpointing.**  Every node transition is persisted, so a goal resumes at
  the exact node it stopped on — not at "the last cycle we wrote to disk".
* **``interrupt()`` for approval.**  The human-in-the-loop gate becomes a graph
  node that suspends the thread and returns the decision on resume, instead of
  a callback threaded through the pipeline.
* **Event streaming.**  Nodes write progress with ``get_stream_writer()``; the
  UI consumes the stream directly instead of polling a trace file.

What does **not** move: the pipeline itself.  ``author`` calls
``pipeline.run(...)`` — the deterministic DAG with its contracts, firewall,
budget and breaker — exactly as before.  The graph owns the *outer* loop only.

State is a plain ``TypedDict`` of JSON-safe values so a SQLite checkpointer can
serialise it; the pipeline, verifier, store and workspace are closed over by
:func:`build_goal_graph` and never enter the state.
"""
from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Annotated, Any, Callable, Mapping, Optional, Sequence, TypedDict

from .cancellation import CancellationToken
from .goal import (
    GoalCycle,
    GoalResult,
    GoalRunner,
    GoalStore,
    VerificationReport,
    Verifier,
    WorkspaceVerifier,
    _merge_conversation_context,
    _fingerprint_changed_files,
    _now,
    _remaining_goal_budget,
    _repeated_failed_goal_output,
    _safe_excerpt,
)
from .goal_workspace import GoalWorkspace
from .langgraph_runtime import (
    Command,
    graph_state_summary,
    interrupt,
    iter_graph_events,
    open_checkpointer,
    require_langgraph,
    thread_config,
)

__all__ = [
    "GoalState",
    "build_goal_graph",
    "run_goal_graph",
    "GOAL_NODES",
]

#: Node names, in the order a successful cycle visits them.
GOAL_NODES = ("author", "verify", "approve", "publish")


def _merge_cycles(left: list, right: list) -> list:
    """Append new cycles, but let a later write *update* an existing index.

    ``verify`` folds the verification report into the cycle ``author`` just
    appended.  A plain ``operator.add`` would duplicate that cycle, so the
    reducer merges by ``index`` instead: same index replaces, new index appends.
    """
    merged = [dict(item) for item in (left or [])]
    for item in right or []:
        entry = dict(item)
        index = entry.get("index")
        for position, existing in enumerate(merged):
            if index is not None and existing.get("index") == index:
                merged[position] = {**existing, **entry}
                break
        else:
            merged.append(entry)
    return merged


class GoalState(TypedDict, total=False):
    """Everything the goal graph carries between nodes (JSON-safe only)."""

    goal_id: str
    goal: str
    conversation_context: str
    cycle_index: int
    max_cycles: int
    max_fix_iterations: int
    use_subagents: bool
    files: list[str]
    symbols: list[str]
    budget_tokens: int
    require_approval: bool
    #: Merged by ``index`` so ``verify`` can fold its report into the cycle.
    cycles: Annotated[list[dict[str, Any]], _merge_cycles]
    status: str
    accepted: bool
    error: str
    feedback: str
    no_progress: bool
    budget_exhausted: bool
    changed_files: list[str]
    published_files: list[str]
    tokens: int
    calls: int
    started_at: str
    updated_at: str
    workspace_root: str
    #: The diff the approval node shows, and the decision it got back.
    pending_diff: str
    verification: Optional[dict[str, Any]]
    approval: dict[str, Any]
    #: The reply, when the goal asked for one.  A chat goal's output is a
    #: sentence, not a diff; it has to survive the graph to reach the result.
    answer: str
    answer_citations: list[str]
    answer_searched: bool


def _writer() -> Callable[[dict[str, Any]], None]:
    """The node's stream writer, or a no-op when streaming is not active."""
    try:
        from langgraph.config import get_stream_writer

        return get_stream_writer()
    except Exception:  # noqa: BLE001 - a node must run outside a stream too
        return lambda _payload: None


def build_goal_graph(
    pipeline: Any,
    *,
    repo_root: Path,
    verifier: Optional[Verifier] = None,
    store: Optional[GoalStore] = None,
    workspace: Optional[GoalWorkspace] = None,
    checkpointer: Any = None,
    emit: Optional[Callable[[str, dict[str, Any]], None]] = None,
    cancellation: Optional[CancellationToken] = None,
) -> Any:
    """Compile the goal graph.

    ``pipeline`` is the deterministic core; ``verifier``/``store``/``workspace``
    default to the same objects ``GoalRunner`` would build, so a graph run and a
    classic run leave identical artefacts on disk.

    ``cancellation`` is closed over rather than carried in the state: the state
    is serialised by the checkpointer, and a token is not JSON-safe.
    """
    require_langgraph()

    from langgraph.graph import END, START, StateGraph

    root = Path(repo_root).resolve()
    check: Verifier = verifier or WorkspaceVerifier()
    goal_store = store or GoalStore(root)

    def _emit(event: str, **payload: Any) -> None:
        """Trace + stream in one call, so both surfaces see the same events."""
        body = {"event": event, **payload}
        if emit is not None:
            try:
                emit(event, body)
            except Exception:  # noqa: BLE001 - telemetry must never break a run
                pass
        _writer()(body)

    def _persist(state: Mapping[str, Any], **overrides: Any) -> None:
        """Write the durable record the UI and ``--resume`` read.

        Called at the end of every node, so a crash mid-run leaves a record that
        says which cycle was in flight instead of only the pre-run snapshot.
        """
        record = {
            "schema_version": 1,
            "goal_id": state.get("goal_id", ""),
            "goal": state.get("goal", ""),
            "status": state.get("status", "running"),
            "accepted": bool(state.get("accepted")),
            "cycles": list(state.get("cycles") or []),
            "active_run_id": "",
            "active_cycle_index": 0,
            "tokens": int(state.get("tokens") or 0),
            "budget_tokens": int(state.get("budget_tokens") or 0),
            "calls": int(state.get("calls") or 0),
            "started_at": state.get("started_at", ""),
            "updated_at": _now(),
            "error": state.get("error", ""),
            "workspace_root": state.get("workspace_root", ""),
            "published_files": list(state.get("published_files") or []),
            "engine": "langgraph",
        }
        record.update(overrides)
        try:
            goal_store.save(record)
        except Exception:  # noqa: BLE001 - persistence is best effort per node
            pass

    def _persist_node(state: Mapping[str, Any], **overrides: Any) -> None:
        """Persist the state a node just produced, merged over the live state.

        LangGraph hands a node only its *own* return value, so the record is
        rebuilt from the accumulated state plus that delta — otherwise a node
        would overwrite the durable record with a partial view.
        """
        merged = dict(state)
        merged.update(overrides)
        _persist(merged)

    def _persisting(fn: Callable[[Any], dict[str, Any]]) -> Callable[[Any], dict[str, Any]]:
        """Wrap a node so its result is written to the durable record.

        Without this the record was only written once, at the very end of the
        run, so a crash mid-cycle left the UI and ``--resume`` looking at the
        pre-run snapshot.
        """

        def wrapped(state: Any) -> dict[str, Any]:
            delta = fn(state) or {}
            merged = dict(state)
            merged.update(delta)
            _persist(merged)
            return delta

        wrapped.__name__ = getattr(fn, "__name__", "node")
        return wrapped

    # ── nodes ────────────────────────────────────────────────────────────────
    def author(state: GoalState) -> dict[str, Any]:
        """One implementation cycle: the deterministic pipeline, unchanged."""
        cycle_index = int(state.get("cycle_index") or 0) + 1
        max_cycles = int(state.get("max_cycles") or 8)
        goal = str(state.get("goal") or "")
        feedback = str(state.get("feedback") or "")
        run_id = f"{state.get('goal_id')}-c{cycle_index}-{uuid.uuid4().hex[:6]}"
        prompt = GoalRunner._cycle_prompt(goal, feedback)
        # The graph path must honour the same cooperative cancellation the
        # classic runner does; without this a cancelled goal kept spending
        # model calls until the cycle limit.
        cancel = cancellation
        if cancel is not None and getattr(cancel, "cancelled", False):
            _emit(
                "goal.cancelled",
                node="Цикл из прерван",
                status="Отменено",
                goal_id=state.get("goal_id"),
            )
            return {
                "status": "cancelled",
                "accepted": False,
                "error": str(getattr(cancel, "reason", "") or "cancelled"),
                "updated_at": _now(),
            }

        budget_tokens = int(state.get("budget_tokens") or 0)
        tokens_used = max(0, int(state.get("tokens") or 0))
        budget_remaining = _remaining_goal_budget(budget_tokens, tokens_used)
        if budget_tokens and budget_remaining == 0:
            message = (
                f"Goal token budget exhausted ({tokens_used}/{budget_tokens}); "
                "increase budget_tokens to resume."
            )
            _emit(
                "goal.budget_exhausted",
                node=f"Цикл {cycle_index}",
                status="Остановлено: исчерпан бюджет токенов",
                goal_id=state.get("goal_id"),
                tokens_used=tokens_used,
                budget_tokens=budget_tokens,
            )
            return {
                "status": "blocked",
                "accepted": False,
                "error": message,
                "feedback": message,
                "budget_exhausted": True,
                "updated_at": _now(),
            }

        _emit(
            "goal.cycle_start",
            node=f"Цикл {cycle_index} из {max_cycles}",
            status="Агент вносит изменения",
            goal_id=state.get("goal_id"),
            cycle_index=cycle_index,
            cycle_limit=max_cycles,
        )
        started = time.monotonic()
        try:
            result = pipeline.run(
                prompt,
                run_id=run_id,
                files=list(state.get("files") or []),
                symbols=list(state.get("symbols") or []),
                budget_tokens=budget_remaining,
                use_cache=False,
                dry_run=False,
                use_subagents=bool(state.get("use_subagents")),
                react=True,
                max_fix_iterations=int(state.get("max_fix_iterations") or 2),
                conversation_context=str(state.get("conversation_context") or ""),
                cancellation=cancel,
                resume=False,
            )
        except Exception as exc:  # noqa: BLE001 - a failed cycle is a state, not a crash
            error = f"{type(exc).__name__}: {str(exc)[:700]}"
            cycle = GoalCycle(
                index=cycle_index,
                run_id=run_id,
                pipeline_accepted=False,
                summary="pipeline execution raised an exception",
                error=error,
                duration_ms=(time.monotonic() - started) * 1000,
            )
            _emit(
                "goal.cycle_finish",
                node=f"Цикл {cycle_index}",
                status="Изменения не завершены",
                goal_id=state.get("goal_id"),
                cycle_index=cycle_index,
                summary=_safe_excerpt(error, limit=700),
            )
            return {
                "cycle_index": cycle_index,
                "cycles": [cycle.to_dict()],
                "accepted": False,
                "status": "failed",
                "error": error,
                "feedback": error,
                "changed_files": [],
                "pending_diff": "",
                "verification": None,
                "updated_at": _now(),
            }

        accepted = bool(getattr(result, "accepted", False)) and str(
            getattr(result, "status", "") or ""
        ) != "failed"
        changed = list(getattr(result, "changed_files", []) or [])
        error = str(getattr(result, "error", "") or "")
        if not accepted and not error:
            error = GoalRunner._pipeline_feedback(result)
        cycle = GoalCycle(
            index=cycle_index,
            run_id=run_id,
            pipeline_accepted=accepted,
            changed_files=changed,
            summary=str(getattr(result, "summary", "") or "")[:1200],
            tokens=int(getattr(result, "tokens", 0) or 0),
            calls=int(getattr(result, "calls", 0) or 0),
            duration_ms=float(getattr(result, "duration_ms", 0.0) or 0.0),
            error=error[:900],
            output_fingerprint=_fingerprint_changed_files(root, changed),
        )
        total_tokens = int(state.get("tokens") or 0) + cycle.tokens
        total_calls = int(state.get("calls") or 0) + cycle.calls
        if budget_tokens and cycle.tokens > budget_remaining:
            message = (
                "Pipeline exceeded the remaining goal token budget "
                f"({cycle.tokens}>{budget_remaining}); review usage accounting."
            )
            _emit(
                "goal.budget_exceeded",
                node=f"Цикл {cycle_index}",
                status="Pipeline превысил оставшийся бюджет",
                goal_id=state.get("goal_id"),
                cycle_index=cycle_index,
                cycle_tokens=cycle.tokens,
                budget_remaining=budget_remaining,
            )
            return {
                "cycle_index": cycle_index,
                "cycles": [cycle.to_dict()],
                "accepted": False,
                "status": "blocked",
                "error": message,
                "feedback": message,
                "changed_files": changed,
                "pending_diff": str(getattr(result, "final_diff", "") or ""),
                "verification": None,
                "tokens": total_tokens,
                "calls": total_calls,
                "budget_exhausted": True,
                "updated_at": _now(),
            }
        _emit(
            "goal.cycle_finish",
            node=f"Цикл {cycle_index} из {max_cycles}",
            status="Изменения применены" if accepted else "Изменения не применены",
            goal_id=state.get("goal_id"),
            cycle_index=cycle_index,
            cycle_limit=max_cycles,
            accepted=accepted,
            summary=_safe_excerpt(error, limit=700) if error else "",
        )
        return {
            "cycle_index": cycle_index,
            "cycles": [cycle.to_dict()],
            "accepted": accepted,
            "status": "running" if accepted else "needs_attention",
            "error": error[:900],
            "feedback": error[:900],
            "changed_files": changed,
            "pending_diff": str(getattr(result, "final_diff", "") or ""),
            "verification": None,
            "tokens": total_tokens,
            "calls": total_calls,
            # The reply, when the goal asked for one.  This is the LangGraph
            # path — the one the product actually runs — and it dropped the
            # answer: measured live, "What is 2+2?" completed with
            # ``answer: None`` while ``pipeline.answer`` had logged 20 chars.
            # The classic GoalRunner carried it; this path did not.
            "answer": str(getattr(result, "answer", "") or ""),
            "answer_citations": list(getattr(result, "answer_citations", []) or []),
            "answer_searched": bool(getattr(result, "answer_searched", False)),
            "updated_at": _now(),
        }

    def verify(state: GoalState) -> dict[str, Any]:
        """Deterministic project checks — the same verifier as the classic path."""
        if not state.get("accepted"):
            if state.get("budget_exhausted"):
                return {}
            cycles = list(state.get("cycles") or [])
            if len(cycles) >= 2 and _repeated_failed_goal_output(cycles[-1], cycles[-2]):
                message = (
                    "Goal stopped after the same output and failure repeated; "
                    "review the saved workspace or change the task."
                )
                _emit(
                    "goal.no_progress",
                    node=f"Цикл {int(state.get('cycle_index') or 0)}",
                    status="Остановлено: результат не изменился",
                    goal_id=state.get("goal_id"),
                    cycle_index=int(state.get("cycle_index") or 0),
                    output_fingerprint=cycles[-1].get("output_fingerprint", ""),
                )
                return {
                    "status": "needs_attention",
                    "accepted": False,
                    "error": message,
                    "no_progress": True,
                }
            return {}
        cycle_index = int(state.get("cycle_index") or 0)
        max_cycles = int(state.get("max_cycles") or 8)
        _emit(
            "goal.verify_start",
            node=f"Цикл {cycle_index} из {max_cycles}",
            status="Проверяю результат",
            goal_id=state.get("goal_id"),
            cycle_index=cycle_index,
        )
        report: VerificationReport = check.verify(
            root, list(state.get("changed_files") or []), str(state.get("goal") or "")
        )
        _emit(
            "goal.verify_finish",
            node=f"Цикл {cycle_index} из {max_cycles}",
            status=(
                "Проверка пройдена" if report.passed is True
                else "Проверка недоступна" if report.passed is None
                else "Проверка нашла ошибки"
            ),
            goal_id=state.get("goal_id"),
            cycle_index=cycle_index,
            passed=report.passed,
            summary=_safe_excerpt(report.feedback(), limit=700),
        )
        # Fold the verification into the cycle record that was already appended.
        cycles = list(state.get("cycles") or [])
        if cycles:
            cycles[-1] = {**cycles[-1], "verification": report.to_dict()}
        delta = {
            "verification": report.to_dict(),
            "cycles": cycles,
            "feedback": report.feedback() if report.passed is not True else "",
            "accepted": report.passed is True,
            "status": (
                "running" if report.passed is True
                else "blocked" if report.passed is None
                else "needs_attention"
            ),
        }
        if (
            report.passed is False
            and len(cycles) >= 2
            and _repeated_failed_goal_output(cycles[-1], cycles[-2])
        ):
            message = (
                "Goal stopped after the same output and failure repeated; "
                "review the saved workspace or change the task."
            )
            _emit(
                "goal.no_progress",
                node=f"Цикл {cycle_index}",
                status="Остановлено: результат не изменился",
                goal_id=state.get("goal_id"),
                cycle_index=cycle_index,
                output_fingerprint=cycles[-1].get("output_fingerprint", ""),
            )
            delta.update(
                status="needs_attention",
                accepted=False,
                error=message,
                no_progress=True,
            )
        return delta

    def approve(state: GoalState) -> dict[str, Any]:
        """Human-in-the-loop gate.  Suspends the thread; resumes with a decision.

        This is the ``interrupt()`` replacement for ``ApprovalGate``: the graph
        stops here, the caller shows the diff, and ``Command(resume=...)``
        carries the answer back into this same node.
        """
        if not state.get("accepted") or not state.get("require_approval"):
            return {"approval": {"decision": "apply", "reason": "auto"}}
        if state.get("verification", {}).get("passed") is not True:
            # Nothing to publish, so nothing to approve.
            return {"approval": {"decision": "apply", "reason": "not verified"}}
        decision = interrupt(
            {
                "kind": "goal_approval",
                "goal_id": state.get("goal_id"),
                "cycle_index": state.get("cycle_index"),
                "diff": state.get("pending_diff") or "",
                "changed_files": list(state.get("changed_files") or []),
                "verification": state.get("verification") or {},
            }
        )
        if isinstance(decision, Mapping):
            payload = dict(decision)
        elif isinstance(decision, bool):
            payload = {"decision": "apply" if decision else "reject"}
        else:
            payload = {"decision": str(decision or "apply")}
        payload.setdefault("decision", "apply")

        # ``abort`` ends the whole task, so the record must say so.  Routing it
        # to ``stop`` without recording a reason left the run reporting
        # "cycle limit reached before verification passed" — a statement about
        # the cycle budget that is simply false when the user said stop.
        if str(payload.get("decision")) == "abort":
            reason = str(payload.get("reason") or "aborted by the user")
            _emit(
                "goal.approval_abort",
                node="\u0421\u043e\u0433\u043b\u0430\u0441\u043e\u0432\u0430\u043d\u0438\u0435",
                status="\u0417\u0430\u0434\u0430\u0447\u0430 \u043f\u0440\u0435\u0440\u0432\u0430\u043d\u0430",
                goal_id=state.get("goal_id"),
                summary=_safe_excerpt(reason, limit=400),
            )
            return {
                "approval": payload,
                "status": "cancelled",
                "accepted": False,
                "error": reason,
            }

        # ``edit``: the human's diff replaces the pipeline's.  It is applied to
        # the workspace copy here, inside the node, so the publish step that
        # follows publishes what was actually approved.  A diff that does not
        # apply is refused and the run records why \u2014 publishing a diff that was
        # never applied is the failure this gate exists to prevent.
        if str(payload.get("decision")) == "edit":
            edited = str(payload.get("edited_diff") or "")
            ok, paths, error = _apply_edited_diff(
                edited,
                root=root,
                workspace=workspace,
                changed_files=list(state.get("changed_files") or []),
            )
            if not ok:
                _emit(
                    "goal.approval_edit_failed",
                    node="\u0421\u043e\u0433\u043b\u0430\u0441\u043e\u0432\u0430\u043d\u0438\u0435",
                    status="\u041f\u0440\u0430\u0432\u043a\u0430 \u043e\u0442\u043a\u043b\u043e\u043d\u0435\u043d\u0430",
                    goal_id=state.get("goal_id"),
                    summary=_safe_excerpt(error, limit=400),
                )
                return {
                    "approval": {"decision": "abort", "reason": error},
                    "error": error,
                }
            payload["changed_files"] = paths
            return {
                "approval": payload,
                "pending_diff": edited,
                "changed_files": paths,
            }

        return {"approval": payload}

    def publish(state: GoalState) -> dict[str, Any]:
        """Publish verified files atomically, or record why we did not."""
        decision = str((state.get("approval") or {}).get("decision") or "apply")
        # ``edit`` is an approval: the human changed the diff and said go.  It
        # used to fall into the refusal branch, so an edited diff was applied to
        # the workspace and then never published — the run reported "declined by
        # the user: edit" for a decision that meant the opposite.
        if decision not in ("apply", "edit"):
            return {
                "status": "needs_attention",
                "accepted": False,
                "error": f"declined by the user: {decision}",
            }
        if state.get("verification", {}).get("passed") is not True:
            return {}
        published: list[str] = []
        if workspace is not None:
            try:
                published = list(workspace.publish(list(state.get("changed_files") or [])))
            except Exception as exc:  # noqa: BLE001 - a conflict is a resumable state
                return {
                    "status": "needs_attention",
                    "accepted": False,
                    "error": f"publish failed: {type(exc).__name__}: {exc}"[:900],
                }
            # The classic runner releases the project copy once the verified
            # files are published; the graph path must do the same or every
            # completed goal leaks a full copy of the project on disk.
            try:
                workspace.cleanup_project_copy()
            except Exception:  # noqa: BLE001 - cleanup is best effort
                pass
        _emit(
            "goal.publish",
            node="Публикация",
            status="Проверенные файлы опубликованы" if published else "Изменений нет",
            goal_id=state.get("goal_id"),
            files=published,
        )
        return {
            "status": "completed",
            "accepted": True,
            "published_files": published,
            "error": "",
            "updated_at": _now(),
        }

    # ── routing ──────────────────────────────────────────────────────────────
    def after_verify(state: GoalState) -> str:
        if state.get("no_progress") or state.get("status") in {"failed", "blocked"}:
            return "stop"
        if not state.get("accepted"):
            return "retry" if int(state.get("cycle_index") or 0) < int(state.get("max_cycles") or 8) else "stop"
        return "approve"

    def after_approve(state: GoalState) -> str:
        decision = str((state.get("approval") or {}).get("decision") or "apply")
        if decision == "abort":
            # ``abort`` is the one decision that ends the whole task: the user
            # said stop, not "try again".  Routing it to ``publish`` would
            # record a refusal and end the run anyway, but it would also run
            # the publish node, which is not what "abort" means.
            return "stop"
        if decision != "apply":
            # A declined diff is final for this cycle: ``publish`` records the
            # refusal and ends the run, so the reason survives in the record.
            return "publish"
        if state.get("verification", {}).get("passed") is True:
            return "publish"
        return "retry" if int(state.get("cycle_index") or 0) < int(state.get("max_cycles") or 8) else "stop"

    graph = StateGraph(GoalState)
    graph.add_node("author", _persisting(author))
    graph.add_node("verify", _persisting(verify))
    graph.add_node("approve", _persisting(approve))
    graph.add_node("publish", _persisting(publish))
    graph.add_edge(START, "author")
    graph.add_edge("author", "verify")
    graph.add_conditional_edges(
        "verify", after_verify, {"approve": "approve", "retry": "author", "stop": END}
    )
    graph.add_conditional_edges(
        "approve", after_approve, {"publish": "publish", "retry": "author", "stop": END}
    )
    graph.add_edge("publish", END)
    if checkpointer is None:
        from langgraph.checkpoint.memory import InMemorySaver

        checkpointer = InMemorySaver()
    return graph.compile(checkpointer=checkpointer)


def _parse_diff_paths(diff: str) -> list[str]:
    """The file paths a unified diff touches, from its ``+++`` headers.

    Used to derive ``changed_files`` from a human-edited diff.  ``/dev/null``
    (a file creation) is skipped: it is not a path.
    """
    paths: list[str] = []
    for line in str(diff or "").splitlines():
        if not line.startswith("+++ "):
            continue
        raw = line[4:].strip()
        if raw.startswith("b/"):
            raw = raw[2:]
        if raw in ("", "/dev/null"):
            continue
        if raw not in paths:
            paths.append(raw)
    return paths


def _apply_edited_diff(
    edited: str,
    *,
    root: Path,
    workspace: Any,
    changed_files: Sequence[str],
) -> tuple[bool, list[str], str]:
    """Put a human-edited diff onto the workspace copy.

    Returns ``(ok, paths, error)``.

    The pipeline has already applied *its* diff to the workspace copy, so the
    edited diff cannot simply be applied on top \u2014 it would conflict with the
    changes it is meant to replace.  The changed files are therefore restored
    from the untouched destination first, and the edited diff is applied to that
    clean state.  If it does not apply, the edit is refused and the run records
    why: publishing a diff that was never applied is exactly the failure mode
    this gate exists to prevent.
    """
    from gigaforge.core.diff_validator import DiffValidator

    paths = _parse_diff_paths(edited)
    if not paths:
        return False, [], "the edited diff has no +++ headers, so it changes nothing"
    if workspace is None:
        return False, [], "no isolated workspace to apply the edited diff to"

    # Restore the files the pipeline changed, so the edited diff applies to the
    # same base the pipeline started from.
    for relative in list(changed_files) + paths:
        source = Path(workspace.destination) / relative
        target = Path(workspace.root) / relative
        try:
            if source.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(source.read_bytes())
            elif target.exists():
                target.unlink()
        except OSError as exc:
            return False, [], f"could not restore {relative!r}: {exc}"

    result = DiffValidator().validate_and_apply(
        edited, Path(root), worktree=Path(workspace.root)
    )
    if not getattr(result, "ok", False):
        return False, [], f"the edited diff did not apply: {getattr(result, 'reason', '')}"[:400]
    return True, paths, ""


def _decide(
    payload: Mapping[str, Any],
    approve: Optional[Callable[[str], bool]],
    gate: Any = None,
) -> dict[str, Any]:
    """Turn the configured gate into an ``interrupt()`` answer.

    Two shapes are accepted, and the richer one wins:

    * ``gate`` \u2014 an :class:`~gigaforge.core.approval.ApprovalGate`.  Its
      ``review`` returns an :class:`ApprovalResult`, so all four decisions
      (apply / reject / edit / abort) are reachable, and ``edit`` carries the
      human's diff back into the graph.
    * ``approve`` \u2014 the older ``Callable[[str], bool]``.  Kept because it is
      the documented signature and callers depend on it; it can only express
      apply and reject.

    A gate that raises is treated as ``abort``: a broken gate must not publish.
    """
    if gate is None and approve is None:
        return {"decision": "apply", "reason": "no gate configured"}

    diff = str(payload.get("diff") or "")
    files = ", ".join(payload.get("changed_files") or [])
    summary = f"Goal {payload.get('goal_id')} cycle {payload.get('cycle_index')}: {files}"
    prompt = f"{summary}\n\n{diff}"

    if gate is not None:
        try:
            from gigaforge.core.sync_adapter import await_blocking

            result = await_blocking(
                gate.review(
                    diff,
                    payload.get("verification") or {},
                    task_id=str(payload.get("goal_id") or ""),
                )
            )
        except Exception as exc:  # noqa: BLE001 - a broken gate must not publish
            return {"decision": "abort", "reason": f"gate raised: {exc}"}
        decision = getattr(result, "decision", None)
        decision = getattr(decision, "value", decision) or "apply"
        return {
            "decision": str(decision),
            "edited_diff": str(getattr(result, "edited_diff", "") or ""),
            "reason": str(getattr(result, "reason", "") or "gate"),
        }

    try:
        ok = bool(approve(prompt))  # type: ignore[misc]
    except Exception as exc:  # noqa: BLE001 - a broken gate must not publish
        return {"decision": "abort", "reason": f"gate raised: {exc}"}
    return {"decision": "apply" if ok else "reject", "reason": "gate"}


def run_goal_graph(
    pipeline: Any,
    *,
    repo_root: Path,
    goal: str = "",
    goal_id: Optional[str] = None,
    resume: bool = False,
    max_cycles: int = 8,
    max_fix_iterations: int = 2,
    use_subagents: bool = False,
    files: Sequence[str] = (),
    symbols: Sequence[str] = (),
    budget_tokens: int = 0,
    conversation_context: str = "",
    additional_context: str = "",
    approve: Optional[Callable[[str], bool]] = None,
    gate: Any = None,
    cancellation: Optional[CancellationToken] = None,
    verifier: Optional[Verifier] = None,
    store: Optional[GoalStore] = None,
    workspace: Optional[GoalWorkspace] = None,
    checkpointer: Any = None,
    checkpoint_path: Optional[str | Path] = None,
    emit: Optional[Callable[[str, dict[str, Any]], None]] = None,
) -> GoalResult:
    """Run (or resume) a goal on the LangGraph runtime.

    Signature-compatible with :meth:`GoalRunner.run`, so the runtime can switch
    engines without touching any caller.  ``interrupt()`` is driven here: the
    loop streams the graph, and whenever the thread suspends on the approval
    node the ``approve`` callable answers and the graph resumes.
    """
    require_langgraph()

    root = Path(repo_root).resolve()
    goal_store = store or GoalStore(root)
    max_cycles = int(max_cycles)
    if not 1 <= max_cycles <= 20:
        raise ValueError("max_cycles must be between 1 and 20")
    budget_tokens = int(budget_tokens)
    if budget_tokens < 0:
        raise ValueError("budget_tokens cannot be negative")

    saved_record: dict[str, Any] = {}
    prior_context = ""
    if resume:
        if not goal_id:
            raise ValueError("goal_id is required when resuming a goal")
        saved_record = goal_store.load(goal_id)
        prior_context = str(saved_record.get("conversation_context") or "")
        goal = str(saved_record.get("goal") or "").strip()
        if not goal:
            raise ValueError(f"goal {goal_id!r} has no saved objective")
        if saved_record.get("status") == "completed":
            return _result_from_record(saved_record)
        # On resume, zero keeps the saved lifetime cap. A larger explicit value
        # extends the total allowance; previous cycle usage remains charged.
        budget_tokens = budget_tokens or int(saved_record.get("budget_tokens") or 0)
    else:
        goal = str(goal or "").strip()
        if not goal:
            raise ValueError("goal cannot be empty")
        goal_id = goal_id or GoalStore.new_id()

    conversation_context = _merge_conversation_context(
        prior_context, conversation_context, additional_context
    )
    if resume:
        saved_record["conversation_context"] = conversation_context
        goal_store.save(saved_record)
    assert goal_id is not None
    owned_conn = None
    if checkpointer is None:
        path = checkpoint_path or (root / ".gigaforge" / "goals" / f"{goal_id}.graph.sqlite3")
        checkpointer, owned_conn = open_checkpointer(path)

    graph = build_goal_graph(
        pipeline,
        repo_root=root,
        verifier=verifier,
        store=goal_store,
        workspace=workspace,
        checkpointer=checkpointer,
        emit=emit,
        cancellation=cancellation,
    )
    config = thread_config(goal_id)

    # The classic runner provisions its verifier runtime before spending a single
    # model call; the graph path must do the same, or a goal burns its whole
    # cycle budget and only then discovers the verifier cannot run.
    preflight = _preflight(verifier)
    if preflight:
        detail = "Goal runtime is not ready: " + "; ".join(preflight)
        record = {
            "schema_version": 1,
            "goal_id": goal_id,
            "goal": goal,
            "status": "blocked",
            "accepted": False,
            "cycles": [],
            "tokens": 0,
            "budget_tokens": budget_tokens,
            "calls": 0,
            "started_at": _now(),
            "updated_at": _now(),
            "error": detail,
            "conversation_context": conversation_context,
            "workspace_root": str(getattr(workspace, "root", "") or ""),
            "published_files": [],
            "engine": "langgraph",
        }
        try:
            goal_store.save(record)
        except Exception:  # noqa: BLE001
            pass
        return _result_from_record(record)

    try:
        if resume:
            snapshot = graph_state_summary(graph, config)
            saved_cycles = list(saved_record.get("cycles") or [])
            saved_cycle_index = (
                int(saved_cycles[-1].get("index") or len(saved_cycles))
                if saved_cycles and isinstance(saved_cycles[-1], Mapping)
                else len(saved_cycles)
            )
            if snapshot["next"]:
                if conversation_context != prior_context:
                    graph.update_state(
                        config, {"conversation_context": conversation_context}
                    )
                graph_input: Any = None
            else:
                graph_input = {
                    "goal_id": goal_id,
                    "goal": goal,
                    "conversation_context": conversation_context,
                    "cycle_index": saved_cycle_index,
                    "max_cycles": max_cycles,
                    "max_fix_iterations": int(max_fix_iterations),
                    "use_subagents": bool(use_subagents),
                    "files": list(files),
                    "symbols": list(symbols),
                    "budget_tokens": budget_tokens,
                    "require_approval": approve is not None or gate is not None,
                    "cycles": saved_cycles,
                    "status": "running",
                    "accepted": False,
                    "error": "",
                    "feedback": "",
                    "no_progress": False,
                    "budget_exhausted": False,
                    "changed_files": [],
                    "published_files": list(saved_record.get("published_files") or []),
                    "tokens": int(saved_record.get("tokens") or 0),
                    "calls": int(saved_record.get("calls") or 0),
                    "started_at": str(saved_record.get("started_at") or _now()),
                    "updated_at": _now(),
                    "workspace_root": str(getattr(workspace, "root", "") or ""),
                    "pending_diff": "",
                    "verification": None,
                    "approval": {},
                }
        else:
            graph_input = {
                "goal_id": goal_id,
                "goal": goal,
                "conversation_context": conversation_context,
                "cycle_index": 0,
                "max_cycles": max_cycles,
                "max_fix_iterations": int(max_fix_iterations),
                "use_subagents": bool(use_subagents),
                "files": list(files),
                "symbols": list(symbols),
                "budget_tokens": budget_tokens,
                "require_approval": approve is not None or gate is not None,
                "cycles": [],
                "status": "running",
                "accepted": False,
                "error": "",
                "feedback": "",
                "no_progress": False,
                "budget_exhausted": False,
                "changed_files": [],
                "published_files": [],
                "tokens": 0,
                "calls": 0,
                "started_at": _now(),
                "updated_at": _now(),
                "workspace_root": str(getattr(workspace, "root", "") or ""),
                "pending_diff": "",
                "verification": None,
                "approval": {},
            }

        # Drive the graph, answering every interrupt until the thread ends.
        while True:
            if cancellation is not None and cancellation.cancelled:
                break
            for _mode, _chunk in iter_graph_events(graph, graph_input, config):
                pass
            snapshot = graph_state_summary(graph, config)
            if not snapshot["next"]:
                break
            if not snapshot["interrupts"]:
                break
            answer = _decide(snapshot["interrupts"][0].get("value") or {}, approve, gate)
            graph_input = Command(resume=answer)

        values = graph_state_summary(graph, config)["values"]
    finally:
        if owned_conn is not None:
            try:
                owned_conn.close()
            except Exception:  # noqa: BLE001
                pass

    record = {
        "schema_version": 1,
        "goal_id": goal_id,
        "goal": goal,
        "status": str(values.get("status") or "needs_attention"),
        "accepted": bool(values.get("accepted")),
        "cycles": list(values.get("cycles") or []),
        "tokens": int(values.get("tokens") or 0),
        "budget_tokens": int(values.get("budget_tokens") or 0),
        "calls": int(values.get("calls") or 0),
        "started_at": str(values.get("started_at") or ""),
        "updated_at": _now(),
        "error": str(values.get("error") or ""),
        "conversation_context": conversation_context,
        "workspace_root": str(values.get("workspace_root") or ""),
        "published_files": list(values.get("published_files") or []),
        "answer": str(values.get("answer") or ""),
        "answer_citations": list(values.get("answer_citations") or []),
        "answer_searched": bool(values.get("answer_searched")),
        "engine": "langgraph",
    }
    if record["status"] == "running":
        record["status"] = "needs_attention"
        record["error"] = record["error"] or "cycle limit reached before verification passed"
    if cancellation is not None and cancellation.cancelled and record["status"] != "completed":
        record["status"] = "cancelled"
        record["error"] = str(cancellation.reason or "cancelled")
    try:
        goal_store.save(record)
    except Exception:  # noqa: BLE001
        pass
    return _result_from_record(record)


def _preflight(verifier: Optional[Verifier]) -> tuple[str, ...]:
    """Provision the verifier's runtime, mirroring ``GoalRunner._runtime_preflight``.

    Only the default :class:`WorkspaceVerifier` needs provisioning; a custom
    verifier is the caller's responsibility.
    """
    if verifier is not None and not isinstance(verifier, WorkspaceVerifier):
        return ()
    try:
        from gigaforge.runtime_dependencies import (
            ensure_browser_runtime,
            ensure_goal_support_packages,
        )
    except Exception:  # noqa: BLE001 - a missing helper must not block a run
        return ()
    issues = list(ensure_goal_support_packages(auto_install=True, timeout_s=60))
    probe = verifier if isinstance(verifier, WorkspaceVerifier) else WorkspaceVerifier()
    if getattr(probe, "require_browser", False):
        status = ensure_browser_runtime(auto_install=True, timeout_s=180)
        if not status.available:
            issues.append("browser runtime: " + status.detail)
    return tuple(issues)


def _result_from_record(record: Mapping[str, Any]) -> GoalResult:
    """Build the same :class:`GoalResult` the classic runner returns."""
    cycles = [
        GoalCycle(
            index=int(item.get("index") or 0),
            run_id=str(item.get("run_id") or ""),
            pipeline_accepted=bool(item.get("pipeline_accepted")),
            changed_files=list(item.get("changed_files") or []),
            summary=str(item.get("summary") or ""),
            verification=item.get("verification"),
            tokens=int(item.get("tokens") or 0),
            calls=int(item.get("calls") or 0),
            duration_ms=float(item.get("duration_ms") or 0.0),
            error=str(item.get("error") or ""),
            output_fingerprint=str(item.get("output_fingerprint") or ""),
        )
        for item in (record.get("cycles") or [])
        if isinstance(item, Mapping)
    ]
    return GoalResult(
        goal_id=str(record.get("goal_id") or ""),
        goal=str(record.get("goal") or ""),
        status=str(record.get("status") or "unknown"),
        accepted=bool(record.get("accepted")),
        cycles=cycles,
        tokens=int(record.get("tokens") or 0),
        budget_tokens=int(record.get("budget_tokens") or 0),
        calls=int(record.get("calls") or 0),
        started_at=str(record.get("started_at") or ""),
        updated_at=str(record.get("updated_at") or ""),
        error=str(record.get("error") or ""),
        workspace_root=str(record.get("workspace_root") or ""),
        published_files=list(record.get("published_files") or []),
        answer=str(record.get("answer") or ""),
        answer_citations=list(record.get("answer_citations") or []),
        answer_searched=bool(record.get("answer_searched")),
    )
