"""Unified-diff rendering for the terminal.

A diff is not plain text and must not be rendered as such: the ``+``/``-``
prefix *is* the styling.  Two details that produce wrong output when missed:

* **The trailing newline.**  ``diff.split("\\n")`` on a string that ends with a
  newline yields a final empty element which colours as a blank context line, so
  every hunk appears to gain a line.  The empty tail is dropped.
* **``/dev/null`` is on both sides.**  It is the *old* side of a creation and
  the *new* side of a deletion; judging it once in a shared path helper labels a
  new file as a deletion.  The decision is made per side.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

from . import analytics as A

STATUS_STYLES: dict[str, str] = {
    "succeeded": "green",
    "skipped": "dim",
    "running": "cyan",
    "pending": "yellow",
    "failed": "red",
    "timeout": "red",
    "unknown": "dim",
}


def split_lines(text: str) -> list[str]:
    """Split a diff without the phantom trailing ``""`` element."""
    if not text:
        return []
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def _strip_prefix(path: str) -> str:
    """Drop git's per-side ``a/`` / ``b/`` prefix (``/dev/null`` has none)."""
    if path.startswith(("a/", "b/")):
        return path[2:]
    return path


def file_action(old_path: str, new_path: str) -> str:
    """Classify a file record from its two sides (see the ``/dev/null`` note).

    The comparison is on the *unprefixed* paths: ``a/x.py`` and ``b/x.py`` are
    the same file, so comparing the raw strings would call every entry a rename.
    """
    if old_path == "/dev/null":
        return "created"
    if new_path == "/dev/null":
        return "deleted"
    if old_path and new_path and _strip_prefix(old_path) != _strip_prefix(new_path):
        return "renamed"
    return "modified"


def line_kind(line: str) -> str:
    """Classify one diff line: ``add`` / ``del`` / ``hunk`` / ``meta`` / ``context``."""
    if line.startswith("+++") or line.startswith("---"):
        return "meta"
    if line.startswith("@@"):
        return "hunk"
    if line.startswith("diff --git"):
        return "meta"
    if line.startswith("+") and not line.startswith("+++"):
        return "add"
    if line.startswith("-") and not line.startswith("---"):
        return "del"
    return "context"


def file_records(diff_text: str) -> list[dict[str, Any]]:
    """Parse a unified diff into one record per file, with per-file counts."""
    records: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    in_hunk = False

    def open_record(path: str = "", old: str = "", new: str = "") -> dict[str, Any]:
        record = {
            "path": path,
            "old_path": old,
            "new_path": new,
            "action": "modified",
            "hunks": 0,
            "added": 0,
            "removed": 0,
            "lines": [],
        }
        records.append(record)
        return record

    for line in split_lines(diff_text):
        if line.startswith("diff --git "):
            parts = line.split()
            new_path = parts[3][2:] if len(parts) >= 4 and parts[3].startswith("b/") else (
                parts[3] if len(parts) >= 4 else ""
            )
            current = open_record(path=new_path)
            in_hunk = False
            continue
        if line.startswith("--- "):
            old_path = line[4:].strip()
            if current is None or (in_hunk and current["lines"]):
                current = open_record()
            current["old_path"] = old_path
            if not current["path"]:
                current["path"] = old_path
            in_hunk = False
            current["lines"].append(line)
            continue
        if line.startswith("+++ "):
            new_path = line[4:].strip()
            if current is None:
                current = open_record()
            current["new_path"] = new_path
            if new_path and new_path != "/dev/null":
                current["path"] = new_path[2:] if new_path.startswith("b/") else new_path
            elif current["old_path"]:
                current["path"] = (
                    current["old_path"][2:]
                    if current["old_path"].startswith("a/")
                    else current["old_path"]
                )
            current["action"] = file_action(current["old_path"], current["new_path"])
            current["lines"].append(line)
            continue
        if current is None:
            current = open_record()
        kind = line_kind(line)
        if kind == "hunk":
            current["hunks"] += 1
            in_hunk = True
        elif kind == "add":
            current["added"] += 1
        elif kind == "del":
            current["removed"] += 1
        current["lines"].append(line)

    for record in records:
        if record["old_path"] and record["new_path"]:
            record["action"] = file_action(record["old_path"], record["new_path"])
    return [r for r in records if r["lines"] or r["path"]]


def diff_summary(diff_text: str) -> dict[str, Any]:
    """Totals a viewer header shows, computed from the parsed records."""
    records = file_records(diff_text)
    stats = A.diff_stats(diff_text)
    counts: dict[str, int] = {}
    for record in records:
        counts[record["action"]] = counts.get(record["action"], 0) + 1
    return {
        "files": records,
        "file_count": len(records),
        "actions": counts,
        "hunks": stats["hunks"],
        "added": stats["added"],
        "removed": stats["removed"],
        "chars": stats["chars"],
    }


def plain_text(diff_text: str, *, limit: int | None = None) -> str:
    """A stable, markup-free rendering (used by tests and ``--json`` payloads)."""
    lines = split_lines(diff_text)
    if limit is not None:
        lines = lines[:limit]
    return "\n".join(lines)


# ── rich rendering (optional import) ───────────────────────────────────────
def render_rich(diff_text: str, *, limit: int | None = None) -> Any:
    """A ``rich.Text`` diff panel body with styled ``+``/``-`` lines.

    Styling is applied per span via ``Text.append(style=...)`` — never by
    assembling ``[green]`` markup, which ``rich`` would print literally.
    """
    from rich.text import Text  # imported lazily so the module stays importable without rich

    text = Text()
    lines = split_lines(diff_text)
    if limit is not None:
        lines = lines[:limit]
    for index, line in enumerate(lines):
        kind = line_kind(line)
        style = {
            "add": "green",
            "del": "red",
            "hunk": "cyan",
            "meta": "bold",
        }.get(kind, "")
        if index:
            text.append("\n")
        text.append(line, style=style)
    return text


def step_style(status: str) -> str:
    return STATUS_STYLES.get(str(status), "")


def status_glyph(status: str) -> str:
    return {
        "succeeded": "✓",
        "running": "▶",
        "skipped": "·",
        "pending": "…",
        "failed": "✗",
        "timeout": "⏱",
    }.get(str(status), "?")


def cache_meter(hit_rate: float, *, width: int = 20) -> str:
    """A text meter for the cache hit rate (no rich markup)."""
    ratio = max(0.0, min(1.0, float(hit_rate or 0.0)))
    filled = int(round(ratio * width))
    return "█" * filled + "░" * (width - filled)


def summary_line(snapshot: Mapping[str, Any]) -> str:
    """A one-line status string, used as a Live title and in chat replies."""
    run = snapshot.get("run") or {}
    kpis = snapshot.get("kpis") or {}
    progress = snapshot.get("progress") or {}
    cost = (kpis.get("cost") or {}).get("display") or "$0.000000"
    cache = A.as_float((kpis.get("cache") or {}).get("hit_rate"))
    return (
        f"{run.get('status') or 'unknown'} · {progress.get('done', 0)}/{progress.get('total', 0)} nodes · "
        f"{A.as_int((kpis.get('tokens') or {}).get('total')):,} tok · {cost} · cache {cache * 100:.0f}%"
    )


def iter_statuses(steps: Iterable[Mapping[str, Any]]) -> list[str]:
    return [str(step.get("status") or "unknown") for step in steps]


__all__ = [
    "STATUS_STYLES",
    "cache_meter",
    "diff_summary",
    "file_action",
    "file_records",
    "iter_statuses",
    "line_kind",
    "plain_text",
    "render_rich",
    "split_lines",
    "status_glyph",
    "step_style",
    "summary_line",
]
