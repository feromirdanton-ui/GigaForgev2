"""Renderers: the same snapshot as console text, Markdown or JSON.

Three rules the renderers exist to enforce:

* **Machine output never goes through ``rich``.**  ``rich`` soft-wraps at the
  terminal width and inserts newlines *inside* tokens, so pretty-printed JSON
  piped into ``jq`` stops parsing.  :func:`render_json` writes plain text to
  stdout, and the CLI suppresses the progress UI whenever ``--json`` is set.
* **Markup is never assembled by string concatenation.**  ``rich``'s
  ``Text.assemble`` does not parse ``[green]…[/]`` markup, so a span built
  programmatically prints its tags literally (and leaks into HTML if the same
  string is reused).  Styles are applied with ``Text.append(..., style=...)``.
* **One source of truth.**  Every renderer reads the snapshot dict from
  :mod:`gigaforge.ui.snapshot`; none of them reaches for the database.
"""

from __future__ import annotations

import json
import sys
from typing import Any, Mapping, Sequence, TextIO


def _ensure_utf8(stream: TextIO) -> TextIO:
    """Reconfigure ``stream`` for UTF-8 so unicode glyphs survive on Windows.

    On legacy Python consoles (cp1251, cp866, …) the default codec raises on
    non-Latin-1 code points.  Our renderers emit Unicode box characters and
    the ░/▓/█ progress glyphs, so the stream must accept them.  ``errors=replace``
    is the safe choice — it can never mask a real bug, only an unfortunate
    locale.
    """
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # pragma: no cover - non-reconfigurable streams (IDLE, pytest capture)
        pass
    return stream

BANNER = "GigaForge V2"

#: Exit codes the CLI and the renderers agree on.
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_NO_DATA = 2
EXIT_USAGE = 3


# ── plain / machine output ─────────────────────────────────────────────────
def render_json(snapshot: Mapping[str, Any], *, indent: int = 2) -> str:
    """Deterministic JSON — no rich, no wrapping, pipe-safe."""
    return json.dumps(snapshot, indent=indent, ensure_ascii=False, sort_keys=False, default=str)


def render_markdown(snapshot: Mapping[str, Any], *, diff_lines: int = 40) -> str:
    """A self-contained Markdown report (for a PR comment or a chat message)."""
    run = snapshot.get("run") or {}
    kpis = snapshot.get("kpis") or {}
    tokens = kpis.get("tokens") or {}
    cost = kpis.get("cost") or {}
    cache = kpis.get("cache") or {}
    repair = kpis.get("repair") or {}
    latency = kpis.get("latency") or {}
    progress = snapshot.get("progress") or {}
    diff = snapshot.get("diff") or {}

    lines: list[str] = []
    lines.append(f"# {BANNER} — run report")
    lines.append("")
    lines.append(f"**run** `{run.get('run_id') or '-'}`  ")
    lines.append(f"**status** {run.get('status') or 'unknown'} · "
                 f"**accepted** {'yes' if run.get('accepted') else 'no'} · "
                 f"**duration** {_human_ms(run.get('duration_ms'))}")
    lines.append("")
    lines.append("## KPIs")
    lines.append("")
    lines.append("| metric | value |")
    lines.append("|---|---|")
    lines.append(f"| tokens | {tokens.get('total', 0):,} "
                 f"(prompt {tokens.get('prompt', 0):,} / completion {tokens.get('completion', 0):,}) |")
    lines.append(f"| model calls | {tokens.get('calls', 0):,} |")
    lines.append(f"| cost | {cost.get('display') or _usd(cost.get('micros'))} |")
    lines.append(f"| cache hit rate | {_pct(cache.get('hit_rate'))} "
                 f"({cache.get('hits', 0)} hits / {cache.get('misses', 0)} misses) |")
    lines.append(f"| reject rate | {_pct(repair.get('reject_rate'))} "
                 f"({repair.get('rejections', 0)} of {repair.get('proposals', 0)} proposals) |")
    lines.append(f"| router p95 | {_human_ms(latency.get('router_p95_ms'))} |")
    lines.append(f"| progress | {progress.get('done', 0)}/{progress.get('total', 0)} "
                 f"({progress.get('percent', 0)}%) |")
    lines.append("")

    steps = snapshot.get("steps") or []
    if steps:
        lines.append("## Pipeline steps")
        lines.append("")
        lines.append("| # | node | status | duration | attempts |")
        lines.append("|---|---|---|---|---|")
        for step in steps:
            lines.append(
                f"| {step.get('index', 0)} | `{step.get('node')}` | {step.get('status')} | "
                f"{_human_ms(step.get('duration_ms'))} | {step.get('attempts', 1)} |"
            )
        lines.append("")

    phases = snapshot.get("phases") or []
    if phases:
        lines.append("## Per-phase cost")
        lines.append("")
        lines.append("| phase | model | calls | tokens | cost | p95 |")
        lines.append("|---|---|---|---|---|---|")
        for phase in phases:
            lines.append(
                f"| {phase.get('phase')} | {phase.get('model') or '-'} | {phase.get('calls', 0)} | "
                f"{phase.get('tokens', 0):,} | {_usd(phase.get('cost_micros'))} | "
                f"{_human_ms(phase.get('latency_p95_ms'))} |"
            )
        lines.append("")

    if diff.get("files"):
        lines.append("## Diff")
        lines.append("")
        lines.append(f"{diff.get('hunks', 0)} hunks · +{diff.get('added', 0)} / −{diff.get('removed', 0)}")
        lines.append("")
        for name in diff.get("files", [])[:20]:
            lines.append(f"- `{name}`")
        lines.append("")
        text = str(diff.get("text") or "")
        if text and diff_lines:
            lines.append("```diff")
            lines.extend(text.split("\n")[:diff_lines])
            lines.append("```")
            lines.append("")

    errors = snapshot.get("errors") or []
    if errors:
        lines.append("## Errors")
        lines.append("")
        for error in errors[:20]:
            lines.append(f"- {error}")
        lines.append("")

    source = snapshot.get("source") or {}
    notes = source.get("missing") or []
    if notes:
        lines.append("## Data sources")
        lines.append("")
        for note in notes:
            lines.append(f"- {note}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def render_console(snapshot: Mapping[str, Any], *, diff_lines: int = 30) -> str:
    """Plain-text console view (a colour-free fallback and the test surface)."""
    run = snapshot.get("run") or {}
    kpis = snapshot.get("kpis") or {}
    tokens = kpis.get("tokens") or {}
    cost = kpis.get("cost") or {}
    cache = kpis.get("cache") or {}
    repair = kpis.get("repair") or {}
    latency = kpis.get("latency") or {}
    progress = snapshot.get("progress") or {}

    width = 72
    lines = ["═" * width, f" {BANNER} · run report", "═" * width]
    lines.append(f"run      {run.get('run_id') or '-'}")
    lines.append(f"status   {run.get('status') or 'unknown'}   accepted={bool(run.get('accepted'))}"
                 f"   {_human_ms(run.get('duration_ms'))}")
    lines.append("─" * width)
    lines.append(f"tokens   {tokens.get('total', 0):,}  (prompt {tokens.get('prompt', 0):,} / "
                 f"completion {tokens.get('completion', 0):,}) over {tokens.get('calls', 0)} calls")
    lines.append(f"cost     {cost.get('display') or _usd(cost.get('micros'))}")
    lines.append(f"cache    hit rate {_pct(cache.get('hit_rate'))}  "
                 f"hits={cache.get('hits', 0)} misses={cache.get('misses', 0)} "
                 f"writes={cache.get('writes', 0)} entries={cache.get('entries', 0)}")
    lines.append(f"repair   reject rate {_pct(repair.get('reject_rate'))}  "
                 f"rejections={repair.get('rejections', 0)}/{repair.get('proposals', 0)}")
    lines.append(f"latency  router p50 {_human_ms(latency.get('router_p50_ms'))} · "
                 f"p95 {_human_ms(latency.get('router_p95_ms'))}")
    lines.append(f"progress {progress.get('done', 0)}/{progress.get('total', 0)} "
                 f"({progress.get('percent', 0)}%)  failed={progress.get('failed', 0)}")
    lines.append("─" * width)

    steps = snapshot.get("steps") or []
    if steps:
        lines.append("STEPS")
        for step in steps:
            lines.append(
                f"  {step.get('index', 0):>2}  {str(step.get('node')):<12} "
                f"{str(step.get('status')):<10} {_human_ms(step.get('duration_ms')):>10}"
            )
        lines.append("─" * width)

    phases = snapshot.get("phases") or []
    if phases:
        lines.append("PHASES")
        for phase in phases:
            lines.append(
                f"  {str(phase.get('phase')):<10} {str(phase.get('model') or '-'):<14} "
                f"calls={phase.get('calls', 0):<4} tokens={phase.get('tokens', 0):>8,} "
                f"cost={_usd(phase.get('cost_micros'))} p95={_human_ms(phase.get('latency_p95_ms'))}"
            )
        lines.append("─" * width)

    diff = snapshot.get("diff") or {}
    if diff.get("files"):
        lines.append(f"DIFF  {diff.get('hunks', 0)} hunks · +{diff.get('added', 0)} / "
                     f"−{diff.get('removed', 0)} · {len(diff.get('files') or [])} file(s)")
        for name in diff.get("files", [])[:15]:
            lines.append(f"  {name}")
        text = str(diff.get("text") or "")
        if text and diff_lines:
            for line in text.split("\n")[:diff_lines]:
                lines.append(f"    {line}")
        lines.append("─" * width)

    notes = (snapshot.get("source") or {}).get("missing") or []
    if notes:
        lines.append("NOTE")
        for note in notes:
            lines.append(f"  {note}")
        lines.append("─" * width)
    return "\n".join(lines).rstrip() + "\n"


# ── small formatters shared by all text renderers ──────────────────────────
def _pct(value: Any) -> str:
    try:
        return f"{float(value) * 100:.1f}%"
    except (TypeError, ValueError):
        return "0.0%"


def _usd(micros: Any) -> str:
    try:
        return f"${float(micros or 0) / 1_000_000:.6f}"
    except (TypeError, ValueError):
        return "$0.000000"


def _human_ms(value: Any) -> str:
    try:
        ms = float(value)
    except (TypeError, ValueError):
        return "-"
    if ms < 1000:
        return f"{ms:.0f} ms"
    if ms < 60_000:
        return f"{ms / 1000:.2f} s"
    minutes, seconds = divmod(ms / 1000.0, 60.0)
    return f"{int(minutes)}m {seconds:.1f}s"


def render(snapshot: Mapping[str, Any], fmt: str = "console") -> str:
    """Dispatch on a format name; unknown names fall back to console."""
    if fmt == "json":
        return render_json(snapshot)
    if fmt in {"md", "markdown"}:
        return render_markdown(snapshot)
    return render_console(snapshot)


def write_machine(text: str, *, stream: Any = None) -> None:
    """Emit machine-readable output straight to stdout, bypassing rich.

    The stream is reconfigured to UTF-8 so that progress glyphs and box
    characters survive on Windows consoles that default to cp1251.
    """
    target = _ensure_utf8(stream or sys.stdout)
    target.write(text if text.endswith("\n") else text + "\n")
    target.flush()


__all__ = [
    "BANNER",
    "EXIT_FAILED",
    "EXIT_NO_DATA",
    "EXIT_OK",
    "EXIT_USAGE",
    "render",
    "render_console",
    "render_json",
    "render_markdown",
    "write_machine",
]
