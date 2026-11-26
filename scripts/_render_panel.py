"""Render the control-room panel from a realistic in-memory snapshot.

Used for design QA: it produces the same HTML the ``gigaforge web --out``
command writes, but from a fully populated snapshot so every panel, table,
badge and chip is on the page when the WCAG/overflow probe measures it.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gigaforge.ui.snapshot import NODE_ORDER  # noqa: E402
from gigaforge.ui.webui import render_html  # noqa: E402


def _steps() -> list[dict]:
    rows = []
    statuses = ["succeeded"] * 8 + ["running", "failed", "skipped"]
    for i, node in enumerate(NODE_ORDER):
        status = statuses[i] if i < len(statuses) else "pending"
        rows.append(
            {
                "node": node,
                "status": status,
                "started_at": f"2026-11-26T09:{10 + i:02d}:00Z",
                "duration_ms": 1200 + i * 340,
                "attempts": 2 if i == 3 else 1,
                "error_msg": "gate rejected: contrast 2.58 < 4.5" if status == "failed" else "",
            }
        )
    return rows


def _trace() -> list[dict]:
    events = []
    for i in range(40):
        events.append(
            {
                "ts": f"2026-11-26T09:{10 + (i % 50):02d}:{i:02d}Z",
                "event": ["dag.node_start", "dag.node_done", "router.call", "fixloop.rejected"][i % 4],
                "node": NODE_ORDER[i % len(NODE_ORDER)],
                "phase": ["plan", "generate", "critic", "repair"][i % 4],
                "model": "GigaChat-Pro",
                "status": ["ok", "ok", "ok", "failed"][i % 4],
                "tokens": 1200 + i * 37,
                "duration_ms": 800 + i * 21,
            }
        )
    return events


def snapshot() -> dict:
    steps = _steps()
    done = sum(1 for s in steps if s["status"] == "succeeded")
    return {
        "snapshot_version": 1,
        "generated_at": "2026-11-26T09:41:12Z",
        "source": {
            "db_path": ".gigaforge/core.sqlite3",
            "trace_path": ".gigaforge/traces/core.jsonl",
            "db_present": True,
            "trace_present": True,
            "missing": [],
        },
        "run": {
            "run_id": "run-2026-11-26-0941-cyberpunk-landing",
            "goal": "Build a cyberpunk landing page with a hero, feature grid and pricing table",
            "status": "succeeded",
            "accepted": True,
            "started_at": "2026-11-26T09:10:00Z",
            "finished_at": "2026-11-26T09:41:12Z",
            "duration_ms": 1872000,
        },
        "kpis": {
            "tokens": {"total": 184320, "prompt": 121400, "completion": 62920, "calls": 96},
            "cost": {
                "micros": 214500,
                "usd": 0.2145,
                "display": "$0.2145",
                "by_phase_usd": {"plan": 0.03, "generate": 0.12, "critic": 0.04, "repair": 0.02},
                "prices": {"GigaChat": {"prompt_per_million": 0.5, "completion_per_million": 1.5}},
            },
            "cache": {
                "hit_rate": 0.62,
                "hits": 58,
                "misses": 36,
                "writes": 41,
                "entries": 41,
                "bytes": 918273,
                "namespaces": {"plan": 12, "generate": 19, "critic": 10},
            },
            "repair": {
                "proposals": 14,
                "rejections": 3,
                "reject_rate": 0.214,
                "fixloop_runs": 14,
                "max_iterations": 4,
                "avg_iterations": 1.8,
            },
            "latency": {
                "router_p50_ms": 1420,
                "router_p95_ms": 5310,
                "client_p95_ms": 6120,
                "samples": 96,
            },
            "health": {
                "router_errors": 2,
                "breaker_rejected": 1,
                "ratelimit_429": 0,
                "subagent_runs": 6,
            },
        },
        "phases": [
            {"phase": "plan", "model": "GigaChat-Pro", "calls": 12, "tokens": 21000, "cost_micros": 30000, "latency_p95_ms": 2100, "errors": 0},
            {"phase": "generate", "model": "GigaChat-Pro", "calls": 48, "tokens": 112000, "cost_micros": 120000, "latency_p95_ms": 6400, "errors": 1},
            {"phase": "critic", "model": "GigaChat-Max", "calls": 22, "tokens": 38000, "cost_micros": 40000, "latency_p95_ms": 3300, "errors": 1},
            {"phase": "repair", "model": "GigaChat-Pro", "calls": 14, "tokens": 13320, "cost_micros": 24500, "latency_p95_ms": 2800, "errors": 0},
        ],
        "models": [
            {"model": "GigaChat-Pro", "calls": 74},
            {"model": "GigaChat-Max", "calls": 22},
        ],
        "steps": steps,
        "progress": {
            "done": done,
            "total": len(steps),
            "running": 1,
            "failed": 1,
            "percent": round(done / len(steps) * 100),
            "fraction": done / len(steps),
        },
        "node_order": list(NODE_ORDER),
        "step_status_counts": {"succeeded": done, "failed": 1, "running": 1, "skipped": 1},
        "trace": _trace(),
        "trace_counts": {"dag.node_start": 11, "dag.node_done": 9, "router.call": 10, "fixloop.rejected": 3},
        "diff": {
            "chars": 4820,
            "files": ["index.html", "styles.css", "app.js"],
            "hunks": 7,
            "added": 214,
            "removed": 38,
            "text": (
                "diff --git a/index.html b/index.html\n"
                "--- a/index.html\n+++ b/index.html\n"
                "@@ -1,6 +1,9 @@\n"
                "-<div class=\"hero\">\n"
                "+<section class=\"hero\" aria-label=\"hero\">\n"
                "+  <h1>Neon City</h1>\n"
                "+  <p>Build faster with the GigaForge harness.</p>\n"
                " </div>\n"
            ),
            "truncated": False,
        },
        "errors": ["critic: contrast 2.58 < 4.5 on .hero p"],
        "index": {"present": True, "files": 128, "symbols": 940, "languages": {"python": 96, "javascript": 24, "html": 8}},
        "totals": {"runs_recorded": 12, "cache_entries": 41, "trace_events": 40, "metrics_points": 320},
        "series": [{"value": 1000 + i * 137} for i in range(60)],
    }


if __name__ == "__main__":
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/panel.html")
    out.write_text(render_html(snapshot()), encoding="utf-8")
    print(f"wrote {out} ({out.stat().st_size} bytes)")
