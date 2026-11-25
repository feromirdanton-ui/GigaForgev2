"""GigaForge command-line entry point.

``gigaforge`` wires the existing layers into one tool:

* ``run``   — execute the task pipeline;
* ``report``/``trace``/``steps``/``diff``/``cost``/``cache`` — read the sinks;
* ``ui``    — live terminal dashboard;
* ``web``   — self-contained HTML control room (write or serve);
* ``chat``  — agentic chat interface over the same telemetry.

Exit codes are part of the contract, because a CI job reads them::

    0  success
    1  the run finished but was not accepted / failed
    2  there is no data to show (no run recorded, empty sink)
    3  usage or configuration error

Machine output (``--json``) is written straight to stdout, bypassing ``rich``,
so it can be piped into ``jq`` without the soft-wrapping that would break
parsing.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Annotated, Any, Optional

import typer

from gigaforge.ui import analytics as A
from gigaforge.ui import cache_ops as CACHE_OPS
from gigaforge.ui import diffview as D
from gigaforge.ui import renderers as R
from gigaforge.ui import tui as T
from gigaforge.ui import webui as W
from gigaforge.ui.chat import ChatEngine, run_repl
from gigaforge.ui.snapshot import NODE_ORDER, build_snapshot
from gigaforge.ui.store import UiStore


def _has_rich() -> bool:
    """Whether ``rich`` is importable in this deployment."""
    try:
        import rich  # noqa: F401

        return True
    except Exception:  # pragma: no cover - rich is a declared dependency
        return False


app = typer.Typer(
    name="gigaforge",
    help="GigaForge V2 — orchestration harness for the GigaChat API (Sber).",
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode="rich" if _has_rich() else None,
)

state: dict[str, Any] = {"db": None, "trace": None, "l0": None, "prices": None, "model": "GigaChat"}


def _console(*, machine: bool = False):
    """A rich console for humans, or ``None`` when emitting machine output."""
    if machine:
        return None
    try:
        from rich.console import Console

        return Console()
    except Exception:  # pragma: no cover - rich is a declared dependency
        return None


@app.callback()
def main_callback(
    db: Annotated[Optional[str], typer.Option("--db", help="Path to the core SQLite database.")] = None,
    trace: Annotated[Optional[str], typer.Option("--trace", help="Path to the JSONL trace file.")] = None,
    l0: Annotated[Optional[str], typer.Option("--l0", help="Path to the L0 index database.")] = None,
    model: Annotated[str, typer.Option("--model", help="Default model name for pricing.")] = "GigaChat",
) -> None:
    """Resolve the telemetry locations once, for every sub-command."""
    state["db"] = db
    state["trace"] = trace
    state["l0"] = l0
    state["model"] = model


def _store(repo: Optional[Path] = None) -> UiStore:
    """Build the read-only store from the resolved configuration.

    ``repo`` matters: the pipeline writes its trace under the *repository* it
    operates on (``runtime.build_runtime`` sets ``trace_path`` to
    ``<repo>/.gigaforge/traces/core.jsonl``), while this store resolved the
    default relative to the *process's* working directory.  Measured: a run
    against ``--repo /workspace/live/run3`` finished with 6 model calls and
    4751 tokens, and the dashboard printed

        ! trace file not found at .gigaforge/traces/core.jsonl
        0 tokens · 0 model calls · 0/11 nodes

    — a run that had just done real work reported as if nothing had happened.
    The store now looks where the run wrote.
    """
    kwargs: dict[str, Any] = {}
    if state.get("db"):
        kwargs["db_path"] = state["db"]
    if state.get("trace"):
        kwargs["trace_path"] = state["trace"]
    if state.get("l0"):
        kwargs["l0_db_path"] = state["l0"]
    if kwargs:
        return UiStore(**kwargs)
    try:  # prefer the pipeline's own defaults so the CLI and the app agree
        from gigaforge.core import CoreConfig

        config = CoreConfig.from_env()
        if repo is not None:
            # Same resolution the runtime uses, so the two cannot disagree.
            #
            # ``db_path`` moves with ``trace_path``.  It used to move alone, so
            # the store read the trace from the repository and the database from
            # the process's working directory: measured, ``gigaforge run --repo
            # /workspace/live/a2 --json`` finished with ``accepted: True`` and
            # two files changed, and the snapshot it printed said
            #
            #     "status": "unknown", "accepted": false,
            #     "totals": {"runs_recorded": 0, ...}
            #
            # because it was reading a different database.  A report that
            # contradicts the run it is reporting on is worse than no report.
            config = replace(
                config,
                repo_root=str(repo),
                db_path=str(Path(repo) / ".gigaforge" / "core.sqlite3"),
                trace_path=str(Path(repo) / ".gigaforge" / "traces" / "core.jsonl"),
            )
        return UiStore.from_config(config)
    except Exception:
        return UiStore()


def _prices() -> Any:
    if state.get("prices") is None:
        state["prices"] = A.price_table()
    return state["prices"]


def _snapshot(store: UiStore, run_id: Optional[str]) -> dict[str, Any]:
    return build_snapshot(store, run_id=run_id, prices=_prices(), default_model=state["model"])


def _emit(text: str, *, machine: bool = False) -> None:
    """Print, choosing a stream that cannot corrupt machine output."""
    if machine:
        R.write_machine(text)
        return
    console = _console()
    if console is None:
        R.write_machine(text)
    else:
        console.print(text, markup=False, highlight=False)


def _no_data(store: UiStore, *, machine: bool = False) -> None:
    note = (
        f"no telemetry to show (db={store.db_present}, trace={store.trace_present}).\n"
        "Run the pipeline once (gigaforge run \"<goal>\") or point --db/--trace at an existing sink."
    )
    _emit(note, machine=machine)


# ══════════════════════════════════════════════════════════════════════════
# run
# ══════════════════════════════════════════════════════════════════════════
def _build_pipeline(repo: Optional[Path] = None) -> Any:
    """Construct the real pipeline through the composition root.

    The CLI used to build a ``GigaClient`` with no token provider and hand a
    ``TaskPipeline`` no client at all, so every run failed before its first node.
    ``gigaforge.runtime.build_runtime`` is now the only construction path, and it
    is the same one the desktop window uses — a difference between the two
    front ends can no longer hide a wiring bug in one of them.
    """
    try:
        from gigaforge.runtime import build_runtime
    except Exception as exc:
        _emit(f"cannot import the harness runtime: {exc}")
        raise typer.Exit(R.EXIT_USAGE)

    try:
        runtime = build_runtime(
            require_provider=True,
            repo_root=str(repo) if repo else None,
        )
    except Exception as exc:
        _emit(
            f"no model provider is available ({type(exc).__name__}: {exc}).\n"
            "Set GIGAFORGE_AUTH_KEY (base64 client_id:client_secret) in the environment or "
            ".env, or use `gigaforge report` to read an existing run."
        )
        raise typer.Exit(R.EXIT_USAGE)
    state["runtime"] = runtime
    return runtime.pipeline


@app.command()
def run(
    goal: Annotated[str, typer.Argument(help="The task to accomplish.")],
    repo: Annotated[Optional[Path], typer.Option("--repo", "-r", help="Repository root to operate on (default: cwd).")] = None,
    files: Annotated[Optional[list[str]], typer.Option("--file", "-f", help="Focus the slice on these files.")] = None,
    symbols: Annotated[Optional[list[str]], typer.Option("--symbol", "-s", help="Focus the slice on these symbols.")] = None,
    budget_tokens: Annotated[int, typer.Option("--budget", help="Hard token budget for the run (0 = unlimited).")] = 0,
    no_cache: Annotated[bool, typer.Option("--no-cache", help="Bypass the result cache.")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Validate without writing to the workspace.")] = False,
    resume: Annotated[bool, typer.Option("--resume", help="Resume from the last checkpoint.")] = False,
    max_fix_iterations: Annotated[Optional[int], typer.Option("--max-fix-iterations", help="Cap the repair loop.")] = None,
    report_out: Annotated[Optional[str], typer.Option("--report-out", help="Write a JSON run report here.")] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Emit the snapshot as JSON (pipe-safe).")] = False,
    dashboard: Annotated[bool, typer.Option("--dashboard/--no-dashboard", help="Print the dashboard after the run.")] = True,
) -> None:
    """Run the pipeline end to end and show the resulting telemetry."""
    pipeline = _build_pipeline(repo)
    try:
        result = pipeline.run(
            goal,
            files=tuple(files or ()),
            symbols=tuple(symbols or ()),
            budget_tokens=int(budget_tokens),
            use_cache=not no_cache,
            dry_run=bool(dry_run),
            resume=bool(resume),
            max_fix_iterations=max_fix_iterations,
        )
    except Exception as exc:  # noqa: BLE001 - surfaced as an exit code
        _emit(f"run failed: {type(exc).__name__}: {exc}", machine=as_json)
        raise typer.Exit(R.EXIT_FAILED)
    finally:
        runtime = state.get("runtime")
        closer = getattr(runtime, "aclose", None)
        if callable(closer):
            closer()

    run_id = str(getattr(result, "run_id", "") or "")
    report = None
    to_dict = getattr(result, "to_dict", None)
    if callable(to_dict):
        try:
            report = to_dict()
        except Exception:  # pragma: no cover - defensive
            report = None

    if report_out:
        target = Path(report_out)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = report if report is not None else {"run_id": run_id}
        target.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        if not as_json:
            _emit(f"wrote run report to {target}")

    store = _store(repo)
    snapshot = _snapshot(store, run_id or None)
    if as_json:
        # ``--json`` is the machine-readable *result*, not the dashboard.
        # It used to emit the UI snapshot, whose keys are ``run``/``kpis``/
        # ``steps`` — so a caller reading ``status`` or ``accepted`` from
        # ``run --json`` got ``None`` for both, and a script could not tell a
        # successful run from a failed one.  The snapshot is still available,
        # nested, for callers that want the telemetry.
        payload = dict(report or {})
        payload.setdefault("run_id", run_id)
        payload.setdefault("status", str(getattr(result, "status", "") or ""))
        payload.setdefault("accepted", bool(getattr(result, "accepted", False)))
        payload["snapshot"] = snapshot
        _emit(json.dumps(payload, indent=2, ensure_ascii=False, default=str), machine=True)
    elif dashboard:
        T.print_dashboard(snapshot, diff_lines=20)

    accepted = bool(getattr(result, "accepted", False))
    status = str(getattr(result, "status", "") or "")
    if not as_json:
        _emit(f"run {run_id}: status={status or 'unknown'} accepted={accepted}")
    raise typer.Exit(R.EXIT_OK if accepted and status != "failed" else R.EXIT_FAILED)


@app.command()
def goal(
    task: Annotated[Optional[str], typer.Argument(help="The objective to pursue. Omit when resuming.")] = None,
    repo: Annotated[Optional[Path], typer.Option("--repo", "-r", help="Repository root (default: cwd).")] = None,
    resume: Annotated[Optional[str], typer.Option("--resume", help="Resume a saved goal by ID.")] = None,
    goal_id: Annotated[Optional[str], typer.Option("--goal-id", help="Choose an ID for this new goal.")] = None,
    max_cycles: Annotated[int, typer.Option("--max-cycles", min=1, max=20, help="Author/verify cycles per invocation.")] = 8,
    max_fix_iterations: Annotated[int, typer.Option("--max-fix-iterations", min=1, max=10, help="In-cycle repair attempts.")] = 2,
    budget_tokens: Annotated[int, typer.Option("--budget", help="Lifetime token cap across goal cycles (0 = unlimited). ")] = 0,
    files: Annotated[Optional[list[str]], typer.Option("--file", "-f", help="Focus on these files.")] = None,
    use_subagents: Annotated[bool, typer.Option("--subagents/--no-subagents", help="Delegate implementation into isolated sub-agent worktrees.")] = False,
    static_only: Annotated[bool, typer.Option("--static-only", help="Allow HTML goals to complete without a browser runtime.")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Emit the durable goal report as JSON.")] = False,
) -> None:
    """Pursue a goal through repeated implementation, execution and repair cycles."""
    if resume and goal_id:
        _emit("use either --resume or --goal-id, not both", machine=as_json)
        raise typer.Exit(R.EXIT_USAGE)

    _build_pipeline(repo)
    runtime = state.get("runtime")
    try:
        from gigaforge.core.goal import WorkspaceVerifier

        result = runtime.run_goal(
            task or "",
            goal_id=resume or goal_id,
            resume=bool(resume),
            max_cycles=max_cycles,
            max_fix_iterations=max_fix_iterations,
            use_subagents=use_subagents,
            files=tuple(files or ()),
            budget_tokens=budget_tokens,
            verifier=WorkspaceVerifier(require_browser=not static_only),
        )
    except Exception as exc:  # noqa: BLE001 - keep a failed goal inspectable
        _emit(f"goal failed: {type(exc).__name__}: {exc}", machine=as_json)
        raise typer.Exit(R.EXIT_FAILED)
    finally:
        closer = getattr(runtime, "aclose", None)
        if callable(closer):
            closer()

    if as_json:
        _emit(json.dumps(result.to_dict(), indent=2, ensure_ascii=False), machine=True)
    else:
        _emit(
            f"goal {result.goal_id}: status={result.status} accepted={result.accepted} "
            f"cycles={len(result.cycles)} tokens={result.tokens} calls={result.calls}"
        )
        for cycle in result.cycles:
            _emit(f"  cycle {cycle.index} ({cycle.run_id}): pipeline={cycle.pipeline_accepted}")
            for check in (cycle.verification or {}).get("checks", []):
                _emit(f"    {check['status']}: {check['name']} — {check['summary']}")
        if result.published_files:
            _emit("  published: " + ", ".join(result.published_files))
        elif result.workspace_root and result.status != "completed":
            _emit(f"  isolated workspace kept for resume: {result.workspace_root}")
        if result.error:
            _emit(f"  detail: {result.error}")
        _emit(f"  resume with: gigaforge goal --resume {result.goal_id}")
    raise typer.Exit(R.EXIT_OK if result.ok else R.EXIT_FAILED)


# ══════════════════════════════════════════════════════════════════════════
# read-only views
# ══════════════════════════════════════════════════════════════════════════
@app.command()
def report(
    run_id: Annotated[Optional[str], typer.Option("--run-id", help="Report on a specific run.")] = None,
    fmt: Annotated[str, typer.Option("--format", "-o", help="console | markdown | json")] = "console",
    out: Annotated[Optional[str], typer.Option("--out", help="Write the report to this file.")] = None,
    diff_lines: Annotated[int, typer.Option("--diff-lines", help="Diff lines to include.")] = 30,
) -> None:
    """Render the run report as console text, Markdown or JSON."""
    store = _store()
    machine = fmt.lower() == "json"
    if not store.db_present and not store.trace_present:
        _no_data(store, machine=machine)
        raise typer.Exit(R.EXIT_NO_DATA)
    snapshot = _snapshot(store, run_id)
    if fmt.lower() in {"md", "markdown"}:
        body = R.render_markdown(snapshot, diff_lines=diff_lines)
    elif machine:
        body = R.render_json(snapshot)
    else:
        body = R.render_console(snapshot, diff_lines=diff_lines)
    if out:
        target = Path(out)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
        _emit(f"wrote {target} ({fmt})", machine=False)
        return
    _emit(body, machine=machine)


@app.command()
def trace(
    run_id: Annotated[Optional[str], typer.Option("--run-id", help="Only this run's events.")] = None,
    filter_text: Annotated[Optional[str], typer.Option("--filter", help="Substring filter across event fields.")] = None,
    limit: Annotated[int, typer.Option("--limit", help="Show at most this many events.")] = 60,
    as_json: Annotated[bool, typer.Option("--json", help="Emit JSON (pipe-safe).")] = False,
) -> None:
    """View the JSONL trace: the ordered event stream of a run."""
    store = _store()
    if not store.trace_present:
        _no_data(store, machine=as_json)
        raise typer.Exit(R.EXIT_NO_DATA)
    events = store.trace_events(run_id=run_id)
    if filter_text:
        needle = filter_text.lower()
        events = [e for e in events if needle in json.dumps(e, default=str).lower()]
    if as_json:
        _emit(R.render_json({"count": len(events), "events": events[-limit:]}), machine=True)
    else:
        counts = store.trace_counts(run_id)
        lines = [f"trace: {len(events)} event(s)"]
        if counts:
            lines.append("counts: " + ", ".join(f"{k}={v}" for k, v in counts.items()))
        for event in events[-limit:]:
            detail = " · ".join(
                str(v) for v in (
                    event.get("node"), event.get("phase"), event.get("model"),
                    event.get("status"), event.get("code"), event.get("error"),
                ) if v
            )
            lines.append(f"{event.get('ts') or '-':<22} {str(event.get('event') or '?'):<22} {detail}")
        _emit("\n".join(lines))
    if not events:
        raise typer.Exit(R.EXIT_NO_DATA)


@app.command()
def steps(
    run_id: Annotated[Optional[str], typer.Option("--run-id", help="Steps for a specific run.")] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Emit JSON (pipe-safe).")] = False,
) -> None:
    """Show the node timeline: status, duration and attempts per node."""
    store = _store()
    snapshot = _snapshot(store, run_id)
    timeline = snapshot.get("steps") or []
    if as_json:
        _emit(R.render_json({"node_order": list(NODE_ORDER), "steps": timeline, "progress": snapshot.get("progress")}), machine=True)
        return
    if not timeline:
        _no_data(store)
        raise typer.Exit(R.EXIT_NO_DATA)
    progress = snapshot.get("progress") or {}
    lines = [f"progress {progress.get('done', 0)}/{progress.get('total', 0)} ({progress.get('percent', 0)}%)"]
    for step in timeline:
        lines.append(
            f"  {str(step.get('status')):<10} {str(step.get('node')):<11} "
            f"{A.human_duration(step.get('duration_ms')):>10}"
            + (f"  ×{step.get('attempts')}" if A.as_int(step.get("attempts"), 1) > 1 else "")
            + (f"  {step.get('error_msg')}" if step.get("error_msg") else "")
        )
    _emit("\n".join(lines))


@app.command()
def diff(
    run_id: Annotated[Optional[str], typer.Option("--run-id", help="Diff for a specific run.")] = None,
    files_only: Annotated[bool, typer.Option("--files-only", help="List changed files without the hunks.")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Emit JSON (pipe-safe).")] = False,
) -> None:
    """View the run's final diff with per-file added/removed counts."""
    store = _store()
    snapshot = _snapshot(store, run_id)
    payload = snapshot.get("diff") or {}
    records = D.diff_summary(str(payload.get("text") or ""))
    if as_json:
        _emit(R.render_json({
            "files": records["files"],
            "actions": records["actions"],
            "hunks": records["hunks"],
            "added": records["added"],
            "removed": records["removed"],
        }), machine=True)
        return
    if not records["files"]:
        _no_data(store)
        raise typer.Exit(R.EXIT_NO_DATA)
    lines = [f"{records['file_count']} file(s) · {records['hunks']} hunks · "
             f"+{records['added']} / −{records['removed']}"]
    for record in records["files"]:
        lines.append(f"  {record['action']:<9} {record['path']}  +{record['added']}/−{record['removed']}")
    if not files_only:
        lines.append("")
        lines.extend(D.split_lines(str(payload.get("text") or ""))[:200])
    _emit("\n".join(lines))


@app.command()
def cost(
    run_id: Annotated[Optional[str], typer.Option("--run-id", help="Cost for a specific run.")] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Emit JSON (pipe-safe).")] = False,
) -> None:
    """Show token usage and cost, total and per phase."""
    store = _store()
    snapshot = _snapshot(store, run_id)
    kpis = snapshot.get("kpis") or {}
    if as_json:
        _emit(R.render_json({
            "tokens": kpis.get("tokens"),
            "cost": kpis.get("cost"),
            "latency": kpis.get("latency"),
            "phases": snapshot.get("phases"),
            "models": snapshot.get("models"),
        }), machine=True)
        return
    tokens = kpis.get("tokens") or {}
    money = kpis.get("cost") or {}
    if not A.as_int(tokens.get("total")) and not A.as_int(money.get("micros")):
        _no_data(store)
        raise typer.Exit(R.EXIT_NO_DATA)
    lines = [
        f"tokens {A.as_int(tokens.get('total')):,} "
        f"(prompt {A.as_int(tokens.get('prompt')):,} / completion {A.as_int(tokens.get('completion')):,}) "
        f"over {A.as_int(tokens.get('calls')):,} calls",
        f"total {money.get('display') or A.human_usd(money.get('micros'))}",
    ]
    for phase in snapshot.get("phases") or []:
        lines.append(
            f"  {str(phase.get('phase')):<10} {str(phase.get('model') or '-'):<14} "
            f"calls={A.as_int(phase.get('calls')):<4} tokens={A.as_int(phase.get('tokens')):>8,} "
            f"cost={A.human_usd(phase.get('cost_micros'))} p95={A.human_duration(phase.get('latency_p95_ms'))}"
        )
    _emit("\n".join(lines))


@app.command()
def cache(
    purge: Annotated[bool, typer.Option("--purge", help="Delete cache entries (a write operation).")] = False,
    namespace: Annotated[Optional[str], typer.Option("--namespace", help="Restrict to one namespace.")] = None,
    older_than: Annotated[Optional[str], typer.Option("--older-than", help="Age filter, e.g. 7d / 12h / 30m.")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Count what would be deleted, delete nothing.")] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Confirm the deletion (required).")] = False,
    vacuum: Annotated[bool, typer.Option("--vacuum", help="Reclaim space after a purge.")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Emit JSON (pipe-safe).")] = False,
) -> None:
    """Inspect the cache, or purge it with an explicit confirmation."""
    store = _store()
    if purge:
        # Mutation is a separate path from every read command, and it requires
        # an explicit --yes: an accidental `cache --purge` must not lose data.
        if not yes and not dry_run:
            _emit("refusing to purge without --yes (use --dry-run to preview)", machine=as_json)
            raise typer.Exit(R.EXIT_USAGE)
        try:
            outcome = CACHE_OPS.purge(
                state.get("db") or store.db_path,
                namespace=namespace,
                older_than=older_than,
                dry_run=bool(dry_run),
            )
            if vacuum and not dry_run:
                outcome["vacuum"] = CACHE_OPS.vacuum(state.get("db") or store.db_path)
        except (OSError, ValueError) as exc:
            _emit(f"purge failed: {exc}", machine=as_json)
            raise typer.Exit(R.EXIT_USAGE)
        if as_json:
            _emit(R.render_json(outcome), machine=True)
        else:
            verb = "would delete" if outcome["dry_run"] else "deleted"
            _emit(
                f"{verb} {outcome['deleted']} of {outcome['matched']} entr(ies), "
                f"freeing {A.human_bytes(outcome['freed_bytes'])}"
                + (f" (vacuum reclaimed {A.human_bytes(outcome['vacuum']['reclaimed'])})" if outcome.get("vacuum") else "")
            )
        return

    summary = store.cache_summary()
    if as_json:
        _emit(R.render_json({"summary": summary, "entries": store.cache_entries(limit=200)}), machine=True)
        return
    lines = [
        f"hit rate {A.as_float(summary.get('hit_rate')) * 100:.1f}%  "
        f"hits {summary.get('hits')} · misses {summary.get('misses')} · writes {summary.get('writes')}",
        f"entries {summary.get('entries')} · size {A.human_bytes(summary.get('bytes'))}",
    ]
    namespaces = summary.get("namespaces") or {}
    if namespaces:
        lines.append("namespaces: " + ", ".join(f"{k}={v}" for k, v in namespaces.items()))
    entries = store.cache_entries(limit=25)
    if entries:
        lines.append("")
        lines.append(f"{'created':<21}{'namespace':<12}{'type':<10}{'model':<16}{'hits':>5}{'bytes':>9}")
        for entry in entries:
            lines.append(
                f"{str(entry.get('created_at') or '-')[:19]:<21}{str(entry.get('namespace') or '-'):<12}"
                f"{str(entry.get('value_type') or '-'):<10}{str(entry.get('model') or '-'):<16}"
                f"{A.as_int(entry.get('hits')):>5}{A.as_int(entry.get('bytes')):>9}"
            )
    _emit("\n".join(lines))


@app.command()
def paths(
    as_json: Annotated[bool, typer.Option("--json", help="Emit JSON (pipe-safe).")] = False,
) -> None:
    """Print the resolved telemetry locations (useful when a panel looks empty)."""
    store = _store()
    payload = {
        "db_path": store.db_path,
        "db_present": store.db_present,
        "trace_path": store.trace_path,
        "trace_present": store.trace_present,
        "l0_db_path": store.l0_db_path,
        "tables": store.tables(),
        "l0_tables": store.tables(l0=True),
    }
    if as_json:
        _emit(R.render_json(payload), machine=True)
        return
    lines = [f"{k}: {v}" for k, v in payload.items() if k != "tables"]
    lines.append("tables: " + ", ".join(payload["tables"]) if payload["tables"] else "tables: (none)")
    _emit("\n".join(lines))


# ══════════════════════════════════════════════════════════════════════════
# interfaces
# ══════════════════════════════════════════════════════════════════════════
@app.command()
def ui(
    run_id: Annotated[Optional[str], typer.Option("--run-id", help="Dashboard for a specific run.")] = None,
    watch: Annotated[bool, typer.Option("--watch/--once", help="Repaint continuously or render once.")] = False,
    interval: Annotated[float, typer.Option("--interval", help="Seconds between repaints.")] = 1.0,
    iterations: Annotated[Optional[int], typer.Option("--iterations", help="Stop after N repaints (tests).")] = None,
    diff_lines: Annotated[int, typer.Option("--diff-lines", help="Diff lines to show.")] = 24,
    plain: Annotated[bool, typer.Option("--plain", help="Plain text output (no rich, no colour).")] = False,
) -> None:
    """Open the terminal dashboard (TUI) for a run."""
    store = _store()
    if not store.db_present and not store.trace_present:
        _no_data(store)
        raise typer.Exit(R.EXIT_NO_DATA)
    if watch:
        T.watch(
            store,
            run_id=run_id,
            interval=interval,
            iterations=iterations,
            diff_lines=diff_lines,
            plain=plain,
            prices=_prices(),
            default_model=state["model"],
        )
        return
    T.print_dashboard(_snapshot(store, run_id), diff_lines=diff_lines, plain=plain)


@app.command()
def web(
    out: Annotated[Optional[str], typer.Option("--out", help="Write a portable HTML panel to this path.")] = None,
    serve: Annotated[bool, typer.Option("--serve", help="Serve the panel over HTTP instead of writing a file.")] = False,
    host: Annotated[str, typer.Option("--host", help="Bind address.")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", help="Port to bind.")] = 8787,
    open_browser: Annotated[bool, typer.Option("--open", help="Open the panel in a browser.")] = False,
    run_id: Annotated[Optional[str], typer.Option("--run-id", help="Panel for a specific run.")] = None,
) -> None:
    """Build or serve the self-contained HTML control room."""
    store = _store()
    if not store.db_present and not store.trace_present:
        _no_data(store)
        raise typer.Exit(R.EXIT_NO_DATA)
    if serve:
        local_bind = W.is_loopback_host(host)
        engine = _chat_engine(store) if local_bind else None
        if not local_bind:
            _emit("network control room is read-only; chat is restricted to loopback")
        handler = W.make_handler(
            store,
            run_id=run_id,
            prices=_prices(),
            default_model=state["model"],
            chat_handler=None if engine is None else engine_handler(engine),
        )
        from http.server import ThreadingHTTPServer

        httpd = ThreadingHTTPServer((host, port), handler)
        _emit(f"GigaForge control room on http://{host}:{port}/  (Ctrl-C to stop)")
        if open_browser:  # pragma: no cover - interactive
            import threading
            import webbrowser

            threading.Timer(0.4, lambda: webbrowser.open(f"http://{host}:{port}/")).start()
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:  # pragma: no cover - interactive
            pass
        finally:
            httpd.server_close()
        return

    target = Path(out or "gigaforge-panel.html")
    path = W.write_html(store, target, run_id=run_id, prices=_prices(), default_model=state["model"])
    snapshot = _snapshot(store, run_id)
    _emit(
        f"wrote {path} — self-contained panel "
        f"({A.as_int((snapshot.get('kpis') or {}).get('tokens', {}).get('total')):,} tokens, "
        f"{len(snapshot.get('steps') or [])} steps)"
    )


def engine_handler(engine: ChatEngine):
    from gigaforge.ui.chat import build_chat_handler

    return build_chat_handler(engine)


def _engine(
    store: UiStore,
    *,
    session_id: Optional[str] = None,
    offline: bool = False,
    repo: Optional[Path] = None,
) -> ChatEngine:
    """Build a chat engine whose backend drives the *real* composed pipeline.

    Previously this called ``default_engine`` without a pipeline, so the backend
    built a ``TaskPipeline`` with no model client: slash commands worked and every
    goal failed.  The engine now comes from the runtime, which owns the client.
    """
    from gigaforge.runtime import build_runtime
    from gigaforge.ui.chat import default_engine

    try:
        runtime = build_runtime(repo_root=str(repo) if repo else None)
    except Exception:  # noqa: BLE001 - chat must still start (scripted/commands only)
        runtime = None
    if runtime is not None:
        state["runtime"] = runtime
        return runtime.chat_engine(
            session_id=session_id,
            prices=_prices(),
            default_model=state["model"],
            offline=offline,
        )
    return default_engine(
        store,
        session_id=session_id,
        prices=_prices(),
        default_model=state["model"],
        offline=offline,
    )


def _chat_engine(store: UiStore) -> Optional[ChatEngine]:
    try:
        return _engine(store)
    except Exception:  # pragma: no cover - chat is optional
        return None


@app.command()
def chat(
    message: Annotated[Optional[str], typer.Option("--message", "-m", help="Send one message and exit (no REPL).")] = None,
    repo: Annotated[Optional[Path], typer.Option("--repo", "-r", help="Repository root to operate on (default: cwd).")] = None,
    session_id: Annotated[Optional[str], typer.Option("--session-id", help="Resume a specific transcript.")] = None,
    offline: Annotated[bool, typer.Option("--offline", help="Use the scripted backend (no model calls).")] = False,
    loop: Annotated[bool, typer.Option("--loop", help="Keep working through a goal queue (long-running mode).")] = False,
    queue: Annotated[Optional[Path], typer.Option("--queue", help="Goal queue file for --loop (one goal per line).")] = None,
    once: Annotated[bool, typer.Option("--once", help="With --loop: drain the queue and exit instead of watching it.")] = False,
    max_goals: Annotated[int, typer.Option("--max-goals", help="With --loop: stop after N goals (0 = unlimited).")] = 0,
    as_json: Annotated[bool, typer.Option("--json", help="Emit events as JSON lines.")] = False,
) -> None:
    """Talk to the harness: type a goal, or a /command, and watch it work."""
    store = _store()
    engine = _engine(store, session_id=session_id, offline=offline, repo=repo)
    if loop:
        from gigaforge.ui.loop import run_queue

        code = run_queue(engine, repo=repo, queue_path=queue, once=once,
                         max_goals=max_goals, as_json=as_json, emit=_emit)
        raise typer.Exit(code)
    if message is not None:
        events = engine.handle(message)
        if as_json:
            R.write_machine("\n".join(json.dumps(e.to_dict(), ensure_ascii=False, default=str) for e in events))
        else:
            from gigaforge.ui.chat import render_event

            for event in events:
                line = render_event(event)
                if line:
                    _emit(line)
        return
    if as_json:
        _emit("--json requires --message", machine=True)
        raise typer.Exit(R.EXIT_USAGE)
    run_repl(engine)


@app.command()
def doctor(
    repo: Annotated[Optional[Path], typer.Option("--repo", "-r", help="Path to repo to check.")] = None,
    offline: Annotated[bool, typer.Option("--offline", help="Skip OAuth network check.")] = False,
    fix: Annotated[bool, typer.Option("--fix", help="Apply safe auto-fixes (.env template).")] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Diagnose the local environment: GigaForge component health checks."""
    from gigaforge.doctor import run_doctor

    code = run_doctor(repo=repo, offline=offline, fix=fix)
    raise typer.Exit(code)


@app.command()
def stack() -> None:
    """Show the stack this install will actually run.

    The merge's recurring defect was a capability that exists, is tested, and is
    bypassed by the path the product takes.  Documentation cannot catch that: the
    README described the merged stack while a default install ran the pre-merge
    one.  This command resolves the answer from the SAME functions the runtime
    calls, so it cannot drift from behaviour.
    """
    import importlib.util
    import os

    from gigaforge.agents.engine import react_engine, subagent_engine
    from gigaforge.core.strategies import build_plan_strategy, build_review_strategy

    def _have(mod: str) -> str:
        return "installed" if importlib.util.find_spec(mod) else "absent"

    def _env(key: str, default: str) -> str:
        return os.environ.get(key) or default

    typer.echo("GigaForge stack")
    typer.echo("-" * 46)
    typer.echo(f"  provider        {_env('GIGAFORGE_PROVIDER', 'auto')}")
    typer.echo(f"  plan strategy   {build_plan_strategy().name}")
    typer.echo(f"  review strategy {build_review_strategy().name}")
    typer.echo(f"  react engine    {react_engine()}")
    typer.echo(f"  subagent engine {subagent_engine()}")
    # Print the profile that will actually be used, resolved by the same
    # function the runtime calls — not the raw env var, which may name a
    # profile that does not exist.
    from gigaforge.harness.profiles import resolve_profile

    _profile = resolve_profile()
    typer.echo(f"  harness profile {_profile.name} — {_profile.description}")
    typer.echo(
        f"  writes          {'allowed' if _profile.allow_writes else 'refused'}"
        f" | steps {_profile.max_steps} | cache {_profile.observation_cache}"
    )
    # Read the SAME objects the runtime reads.  The stack command used to print
    # ``os.environ`` defaults, so it advertised ``react budget 8000 out`` while
    # the runner sent ``max_tokens=1000`` — a diagnostic that disagrees with the
    # code is worse than none, because it is believed.
    from gigaforge.config import get_settings
    from gigaforge.core.context_window_guard import ContextWindowGuard

    settings = get_settings()
    typer.echo(f"  compact at      {ContextWindowGuard().trigger_fraction}")
    typer.echo(
        f"  react budget    {settings.budget_react_in} in / "
        f"{settings.budget_react_out} out"
    )
    typer.echo("-" * 46)
    typer.echo(f"  gigachat SDK    {_have('gigachat')}")
    typer.echo(f"  langgraph       {_have('langgraph')}")
    typer.echo(f"  deepagents      {_have('deepagents')}")
    typer.echo("-" * 46)
    typer.echo("  live probe      python -m gigaforge.harness.live_probe")


@app.command()
def models(verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False) -> None:
    """List available GigaChat models (live API call)."""
    from gigaforge.client.errors import AuthError, UpstreamError
    from gigaforge.runtime import build_runtime

    rt = None
    try:
        rt = build_runtime(require_provider=True)
        ms = rt.client.list_models()
    except UpstreamError as e:
        typer.echo(f"upstream error: {e}", err=True)
        raise typer.Exit(1) from e
    except AuthError as e:
        typer.echo(f"auth error: {e}", err=True)
        raise typer.Exit(3) from e
    except Exception as e:
        typer.echo(f"error[{type(e).__name__}]: {e}", err=True)
        raise typer.Exit(1) from e
    finally:
        if rt is not None:
            rt.aclose()

    for m in ms:
        typer.echo(f"{m.get('id')}\t{m.get('owned_by', 'GigaChat')}")


@app.command()
def smoke(verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False) -> None:
    """Live connectivity smoke test against GigaChat (OAuth + models + 1 chat)."""
    from gigaforge.client.errors import AuthError, UpstreamError
    from gigaforge.runtime import build_runtime

    rt = None
    try:
        rt = build_runtime(require_provider=True)
        rt.token_provider.get_token()
        typer.echo("[1/3] OAuth ok")

        ms = rt.client.list_models()
        typer.echo(f"[2/3] models: {len(ms)} available")
        for m in ms[:5]:
            typer.echo(f"      - {m.get('id')}")

        route = rt.pipeline.router.route("summarize")
        res = rt.client.complete(
            [{"role": "user", "content": "Say 'pong' and nothing else."}],
            model=route.model,
            temperature=0.0,
            max_tokens=10,
            task_id="smoke",
            phase="summarize",
        )
        typer.echo(f"[3/3] chat ok: {res.content!r} usage={res.usage}")
    except AuthError as e:
        typer.echo(
            f"auth error: {e}\n"
            "hint: if hitting a TLS trust issue with the Sber corp CA, try "
            "GIGAFORGE_TLS_VERIFY_OFF=1 (dev/test only — disables TLS verification).",
            err=True,
        )
        raise typer.Exit(3) from e
    except UpstreamError as e:
        typer.echo(f"upstream error: {e}", err=True)
        raise typer.Exit(1) from e
    except Exception as e:
        typer.echo(f"error[{type(e).__name__}]: {e}", err=True)
        raise typer.Exit(1) from e
    finally:
        if rt is not None:
            rt.aclose()

    raise typer.Exit(0)


@app.command()
def index(
    repo: Annotated[Path, typer.Argument(help="Path to the repo to index.")],
    full: Annotated[
        bool,
        typer.Option(
            "--full",
            help="Force a full reindex (skip the mtime/sha cache). Use after schema changes.",
        ),
    ] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Build / refresh the L0 symbol index for a repo (0 LLM calls)."""
    from gigaforge.l0.index import L0Index

    repo_path = repo.resolve()
    if not repo_path.is_dir():
        typer.echo(f"error: {repo_path} is not an existing directory", err=True)
        raise typer.Exit(2)
    idx = L0Index()
    try:
        if full:
            n = idx.index_repo(repo_path)
            typer.echo(f"indexed {n} file(s) (full reindex) in {repo_path}")
        else:
            n = idx.incremental_update(repo_path)
            typer.echo(
                f"indexed {n} changed file(s) in {repo_path} "
                f"(use --full for a complete reindex)"
            )
    finally:
        idx.close()


@app.command()
def mcp(
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Launch the GigaForge MCP (Model Context Protocol) server over stdio."""
    import asyncio

    from gigaforge.mcp.server import run_stdio

    asyncio.run(run_stdio())


@app.command()
def bench(
    as_json: Annotated[bool, typer.Option("--json", help="Emit JSON report.")] = False,
    out: Annotated[
        str,
        typer.Option("--out", help="Write the JSON report to this path (for CI)."),
    ] = "",
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
    repeats: Annotated[
        int,
        typer.Option("--repeats", "-k", help="Independent attempts per task (pass@k)."),
    ] = 1,
    live: Annotated[
        bool,
        typer.Option(
            "--live",
            help="Use the real provider instead of the scripted fake (nightly).",
        ),
    ] = False,
) -> None:
    """Run the offline benchmark suite across tasks.

    ``--out PATH`` writes the JSON report to a file and exits non-zero if any
    task failed, so CI can gate on the bench instead of parsing stdout.  The
    bench was not wired to CI at all, and it contained no HTML task, so a
    regression that broke page generation outright left every check green.

    ``--repeats k`` runs each task k times and reports ``pass_at_k``.  A single
    run of a stochastic pipeline is a coin flip reported as a fact: "5/5 passed"
    cannot distinguish a harness that works every time from one that works four
    times in five, and that difference is the whole question.
    """
    from gigaforge.bench import BenchRunner, bench_report, bench_report_json

    runner = BenchRunner(repeats=repeats, live=live)
    results = runner.run_all()
    if out:
        import json as _json
        from pathlib import Path

        # ``bench_report_json`` returns a LIST of rows, not a string.  Writing it
        # directly raised ``TypeError: data must be str, not list`` — and the
        # ``--json`` path printed a Python repr, which is not JSON.  Both are
        # fixed here, in the one place that serialises.
        Path(out).write_text(
            _json.dumps(bench_report_json(results), indent=2), encoding="utf-8"
        )
        failed = [r for r in results if not r.success]
        typer.echo(f"{len(results) - len(failed)}/{len(results)} bench tasks passed")
        for row in failed:
            typer.echo(f"  FAIL {row.name}: {row.error[:200]}")
        if failed:
            raise typer.Exit(code=1)
        return
    if as_json:
        import json as _json

        typer.echo(_json.dumps(bench_report_json(results), indent=2))
    else:
        typer.echo(bench_report(results))


# ══════════════════════════════════════════════════════════════════════════
# desktop application
# ══════════════════════════════════════════════════════════════════════════
@app.command()
def desktop(
    repo: Annotated[Optional[Path], typer.Option("--repo", "-r", help="Repository root to operate on.")] = None,
    view: Annotated[Optional[str], typer.Option("--view", help="dashboard | forge | files | agents | monitoring | settings")] = None,
    offline: Annotated[bool, typer.Option("--offline", help="Scripted chat backend, no model calls.")] = False,
    require_provider: Annotated[bool, typer.Option("--require-provider", help="Refuse to open without a credential.")] = False,
    ui: Annotated[str, typer.Option("--ui", help="qml (primary) or tk (fallback).")] = "qml",
) -> None:
    """Open the GigaForge desktop window — the application's primary surface.

    The window renders telemetry straight from the local sinks, so it opens on a
    machine with no credential (``read-only`` mode). Qt Quick is the default;
    ``--ui tk`` opens the legacy fallback.
    """
    if ui.lower() == "qml":
        from gigaforge.desktop.qt_app import run_qml

        code = run_qml(repo_root=str(repo) if repo else None, offline=offline,
                       require_provider=require_provider, view=view)
    elif ui.lower() == "tk":
        try:
            from gigaforge.desktop import run as run_desktop
        except Exception as exc:  # pragma: no cover - Tk is stdlib, but be explicit
            _emit(
                f"the fallback window needs Tkinter ({type(exc).__name__}: {exc}).\n"
                "Install a Python build with Tcl/Tk support, or use `gigaforge web --serve`."
            )
            raise typer.Exit(R.EXIT_USAGE)
        code = run_desktop(
            repo_root=str(repo) if repo else None,
            offline=offline,
            require_provider=require_provider,
            view=view,
        )
    else:
        _emit("--ui must be either 'qml' or 'tk'.")
        raise typer.Exit(R.EXIT_USAGE)
    raise typer.Exit(int(code or 0))


#: ``gigaforge app`` is the same command under the name people type first.
app.command(name="app", hidden=True)(desktop)


@app.command()
def providers(
    as_json: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """List the registered model providers and their capabilities."""
    try:
        from gigaforge.providers.registry import default_registry
        from gigaforge.runtime import build_runtime

        runtime = build_runtime()
        rows = runtime.describe()
        registry_rows = default_registry().describe()
    except Exception as exc:  # noqa: BLE001
        _emit(f"cannot inspect providers: {exc}")
        raise typer.Exit(R.EXIT_USAGE)
    if as_json:
        _emit(R.render_json({"live": rows, "registry": registry_rows}), machine=True)
        return
    _emit(f"live provider: {rows.get('provider')}  "
          f"({'read-only' if rows.get('offline') else 'runnable'})")
    for row in registry_rows:
        name = row.get("name", "?")
        if row.get("error"):
            _emit(f"  [--] {name}: {row['error']}")
            continue
        caps = row.get("capabilities") or {}
        flags = ", ".join(
            f"{key}={value}" for key, value in caps.items() if value not in (None, "", 0, False)
        )
        _emit(f"  [ok] {name}: {flags or 'no capabilities reported'}")
    _emit("select with GIGAFORGE_PROVIDER=giga|openai "
          "(openai also needs GIGAFORGE_OPENAI_BASE_URL + GIGAFORGE_OPENAI_API_KEY)")


@app.command()
def agents(
    as_json: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """List the agent catalog: what exists and which DAG stage it drives."""
    try:
        from gigaforge.agents import AGENT_CATALOG
    except Exception as exc:  # noqa: BLE001
        _emit(f"cannot import the agent catalog: {exc}")
        raise typer.Exit(R.EXIT_USAGE)
    if as_json:
        _emit(R.render_json({"agents": AGENT_CATALOG}), machine=True)
        return
    for row in AGENT_CATALOG:
        _emit(f"  {row['name']:<16} {row['family']:<11} {row['stage']:<32} {row['role']}")


__all__ = ["app"]


if __name__ == "__main__":
    app()

