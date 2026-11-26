"""Render the control-room panel from a realistic in-memory snapshot.

Used for design QA: it produces the same HTML the ``gigaforge web --out``
command writes, but from a fully populated snapshot so every panel, table,
badge and chip is on the page when the WCAG/overflow probe measures it.

The snapshot itself lives in :mod:`gigaforge.ui.demo_snapshot`, not here.  It
used to be duplicated in this file and in ``tests/test_panel_design.py`` --
103 of 107 keys identical -- and two copies of a fixture drift until the page a
test measures is not the page a screenshot shows.

    python scripts/_render_panel.py /tmp/panel.html
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gigaforge.ui.demo_snapshot import populated_snapshot  # noqa: E402
from gigaforge.ui.webui import render_html  # noqa: E402

#: Kept as a module-level name because ``ui_design_audit.py`` imports it.
snapshot = populated_snapshot


if __name__ == "__main__":
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/panel.html")
    out.write_text(render_html(snapshot()), encoding="utf-8")
    print(f"wrote {out} ({out.stat().st_size} bytes)")
