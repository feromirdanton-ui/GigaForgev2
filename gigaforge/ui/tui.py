"""Rich terminal dashboard for a GigaForge run.

The dashboard is a *view* over one snapshot dict from
:mod:`gigaforge.ui.snapshot` — it holds no state of its own and never touches
SQLite, so the same panel can be driven live (``watch``) or rendered once
(``report``) with identical output.

Layout::

    ┌ GigaForge V2 · <run id> · status ───────────────────────────┐
    │ progress ████████████░░░░░░░░  7/11 nodes                    │
    ├─ KPIs ───────────────────────┬─ cache / repair ──────────────┤
    │ tokens 12,480 (8,120/4,360)  │ cache  ████████░░  62.5%    │
    │ cost  $0.006210               │ reject ░░░░░░░░░░   0.0%    │
    ├─ steps ──────────────────────┴──────────────────────────────┤
    │ ✓ intake  12 ms   ✓ index 240 ms   ▶ plan ...               │
    ├─ diff ──────────────────────────────────────────────────────┤
    │ + added   − removed                                          │
    └──────────────────────────────────────────────────────────────┘

``rich`` is an optional import: without it, :func:`render_plain` still produces
the boxed view as plain text, so a minimal deployment keeps working.
"""

from __future__ import annotations

import sys
import time
from typing import Any, Callable, Mapping, Optional, Sequence

from . import analytics as A
from . import diffview as D
from . import renderers as R
from .snapshot import NODE_ORDER, build_snapshot


def _has_rich() -> bool:
    try:
        import rich  # noqa: F401

        return True
    except Exception:  # pragma: no cover - depends on the deployment
        return False


def _make_console() -> Any:
    """Build a rich Console wired to a UTF-8 stdout.

    On Windows consoles that default to cp1251, rich's auto-detection picks
    up the legacy encoding and crashes on progress glyphs and box characters.
    Forcing UTF-8 + ``legacy_windows=False`` makes the TUI render anywhere.
    """
    from rich.console import Console

    R._ensure_utf8(sys.stdout)  # type: ignore[attr-defined]
    return Console(force_terminal=True, legacy_windows=False, file=sys.stdout)


# ── plain (no-rich) dashboard ──────────────────────────────────────────────
def render_plain(
    snapshot: Mapping[str, Any],
    *,
    width: int = 78,
    diff_lines: int = 24,
    show_diff: bool = True,
) -> str:
    """Boxed dashboard as plain text — the no-``rich`` path and the test surface."""
    run = snapshot.get("run") or {}
    kpis = snapshot.get("kpis") or {}
    tokens = kpis.get("tokens") or {}
    cost = kpis.get("cost") or {}
    cache = kpis.get("cache") or {}
    repair = kpis.get("repair") or {}
    latency = kpis.get("latency") or {}
    progress = snapshot.get("progress") or {}
    steps = snapshot.get("steps") or []

    bar_width = max(10, width - 34)
    filled = int(round(A.as_float(progress.get("fraction")) * bar_width))
    bar = "█" * filled + "░" * (bar_width - filled)

    out: list[str] = []
    out.append("┌─ " + f"GigaForge V2 · {run.get('run_id') or 'no run'}"
               + " " + "─" * max(0, width - 26 - len(str(run.get("run_id") or "no run"))) + "┐")
    out.append(f"│ {str(run.get('status') or 'unknown'):<12} accepted="
               f"{'yes' if run.get('accepted') else 'no ':>3}  "
               f"{A.human_duration(run.get('duration_ms')):<12}"
               f"{'generated ' + str(snapshot.get('generated_at') or ''):>40} │")
    out.append("├" + "─" * (width - 2) + "┤")
    out.append(f"│ progress {bar}  {progress.get('done', 0)}/{progress.get('total', 0)} nodes"
               f"{'':<6}│")
    out.append(f"│ tokens   {A.as_int(tokens.get('total')):>10,}  "
               f"(prompt {A.as_int(tokens.get('prompt')):,} / completion "
               f"{A.as_int(tokens.get('completion')):,}) over {A.as_int(tokens.get('calls'))} calls"
               + " " * max(0, width - 78) + "│")
    out.append(f"│ cost     {str(cost.get('display') or '$0.000000'):>10}   "
               f"router p95 {A.human_duration(latency.get('router_p95_ms')):<10}"
               + " " * max(0, width - 56) + "│")
    out.append(f"│ cache    {D.cache_meter(cache.get('hit_rate'))} "
               f"{A.as_float(cache.get('hit_rate')) * 100:5.1f}%  "
               f"hits {A.as_int(cache.get('hits'))} · misses {A.as_int(cache.get('misses'))} · "
               f"writes {A.as_int(cache.get('writes'))}" + " " * max(0, width - 78) + "│")
    out.append(f"│ repair   reject rate {A.as_float(repair.get('reject_rate')) * 100:5.1f}%  "
               f"({A.as_int(repair.get('rejections'))}/{A.as_int(repair.get('proposals'))})  "
               f"fix-iterations avg {repair.get('avg_iterations', 0)}"
               + " " * max(0, width - 74) + "│")
    out.append("├" + "─" * (width - 2) + "┤")
    out.append("│ STEPS" + " " * (width - 7) + "│")
    if steps:
        for step in steps:
            line = (f"  {D.status_glyph(str(step.get('status')))}"
                    f" {str(step.get('node')):<11} {str(step.get('status')):<10} "
                    f"{A.human_duration(step.get('duration_ms')):>10}")
            out.append("│" + line + " " * max(0, width - 2 - len(line)) + "│")
    else:
        out.append("│  (no node records — the run has not been journaled yet)"
                   + " " * (width - 59) + "│")

    phases = snapshot.get("phases") or []
    if phases:
        out.append("├" + "─" * (width - 2) + "┤")
        out.append("│ PHASES" + " " * (width - 8) + "│")
        for phase in phases:
            line = (f"  {str(phase.get('phase')):<10} {str(phase.get('model') or '-'):<14} "
                    f"calls={A.as_int(phase.get('calls')):<4} tok={A.as_int(phase.get('tokens')):>8,} "
                    f"cost={A.human_usd(phase.get('cost_micros'))}")
            out.append("│" + line + " " * max(0, width - 2 - len(line)) + "│")

    diff = snapshot.get("diff") or {}
    if show_diff and diff.get("files"):
        out.append("├" + "─" * (width - 2) + "┤")
        header = (f"│ DIFF  {diff.get('hunks', 0)} hunks · +{diff.get('added', 0)} / "
                  f"−{diff.get('removed', 0)} · {len(diff.get('files') or [])} file(s)")
        out.append(header + " " * max(0, width - 2 - len(header)) + "│")
        for record in D.diff_summary(str(diff.get("text") or ""))["files"][:8]:
            line = (f"  {record['action']:<9} {record['path']}  "
                    f"+{record['added']}/−{record['removed']}")
            out.append("│" + line + " " * max(0, width - 2 - len(line)) + "│")
        # ``plain_text`` returns a string, so it must be split — iterating the
        # string directly would print one character per line.
        for line in D.split_lines(str(diff.get("text") or ""))[:diff_lines]:
            out.append("│ " + line[: width - 3] + " " * max(0, width - 3 - len(line[: width - 3])) + "│")

    errors = snapshot.get("errors") or []
    if errors:
        out.append("├" + "─" * (width - 2) + "┤")
        out.append("│ ERRORS" + " " * (width - 8) + "│")
        for error in errors[:6]:
            line = "  " + str(error)
            out.append("│" + line[: width - 2] + " " * max(0, width - 2 - len(line[: width - 3])) + "│")

    notes = (snapshot.get("source") or {}).get("missing") or []
    if notes:
        out.append("├" + "─" * (width - 2) + "┤")
        for note in notes:
            line = "  ! " + str(note)
            out.append("│" + line[: width - 2] + " " * max(0, width - 2 - len(line[: width - 2])) + "│")

    out.append("└" + "─" * (width - 2) + "┘")
    return "\n".join(out)


# ── rich dashboard ─────────────────────────────────────────────────────────
def build_renderable(snapshot: Mapping[str, Any], *, width: int = 100, diff_lines: int = 24) -> Any:
    """Rich layout for the dashboard (falls back to plain text without rich)."""
    if not _has_rich():
        from rich.console import Console  # type: ignore  # unreachable without rich

        return Console().render_str(render_plain(snapshot, width=width, diff_lines=diff_lines))

    from rich.console import Group
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    run = snapshot.get("run") or {}
    kpis = snapshot.get("kpis") or {}
    tokens = kpis.get("tokens") or {}
    cost = kpis.get("cost") or {}
    cache = kpis.get("cache") or {}
    repair = kpis.get("repair") or {}
    latency = kpis.get("latency") or {}
    progress = snapshot.get("progress") or {}
    steps = snapshot.get("steps") or []

    blocks: list[Any] = []

    # progress bar
    bar_width = max(10, width - 40)
    ratio = A.as_float(progress.get("fraction"))
    filled = int(round(ratio * bar_width))
    bar = Text()
    bar.append("█" * filled, style="bold green")
    bar.append("░" * (bar_width - filled), style="dim")
    bar.append(
        f"  {progress.get('done', 0)}/{progress.get('total', 0)} nodes"
        f"  ({progress.get('percent', 0)}%)",
        style="bold",
    )
    blocks.append(Panel(bar, title="pipeline progress", border_style="cyan"))

    # KPI grid
    kpi = Table.grid(expand=True, padding=(0, 2))
    kpi.add_column(ratio=1)
    kpi.add_column(ratio=1)
    kpi.add_column(ratio=1)
    kpi.add_column(ratio=1)

    def card(label: str, value: str, style: str = "bold white") -> Any:
        text = Text()
        text.append(value, style=style)
        text.append(f"\n{label}", style="dim")
        return text

    kpi.add_row(
        card("tokens", f"{A.as_int(tokens.get('total')):,}", "bold cyan"),
        card("cost", str(cost.get("display") or "$0.000000"), "bold magenta"),
        card("cache hit", f"{A.as_float(cache.get('hit_rate')) * 100:.1f}%", "bold green"),
        card("reject rate", f"{A.as_float(repair.get('reject_rate')) * 100:.1f}%", "bold yellow"),
    )
    kpi.add_row(
        card("model calls", f"{A.as_int(tokens.get('calls')):,}"),
        card("router p95", A.human_duration(latency.get("router_p95_ms"))),
        card("cache entries", f"{A.as_int(cache.get('entries')):,}"),
        card("duration", A.human_duration(run.get("duration_ms"))),
    )
    blocks.append(Panel(kpi, title="kpis", border_style="blue"))

    # step timeline
    table = Table(expand=True, box=None, pad_edge=False)
    table.add_column("#", width=3, justify="right", style="dim")
    table.add_column("node", style="bold")
    table.add_column("status")
    table.add_column("duration", justify="right")
    table.add_column("attempts", justify="right")
    table.add_column("detail", overflow="ellipsis")
    if steps:
        for step in steps:
            status = str(step.get("status"))
            table.add_row(
                str(step.get("index", 0)),
                str(step.get("node")),
                Text(f"{D.status_glyph(status)} {status}", style=D.step_style(status)),
                A.human_duration(step.get("duration_ms")),
                str(step.get("attempts", 1)),
                str(step.get("error_msg") or step.get("error_code") or ""),
            )
    else:
        table.add_row("", Text("no node records yet", style="dim"), "", "", "", "")
    blocks.append(Panel(table, title="steps", border_style="green"))

    # meters
    meters = Text()
    meters.append("cache ", style="dim")
    meters.append(D.cache_meter(A.as_float(cache.get("hit_rate"))), style="green")
    meters.append(
        f" {A.as_float(cache.get('hit_rate')) * 100:5.1f}%   "
        f"hits {A.as_int(cache.get('hits'))} · misses {A.as_int(cache.get('misses'))} · "
        f"writes {A.as_int(cache.get('writes'))}\n",
        style="white",
    )
    meters.append("reject ", style="dim")
    meters.append(
        D.cache_meter(A.as_float(repair.get("reject_rate"))),
        style="yellow" if A.as_float(repair.get("reject_rate")) else "dim",
    )
    meters.append(
        f" {A.as_float(repair.get('reject_rate')) * 100:5.1f}%   "
        f"({A.as_int(repair.get('rejections'))}/{A.as_int(repair.get('proposals'))} proposals, "
        f"avg {repair.get('avg_iterations', 0)} iterations)",
        style="white",
    )
    blocks.append(Panel(meters, title="cache · repair", border_style="yellow"))

    # phases
    phases = snapshot.get("phases") or []
    if phases:
        ptable = Table(expand=True, box=None, pad_edge=False)
        ptable.add_column("phase", style="bold")
        ptable.add_column("model")
        ptable.add_column("calls", justify="right")
        ptable.add_column("tokens", justify="right")
        ptable.add_column("cost", justify="right")
        ptable.add_column("p95", justify="right")
        for phase in phases:
            ptable.add_row(
                str(phase.get("phase")),
                str(phase.get("model") or "-"),
                str(A.as_int(phase.get("calls"))),
                f"{A.as_int(phase.get('tokens')):,}",
                A.human_usd(phase.get("cost_micros")),
                A.human_duration(phase.get("latency_p95_ms")),
            )
        blocks.append(Panel(ptable, title="per-phase cost", border_style="magenta"))

    # diff
    diff = snapshot.get("diff") or {}
    if diff.get("files"):
        header = Text()
        header.append(
            f"{diff.get('hunks', 0)} hunks · ", style="dim"
        )
        header.append(f"+{diff.get('added', 0)}", style="green")
        header.append("  ")
        header.append(f"−{diff.get('removed', 0)}", style="red")
        header.append(f"  · {len(diff.get('files') or [])} file(s)", style="dim")
        body = D.render_rich(str(diff.get("text") or ""), limit=diff_lines)
        blocks.append(Panel(Group(header, Text(""), body), title="diff", border_style="red"))

    # errors / data notes
    errors = snapshot.get("errors") or []
    if errors:
        body = Text()
        for index, error in enumerate(errors[:10]):
            if index:
                body.append("\n")
            body.append("✗ ", style="red")
            body.append(str(error), style="white")
        blocks.append(Panel(body, title="errors", border_style="red"))

    notes = (snapshot.get("source") or {}).get("missing") or []
    if notes:
        body = Text()
        for index, note in enumerate(notes):
            if index:
                body.append("\n")
            body.append("! ", style="yellow")
            body.append(str(note), style="dim")
        blocks.append(Panel(body, title="data sources", border_style="yellow"))

    title = (f"GigaForge V2 · {run.get('run_id') or 'no run'} · "
             f"{run.get('status') or 'unknown'} · {snapshot.get('generated_at') or ''}")
    return Panel(Group(*blocks), title=title, border_style="cyan")


def render(
    snapshot: Mapping[str, Any],
    *,
    width: int = 100,
    diff_lines: int = 24,
    plain: bool = False,
) -> Any:
    """Return a rich renderable, or a plain string when rich is unavailable."""
    if plain or not _has_rich():
        return render_plain(snapshot, width=min(width, 100), diff_lines=diff_lines)
    return build_renderable(snapshot, width=width, diff_lines=diff_lines)


def print_dashboard(
    snapshot: Mapping[str, Any],
    *,
    width: int = 100,
    diff_lines: int = 24,
    plain: bool = False,
) -> None:
    """Render once to stdout."""
    if plain or not _has_rich():
        R.write_machine(render_plain(snapshot, width=min(width, 100), diff_lines=diff_lines))
        return
    _make_console().print(build_renderable(snapshot, width=width, diff_lines=diff_lines))


def watch(
    store: Any,
    *,
    run_id: Optional[str] = None,
    interval: float = 1.0,
    iterations: Optional[int] = None,
    width: int = 100,
    diff_lines: int = 24,
    plain: bool = False,
    prices: Any = None,
    default_model: str = "GigaChat",
    sleep: Callable[[float], None] = time.sleep,
    on_snapshot: Optional[Callable[[Mapping[str, Any]], None]] = None,
) -> Mapping[str, Any]:
    """Poll the sinks and repaint a live dashboard.

    Stops after ``iterations`` repaints (``None`` = until interrupted) and
    returns the last snapshot.  Returning the snapshot rather than ``None`` is
    what lets the CLI tests assert on real numbers without a terminal.
    """
    use_rich = not plain and _has_rich()
    console = None
    live = None
    if use_rich:
        from rich.console import Console
        from rich.live import Live

        console = _make_console()
        live = Live(console=console, refresh_per_second=max(1.0, 1.0 / max(interval, 0.05)), transient=False)
        live.start()
    last: Mapping[str, Any] = {}
    count = 0
    try:
        while True:
            snapshot = build_snapshot(
                store, run_id=run_id, prices=prices, default_model=default_model
            )
            last = snapshot
            if on_snapshot is not None:
                on_snapshot(snapshot)
            if live is not None:
                live.update(build_renderable(snapshot, width=width, diff_lines=diff_lines))
            else:
                R.write_machine(render_plain(snapshot, width=min(width, 100), diff_lines=diff_lines))
            count += 1
            if iterations is not None and count >= int(iterations):
                break
            sleep(interval)
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        pass
    finally:
        if live is not None:
            live.stop()
    return last


__all__ = [
    "build_renderable",
    "print_dashboard",
    "render",
    "render_plain",
    "watch",
]
