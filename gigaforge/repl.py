"""GigaForge interactive coding REPL — an advanced CLI client focused on coding.

``gigaforge chat`` launches a rich, interactive session that walks the developer
through the GigaForge pipeline step-by-step with full visibility:

    1. repo scan          — show the L0 repo map (ranked symbols) so the dev
                             can pick the target function.
    2. function select    — pick the symbol to edit (autocomplete on names).
    3. task input         — multi-line natural-language task description.
    4. slice preview      — render the Context Slice (target body + callers'
                             signatures + imports) with syntax highlighting,
                             + token budget used. The dev sees EXACTLY what
                             the Coder will see — nothing more.
    5. plan preview       — run the Lite planner; show the step list; confirm.
    6. coder diff         — run the Coder; render the produced unified diff
                             with added/removed lines colour-coded.
    7. validate           — run the DiffValidator; show the apply method
                             (exact/fuzzy/ast_surgical) + lint result.
    8. apply / iterate    — apply to a worktree, run tests, show the
                             Firewall-compressed test summary; on failure,
                             enter the fix-loop with the dev observing each
                             iteration. Approve, reject (retry), or abort.
    9. commit             — atomic git commit on the worktree; show the SHA.
   10. session metrics    — live token spend / cache hits / budget remaining.

Designed for coding: every step is observable, the dev can abort before any
LLM spend, and the diff is the unit of review (never a full file dump).

Non-interactive flags also supported for scripting:
    gigaforge chat --repo . --function validate_email --file math_utils.py \\
        --task "..." --yes   # run the whole flow without prompts.
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from typing import Any

from rich.align import Align
from rich.console import Console, Group
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.rule import Rule
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from gigaforge.client.errors import (
    AuthError,
    BudgetExceeded,
    CircuitBreakerOpen,
    UpstreamError,
)
from gigaforge.core.task_state import TaskState
from gigaforge.firewall.tokens import estimate_tokens

log = logging.getLogger("gigaforge.repl")
console = Console()

# Accent palette — emerald/amber on dark; no indigo/blue per house style.
ACCENT = "bold cyan"
ACCENT_DIM = "cyan"
GOOD = "bold green"
WARN = "bold yellow"
BAD = "bold red"
MUTED = "dim"


def _banner() -> Panel:
    title = Text.assemble(
        ("GigaForge", "bold cyan"),
        (" interactive coding session", "white"),
    )
    subtitle = Text("context is budget · diff is contract · cache is memory", MUTED)
    body = Align.center(Group(title, Text(""), subtitle))
    return Panel(body, border_style="cyan", padding=(1, 4))


def _repo_map_table(entries: list[Any], max_rows: int = 25) -> Table:
    """Render the ranked repo-map as a rich table."""
    t = Table(title="L0 repo map — symbols by inbound references", border_style="cyan",
              title_style=ACCENT, expand=True)
    t.add_column("#", style=MUTED, width=4)
    t.add_column("symbol", style=ACCENT, overflow="fold")
    t.add_column("kind", style="magenta", width=10)
    t.add_column("file", style="blue", overflow="fold")
    t.add_column("refs", justify="right", style="yellow", width=5)
    for i, e in enumerate(entries[:max_rows], 1):
        t.add_row(
            str(i),
            str(getattr(e, "qname", getattr(e, "name", "?"))),
            str(getattr(e, "kind", "?")),
            str(getattr(e, "path", "?")),
            str(getattr(e, "ref_count", getattr(e, "callers", 0)) or 0),
        )
    if len(entries) > max_rows:
        t.caption = f"… {len(entries) - max_rows} more (use /find <name> to filter)"
    return t


def _slice_panel(slice_: Any) -> Panel:
    """Render the Context Slice: target function (full body) + callers
    (signatures only) + imports, with syntax highlighting + token budget."""
    tf = slice_.target_function
    # Target function — full body shown (the Coder edits this).
    target_src = f"{tf.signature}:\n{tf.body}" if tf.body else f"{tf.signature}: ..."
    target_syn = Syntax(target_src, "python", theme="monokai", line_numbers=True,
                        word_wrap=True, background_color="default")
    # Related symbols — signatures only (no bodies).
    related_lines = []
    for rs in (slice_.related_symbols or [])[:12]:
        related_lines.append(f"{rs.signature}")
    related_syn = Syntax("\n".join(related_lines) or "# (none)",
                         "python", theme="ansi_dark", word_wrap=True,
                         background_color="default")
    # Imports.
    imp_lines = [f"import {i.module}" + (f" as {i.alias}" if i.alias else "")
                 for i in (slice_.relevant_imports or [])]
    imports_syn = Syntax("\n".join(imp_lines) or "# (none)", "python",
                         theme="ansi_dark", background_color="default")
    budget = slice_.token_budget_used()

    body = Group(
        Text("target function (full body — the Coder edits this):", ACCENT),
        target_syn,
        Text(""),
        Text("callers / callees (signatures only):", ACCENT_DIM),
        related_syn,
        Text(""),
        Text("relevant imports:", ACCENT_DIM),
        imports_syn,
        Text(""),
        Text(f"slice token budget used: {budget} tokens", WARN if budget > 1500 else MUTED),
        Text(f"target_file: {slice_.target_file or '(unknown)'}", MUTED),
    )
    return Panel(body, title="[bold]Context Slice[/] — what the Coder sees",
                 border_style="cyan", title_align="left")


def _diff_panel(diff_text: str, method: str, lint_ok: bool, reason: str) -> Panel:
    """Render a unified diff with +/- lines colour-coded."""
    lines = diff_text.splitlines()
    rendered: list[Text] = []
    for ln in lines:
        if ln.startswith("+++") or ln.startswith("---") or ln.startswith("@@"):
            rendered.append(Text(ln, style="bold cyan"))
        elif ln.startswith("+"):
            rendered.append(Text(ln, style="green"))
        elif ln.startswith("-"):
            rendered.append(Text(ln, style="red"))
        else:
            rendered.append(Text(ln, style="dim"))
    body = Group(*rendered) if rendered else Text("(empty diff)", MUTED)
    method_color = {"exact": GOOD, "fuzzy": WARN, "ast_surgical": WARN, "none": BAD}.get(method, MUTED)
    lint_color = GOOD if lint_ok else BAD
    header = Text.assemble(
        ("apply method: ", MUTED), (method, method_color),
        ("   lint: ", MUTED), ("clean" if lint_ok else "errors", lint_color),
    )
    if reason and reason != "ok":
        header.append_text(Text(f"   reason: {reason}", BAD))
    return Panel(Group(header, Text(""), body), title="[bold]Coder diff[/]",
                 border_style="cyan", title_align="left")


def _test_summary_panel(compressed: dict) -> Panel:
    """Render the Firewall-compressed test summary (never the raw 5000 lines)."""
    t = Table(border_style="cyan", expand=True, show_header=True, header_style=ACCENT)
    t.add_column("metric", style="cyan")
    t.add_column("value", justify="right", style="white")
    t.add_row("passed", str(compressed.get("passed_count", 0)))
    t.add_row("failed", str(compressed.get("failed_count", 0)))
    t.add_row("errors", str(compressed.get("error_count", 0)))
    t.add_row("skipped", str(compressed.get("skipped_count", 0)))
    failed = compressed.get("failed", []) or []
    body: Any = t
    if failed:
        ft = Table(border_style="red", expand=True, title_style=BAD,
                   title="[bold red]failed tests (Firewall-compressed)[/]")
        ft.add_column("test", style="red", overflow="fold")
        ft.add_column("file:line", style="yellow", overflow="fold")
        ft.add_column("error", style="white", overflow="fold")
        for f in failed[:10]:
            ft.add_row(
                str(f.get("test", "?")),
                f"{f.get('file', '?')}:{f.get('line', '?')}",
                str(f.get("error", "?"))[:120],
            )
        body = Group(t, Text(""), ft)
    return Panel(body, title="[bold]tests[/] (Firewall-compressed — never raw output)",
                 border_style="cyan", title_align="left")


def _budget_panel(budgets: Any, router: Any) -> Panel:
    """Live budget dashboard: per-phase remaining + per-task total."""
    t = Table(border_style="cyan", expand=True, show_header=True, header_style=ACCENT)
    t.add_column("phase", style="cyan")
    t.add_column("remaining", justify="right", style="white")
    t.add_column("task total", justify="right", style="yellow")
    for phase in ("plan", "code", "fixloop", "review"):
        try:
            rem = budgets.remaining(phase)
            tt = budgets.task_total()
        except Exception:
            rem, tt = "?", "?"
        t.add_row(phase, str(rem), str(tt))
    return Panel(t, title="[bold]budget[/]", border_style="cyan", title_align="left")


def _metrics_panel(metrics: Any, task_id: str) -> Panel:
    """Session metrics summary for this task."""
    try:
        agg = metrics.aggregate(task_id=task_id)
    except Exception:
        agg = {}
    t = Table(border_style="cyan", expand=True, show_header=True, header_style=ACCENT)
    t.add_column("metric", style="cyan")
    t.add_column("value", justify="right", style="white")
    t.add_row("LLM calls", str(agg.get("total_calls", 0)))
    t.add_row("tokens in", str(agg.get("total_tokens_in", 0)))
    t.add_row("tokens out", str(agg.get("total_tokens_out", 0)))
    t.add_row("cache hits", str(agg.get("cache_hits", 0)))
    t.add_row("estimated", str(agg.get("estimated_calls", 0)))
    t.add_row("reject rate", f"{agg.get('reject_rate', 0):.1%}")
    return Panel(t, title=f"[bold]session metrics[/] — {task_id}",
                 border_style="cyan", title_align="left")


# --- the interactive flow ----------------------------------------------------


async def _interactive_session(
    rt: Any,
    repo_root: Path,
    *,
    initial_function: str | None,
    initial_file: Path | None,
    initial_task: str | None,
    auto_yes: bool,
) -> int:
    """Drive one interactive coding session. Returns an exit code."""
    from gigaforge.core.cache import compute_hash
    from gigaforge.core.diff_validator import DiffValidator
    from gigaforge.firewall.slicer import ContextSlicer
    from gigaforge.l0.index import L0Index
    from gigaforge.l0.repo_map import build as build_repo_map
    from gigaforge.sandbox.allowlist import CommandRejected
    from gigaforge.sandbox.executor import SandboxExecutor
    from gigaforge.worktree import commit_all, git_sha, make_worktree, remove_worktree

    console.print(_banner())
    s = rt.settings

    # --- 1. repo scan ---
    console.print(Rule("[bold cyan]1. repo scan[/]", style="cyan"))
    console.print(f"indexing [cyan]{repo_root}[/] (0 LLM calls)…")
    idx: L0Index = rt.index
    try:
        n = idx.index_repo(repo_root)
    except Exception as exc:
        console.print(f"[red]index failed:[/] {exc}")
        return 1
    console.print(f"indexed [green]{n}[/] file(s); HEAD = [yellow]{git_sha(repo_root)[:8]}[/]")

    repo_map_entries = build_repo_map(repo_root, idx).entries
    console.print(_repo_map_table(repo_map_entries))

    # --- 2. function select ---
    console.print(Rule("[bold cyan]2. select target function[/]", style="cyan"))
    function = initial_function or Prompt.ask(
        "[cyan]target function[/] (e.g. validate_email, Calculator.add)",
        default=repo_map_entries[0].qname if repo_map_entries else "",
    )
    sym = idx.get_symbol(function)
    if sym is None:
        console.print(f"[red]symbol not found:[/] {function}")
        return 2
    file_path = initial_file or Path(sym.path)
    if not Path(sym.path).is_absolute():
        file_path = repo_root / sym.path
    console.print(f"selected [green]{function}[/] in [blue]{Path(sym.path).name}[/]")
    console.print(f"  signature: [cyan]{sym.signature}[/]")
    console.print(f"  callers: [yellow]{', '.join(idx.get_callers(function)) or '(none)'}[/]")

    # --- 3. task input ---
    console.print(Rule("[bold cyan]3. task description[/]", style="cyan"))
    if initial_task:
        task = initial_task
        console.print(f"task: [white]{task}[/]")
    else:
        console.print("describe the edit (multi-line; end with a blank line):", style=MUTED)
        lines = []
        while True:
            try:
                ln = input("... " if lines else "> ")
            except EOFError:
                break
            if ln == "":
                break
            lines.append(ln)
        task = "\n".join(lines).strip()
    if not task:
        console.print("[red]empty task — aborting[/]")
        return 2
    console.print(f"task ([cyan]{estimate_tokens(task)}[/] tokens): [white]{task[:120]}{'…' if len(task) > 120 else ''}[/]")

    # --- 4. slice preview ---
    console.print(Rule("[bold cyan]4. context slice preview[/]", style="cyan"))
    slicer = ContextSlicer()
    try:
        slice_ = slicer.build(
            idx,
            target_function=function,
            file=file_path.resolve(),
            target_file_rel=str(file_path.relative_to(repo_root)) if file_path.is_absolute() else str(file_path),
        )
    except Exception as exc:
        console.print(f"[red]slice build failed:[/] {exc}")
        return 1
    console.print(_slice_panel(slice_))

    if not auto_yes and not Confirm.ask("[cyan]proceed[/] with this slice? (no LLM calls yet)", default=True):
        console.print("[yellow]aborted before any LLM spend[/]")
        return 0

    # --- 5. plan preview (Lite) ---
    console.print(Rule("[bold cyan]5. plan[/] (GigaChat-3-Lightning)", style="cyan"))
    head_sha = git_sha(repo_root)
    slice_hash = _slice_hash(slice_)
    task_hash = compute_hash(task, slice_hash, head_sha)
    task_id = f"chat-{task_hash[:8]}"
    state = TaskState(goal=task, task_id=task_id, git_sha=head_sha, slice_hash=slice_hash,
                      plan=[], scratchpad=[], metrics={})
    try:
        plan = await rt.pipeline._planner.plan(task, state)
    except Exception as exc:
        return _handle_error(exc, "planner")
    console.print(f"plan ([cyan]{len(plan)}[/] steps):")
    for i, step in enumerate(plan, 1):
        console.print(f"  [cyan]{i}.[/] [yellow]{step.tool}[/] {step.args}")

    if not auto_yes and not Confirm.ask("[cyan]run the coder[/]? (this spends tokens)", default=True):
        return 0

    # --- 6. coder diff (Pro) ---
    console.print(Rule("[bold cyan]6. coder[/] (GigaChat-2-Pro)", style="cyan"))
    try:
        diff = await rt.pipeline._coder.edit(slice_, [], task_id=task_id)
    except Exception as exc:
        return _handle_error(exc, "coder")

    # --- 7. validate ---
    console.print(Rule("[bold cyan]7. validate + apply[/]", style="cyan"))
    validator = DiffValidator(index=idx)
    worktree = make_worktree(repo_root, branch_name=f"chat-{task_id}")
    try:
        result = validator.validate_and_apply(diff, repo_root=repo_root, worktree=worktree)
        console.print(_diff_panel(diff, result.method, result.lint_clean, result.reason))
        if not result.ok:
            console.print(f"[red]diff rejected:[/] {result.reason}")
            if not auto_yes and not Confirm.ask("retry the coder with the rejection?", default=True):
                return 5
            diff = await rt.pipeline._coder.edit(slice_, [], task_id=task_id,
                                                  prev_rejection=result.reason)
            result = validator.validate_and_apply(diff, repo_root=repo_root, worktree=worktree)
            console.print(_diff_panel(diff, result.method, result.lint_clean, result.reason))
            if not result.ok:
                console.print("[red]retry also rejected — aborting[/]")
                return 5

        # --- 8. tests ---
        console.print(Rule("[bold cyan]8. tests[/]", style="cyan"))
        sandbox = SandboxExecutor(worktree=worktree, timeout_s=s.sandbox_timeout_s)
        try:
            exec = await sandbox.run_tests()
            console.print(_test_summary_panel(exec.compressed))
            if not exec.ok:
                console.print(f"[yellow]tests failed — entering fix-loop (max {s.budget_fixloop_iters})[/]")
                for it in range(1, s.budget_fixloop_iters + 1):
                    console.print(f"[cyan]fix-loop iteration {it}/{s.budget_fixloop_iters}[/]")
                    rej = f"tests failed: {exec.compressed}"
                    diff = await rt.pipeline._coder.edit(slice_, [], task_id=task_id, prev_rejection=rej)
                    result = validator.validate_and_apply(diff, repo_root=repo_root, worktree=worktree)
                    if not result.ok:
                        console.print(f"[red]fix diff rejected:[/] {result.reason}")
                        continue
                    exec = await sandbox.run_tests()
                    console.print(_test_summary_panel(exec.compressed))
                    if exec.ok:
                        break
                if not exec.ok:
                    console.print("[red]fix-loop exhausted — aborting[/]")
                    return 5
        except CommandRejected as exc:
            console.print(f"[red]sandbox rejected command:[/] {exc}")
            return 7

        # --- 9. commit ---
        console.print(Rule("[bold cyan]9. commit[/]", style="cyan"))
        try:
            new_sha = commit_all(worktree, f"gigaforge(chat): {task[:60]}")
            console.print(f"committed on worktree: [green]{new_sha}[/]")
        except Exception as exc:
            console.print(f"[yellow]commit failed:[/] {exc}")

    finally:
        try:
            remove_worktree(worktree)
        except Exception as exc:  # noqa: BLE001
            log.warning("worktree cleanup failed: %s", exc)

    # --- 10. session metrics ---
    console.print(Rule("[bold cyan]10. session metrics[/]", style="cyan"))
    console.print(_budget_panel(rt.budgets, rt.router))
    console.print(_metrics_panel(rt.metrics, task_id))
    console.print(Panel(
        Align.center(Text("✓ done — diff applied to a worktree commit; repo_root untouched",
                          style=GOOD)),
        border_style="green",
    ))
    return 0


def _slice_hash(slice_: Any) -> str:
    import hashlib
    import json
    data = slice_.to_prompt_dict()
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _handle_error(exc: Exception, phase: str) -> int:
    """Map typed errors to console messages + exit codes (mirrors the CLI table)."""
    from gigaforge.agents.planner import PlanEscalation
    from gigaforge.sandbox.allowlist import CommandRejected
    codes = {AuthError: 3, BudgetExceeded: 4, CircuitBreakerOpen: 5,
             PlanEscalation: 6, CommandRejected: 7, UpstreamError: 1}
    console.print(f"[red]error in {phase}:[/] [{type(exc).__name__}] {exc}")
    return codes.get(type(exc), 1)


def run_chat(
    repo: Path,
    *,
    function: str | None = None,
    file: Path | None = None,
    task: str | None = None,
    yes: bool = False,
    verbose: bool = False,
) -> int:
    """Entry point for `gigaforge chat`. Returns an exit code."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    repo = repo.resolve()
    if not (repo / ".git").exists():
        console.print(f"[red]error:[/] {repo} is not a git repository", style="red")
        return 2

    from gigaforge.runtime import build_runtime

    async def _go() -> int:
        rt = build_runtime()
        try:
            return await _interactive_session(
                rt, repo,
                initial_function=function,
                initial_file=file,
                initial_task=task,
                auto_yes=yes,
            )
        finally:
            await rt.aclose()

    try:
        return asyncio.run(_go())
    except KeyboardInterrupt:
        console.print("\n[yellow]interrupted by user[/]")
        return 130
    except (AuthError, BudgetExceeded, CircuitBreakerOpen, UpstreamError) as e:
        return _handle_error(e, "session")


if __name__ == "__main__":  # pragma: no cover
    sys.exit(run_chat(Path(".")))
