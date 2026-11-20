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
    _now,
    _safe_excerpt,
)
from .goal_workspace import GoalWorkspace
from .langgraph_runtime import (
    LANGGRAPH_AVAILABLE,
    Command,
    graph_state_summary,
    interrupt,
    iter_graph_events,
    open_checkpointer,
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
    if not LANGGRAPH_AVAILABLE:  # pragma: no cover - guarded by the caller
        raise RuntimeError("langgraph is not installed")

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
                budget_tokens=int(state.get("budget_tokens") or 0),
                use_cache=False,
                dry_run=False,
                use_subagents=bool(state.get("use_subagents")),
                react=True,
                max_fix_iterations=int(state.get("max_fix_iterations") or 2),
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
        )
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
            "tokens": int(state.get("tokens") or 0) + cycle.tokens,
            "calls": int(state.get("calls") or 0) + cycle.calls,
            "updated_at": _now(),
        }

    def verify(state: GoalState) -> dict[str, Any]:
        """Deterministic project checks — the same verifier as the classic path."""
        if not state.get("accepted"):
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
        return {
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
        return {"approval": payload}

    def publish(state: GoalState) -> dict[str, Any]:
        """Publish verified files atomically, or record why we did not."""
        decision = str((state.get("approval") or {}).get("decision") or "apply")
        if decision != "apply":
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
        if state.get("status") == "failed":
            return "stop"
        if not state.get("accepted"):
            return "retry" if int(state.get("cycle_index") or 0) < int(state.get("max_cycles") or 8) else "stop"
        return "approve"

    def after_approve(state: GoalState) -> str:
        decision = str((state.get("approval") or {}).get("decision") or "apply")
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


def _decide(payload: Mapping[str, Any], approve: Optional[Callable[[str], bool]]) -> dict[str, Any]:
    """Turn the existing ``approve`` callable into an ``interrupt()`` answer."""
    if approve is None:
        return {"decision": "apply", "reason": "no gate configured"}
    diff = str(payload.get("diff") or "")
    files = ", ".join(payload.get("changed_files") or [])
    summary = f"Goal {payload.get('goal_id')} cycle {payload.get('cycle_index')}: {files}"
    try:
        ok = bool(approve(f"{summary}\n\n{diff}"))
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
    approve: Optional[Callable[[str], bool]] = None,
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
    if not LANGGRAPH_AVAILABLE:  # pragma: no cover - guarded by the caller
        raise RuntimeError("langgraph is not installed")

    root = Path(repo_root).resolve()
    goal_store = store or GoalStore(root)
    max_cycles = int(max_cycles)
    if not 1 <= max_cycles <= 20:
        raise ValueError("max_cycles must be between 1 and 20")

    if resume:
        if not goal_id:
            raise ValueError("goal_id is required when resuming a goal")
        saved = goal_store.load(goal_id)
        goal = str(saved.get("goal") or "").strip()
        if not goal:
            raise ValueError(f"goal {goal_id!r} has no saved objective")
        if saved.get("status") == "completed":
            return _result_from_record(saved)
    else:
        goal = str(goal or "").strip()
        if not goal:
            raise ValueError("goal cannot be empty")
        goal_id = goal_id or GoalStore.new_id()

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
            "calls": 0,
            "started_at": _now(),
            "updated_at": _now(),
            "error": detail,
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
            graph_input: Any = None if snapshot["next"] else {
                "goal_id": goal_id,
                "goal": goal,
                "cycle_index": 0,
                "max_cycles": max_cycles,
                "max_fix_iterations": int(max_fix_iterations),
                "use_subagents": bool(use_subagents),
                "files": list(files),
                "symbols": list(symbols),
                "budget_tokens": int(budget_tokens),
                "require_approval": approve is not None,
                "cycles": [],
                "status": "running",
                "accepted": False,
                "error": "",
                "feedback": "",
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
        else:
            graph_input = {
                "goal_id": goal_id,
                "goal": goal,
                "cycle_index": 0,
                "max_cycles": max_cycles,
                "max_fix_iterations": int(max_fix_iterations),
                "use_subagents": bool(use_subagents),
                "files": list(files),
                "symbols": list(symbols),
                "budget_tokens": int(budget_tokens),
                "require_approval": approve is not None,
                "cycles": [],
                "status": "running",
                "accepted": False,
                "error": "",
                "feedback": "",
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
            answer = _decide(snapshot["interrupts"][0].get("value") or {}, approve)
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
        "calls": int(values.get("calls") or 0),
        "started_at": str(values.get("started_at") or ""),
        "updated_at": _now(),
        "error": str(values.get("error") or ""),
        "workspace_root": str(values.get("workspace_root") or ""),
        "published_files": list(values.get("published_files") or []),
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
        calls=int(record.get("calls") or 0),
        started_at=str(record.get("started_at") or ""),
        updated_at=str(record.get("updated_at") or ""),
        error=str(record.get("error") or ""),
        workspace_root=str(record.get("workspace_root") or ""),
        published_files=list(record.get("published_files") or []),
    )
