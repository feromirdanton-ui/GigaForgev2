"""The control-room panel is a page, so it is held to the page standard.

``gigaforge wcag`` measures a rendered page; nothing measured the panel the
product actually ships.  Measured before this test existed, on a fully
populated snapshot: the diff hunk header was ``#328df2`` on ``#f7fbff`` -- 3.24
against the 4.5 AA floor -- and the four-tab strip was 322px wide, 2px past a
320px viewport.  Both are the kind of defect that ships because no gate looks.

These tests render the real panel from a real snapshot and assert the two
properties the design work established:

* every text/background pair clears AA (the accent used as *text* is a darker
  token than the accent used as a *fill*);
* nothing overflows at 375px -- the task's adaptivity floor -- and the tab
  strip reflows rather than pushing the page sideways.

They are the regression guard for the fix, not a restatement of it: the
positive control is that the same page passes, so a check that fails
everything cannot pass this file.
"""
from __future__ import annotations

import pytest

from gigaforge.harness.browser_probe import chromium_available, unavailable_reason
from gigaforge.ui.snapshot import NODE_ORDER
from gigaforge.ui.webui import render_html

PLAYWRIGHT = pytest.mark.skipif(
    not chromium_available(),
    reason=f"no launchable Chromium: {unavailable_reason()}",
)


def _populated_snapshot() -> dict:
    """A snapshot with every panel, table, badge and chip on the page.

    An empty snapshot renders the empty states and measures almost nothing --
    the failing hunk header only exists when there is a diff to show.
    """
    steps = [
        {
            "node": node,
            "status": "succeeded" if i < 8 else ("failed" if i == 8 else "running"),
            "started_at": f"2026-11-26T09:{10 + i:02d}:00Z",
            "duration_ms": 1200 + i * 340,
            "attempts": 2 if i == 3 else 1,
            "error_msg": "gate rejected: contrast 2.58 < 4.5" if i == 8 else "",
        }
        for i, node in enumerate(NODE_ORDER)
    ]
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
            "run_id": "run-2026-11-26-0941",
            "goal": "Build a cyberpunk landing page",
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
                "by_phase_usd": {"plan": 0.03, "generate": 0.12},
                "prices": {"GigaChat": {"prompt_per_million": 0.5, "completion_per_million": 1.5}},
            },
            "cache": {
                "hit_rate": 0.62, "hits": 58, "misses": 36, "writes": 41,
                "entries": 41, "bytes": 918273,
                "namespaces": {"plan": 12, "generate": 19},
            },
            "repair": {
                "proposals": 14, "rejections": 3, "reject_rate": 0.214,
                "fixloop_runs": 14, "max_iterations": 4, "avg_iterations": 1.8,
            },
            "latency": {
                "router_p50_ms": 1420, "router_p95_ms": 5310,
                "client_p95_ms": 6120, "samples": 96,
            },
            "health": {
                "router_errors": 2, "breaker_rejected": 1,
                "ratelimit_429": 0, "subagent_runs": 6,
            },
        },
        "phases": [
            {"phase": "plan", "model": "GigaChat-Pro", "calls": 12, "tokens": 21000,
             "cost_micros": 30000, "latency_p95_ms": 2100, "errors": 0},
            {"phase": "generate", "model": "GigaChat-Pro", "calls": 48, "tokens": 112000,
             "cost_micros": 120000, "latency_p95_ms": 6400, "errors": 1},
        ],
        "models": [{"model": "GigaChat-Pro", "calls": 74}, {"model": "GigaChat-Max", "calls": 22}],
        "steps": steps,
        "progress": {"done": 8, "total": len(steps), "running": 1, "failed": 1,
                     "percent": 73, "fraction": 0.73},
        "node_order": list(NODE_ORDER),
        "step_status_counts": {"succeeded": 8, "failed": 1, "running": 1},
        "trace": [
            {"ts": f"2026-11-26T09:{10 + i:02d}:00Z", "event": "router.call",
             "node": NODE_ORDER[i % len(NODE_ORDER)], "phase": "generate",
             "model": "GigaChat-Pro", "status": "ok", "tokens": 1200 + i * 37,
             "duration_ms": 800 + i * 21}
            for i in range(20)
        ],
        "trace_counts": {"router.call": 20},
        "diff": {
            "chars": 4820,
            "files": ["index.html", "styles.css"],
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
                " </div>\n"
            ),
            "truncated": False,
        },
        "errors": ["critic: contrast 2.58 < 4.5 on .hero p"],
        "index": {"present": True, "files": 128, "symbols": 940,
                  "languages": {"python": 96, "javascript": 24}},
        "totals": {"runs_recorded": 12, "cache_entries": 41,
                   "trace_events": 20, "metrics_points": 320},
        "series": [{"value": 1000 + i * 137} for i in range(60)],
    }


@PLAYWRIGHT
def test_the_panel_meets_aa_contrast(tmp_path) -> None:
    """Every text/background pair on the panel clears WCAG AA."""
    from gigaforge.harness.wcag import measure

    page = tmp_path / "panel.html"
    page.write_text(render_html(_populated_snapshot()), encoding="utf-8")
    report = measure(page)

    assert not report.error, report.error
    assert report.pairs, "the panel rendered no measurable text"
    assert not report.failures, (
        "the panel has unreadable text: "
        + "; ".join(f"{p.tag} {p.ratio:.2f} < {p.required} ({p.sample!r})" for p in report.failures)
    )
    assert report.min_ratio >= 4.5


@PLAYWRIGHT
def test_the_panel_does_not_overflow_at_375px(tmp_path) -> None:
    """Nothing sticks out past a 375px viewport -- reflow, not clipping."""
    from gigaforge.harness.wcag import measure

    page = tmp_path / "panel.html"
    page.write_text(render_html(_populated_snapshot()), encoding="utf-8")
    report = measure(page)

    assert not report.error, report.error
    assert not report.overflow_375, (
        "the panel overflows at 375px: " + repr(report.overflow_375_offenders)
    )


def test_the_accent_used_as_text_is_darker_than_the_accent_used_as_fill() -> None:
    """The token split is the fix, so it is asserted directly.

    A single accent cannot be both a fill and readable text on a pale surface:
    ``#328df2`` is 3.24 on the diff background.  The panel therefore carries
    ``--accent`` (fill) and ``--accent-ink`` (text), and this test fails if a
    later edit collapses them back into one.
    """
    from gigaforge.harness.wcag import contrast_ratio
    from gigaforge.ui.webui import _CSS

    assert "--accent-ink" in _CSS, "the text accent token is gone"
    # The two tokens must differ, and the text one must clear AA on the palest
    # surface the panel paints text on.
    assert "--accent:#328df2" in _CSS
    assert "--accent-ink:#1766c2" in _CSS
    assert contrast_ratio("rgb(23,102,194)", "rgb(247,251,255)") >= 4.5
    # And the fill token, used as text, would not have.
    assert contrast_ratio("rgb(50,141,242)", "rgb(247,251,255)") < 4.5
