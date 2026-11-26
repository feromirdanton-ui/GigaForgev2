"""Design audit for the GigaForge control room.

Measures the *rendered* page the same way ``gigaforge wcag`` does -- the probe
JS is imported from ``gigaforge.harness.wcag``, not re-written here, so the
numbers this prints are the numbers the gate reports.

Why it exists: the gate only sees what is on screen.  Three of the panel's four
views are ``display:none`` until a tab is opened, so a single ``wcag`` run
checks the Overview tab and silently skips Steps, Trace and Diff.  This script
opens each view in turn and measures it, and it writes a screenshot per view at
1440px and 375px so the before/after pair is reproducible instead of anecdotal.

    python scripts/ui_design_audit.py --label before
    python scripts/ui_design_audit.py --label after --panel /tmp/panel.html
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gigaforge.harness.wcag import (  # noqa: E402
    AA_LARGE,
    AA_NORMAL,
    _JS,
    _OVERFLOW_JS,
    contrast_ratio,
)

VIEWS = ("overview", "steps", "trace", "diff")


def audit(page: Path, *, shots: Path | None = None, label: str = "") -> dict:
    from playwright.sync_api import sync_playwright

    result: dict = {"page": str(page), "label": label, "views": {}, "widths": {}}
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            for width, height in ((1440, 900), (375, 812), (320, 700)):
                for view in VIEWS:
                    page_obj = browser.new_page(viewport={"width": width, "height": height})
                    try:
                        page_obj.goto(page.resolve().as_uri(), timeout=30000)
                        if view != "overview":
                            page_obj.click("#tab-" + view)
                        page_obj.wait_for_timeout(150)
                        pairs = page_obj.evaluate(_JS) or []
                        over = page_obj.evaluate(_OVERFLOW_JS) or {}
                        rows = []
                        for row in pairs:
                            ratio = contrast_ratio(row["fg"], row["bg"])
                            need = AA_LARGE if row["large"] else AA_NORMAL
                            rows.append(
                                {
                                    "tag": row["tag"],
                                    "sample": row["sample"],
                                    "fg": row["fg"],
                                    "bg": row["bg"],
                                    "size": row["size"],
                                    "large": row["large"],
                                    "ratio": round(ratio, 2),
                                    "required": need,
                                    "passes": ratio >= need,
                                }
                            )
                        key = f"{width}"
                        bucket = result["widths"].setdefault(key, {})
                        bucket[view] = {
                            "pairs": len(rows),
                            "min_ratio": min((r["ratio"] for r in rows), default=0),
                            "failing": [r for r in rows if not r["passes"]],
                            "overflow": bool(over.get("count")),
                            "offenders": list(over.get("worst") or []),
                        }
                        if shots is not None:
                            out = shots / f"{label or 'panel'}-{width}-{view}.png"
                            page_obj.screenshot(path=str(out), full_page=True)
                    finally:
                        page_obj.close()
        finally:
            browser.close()

    worst = 9.9
    fails = 0
    for width, views in result["widths"].items():
        for view, data in views.items():
            fails += len(data["failing"])
            worst = min(worst, data["min_ratio"])
    result["summary"] = {"min_ratio": round(worst, 2), "failing_pairs": fails}
    return result


def print_report(result: dict) -> None:
    print(f"page: {result['page']}  label: {result.get('label') or '-'}")
    for width, views in result["widths"].items():
        for view, data in views.items():
            flag = "OVERFLOW" if data["overflow"] else "ok      "
            print(
                f"  {width:>4}px {view:<9} pairs={data['pairs']:<3} "
                f"min={data['min_ratio']:<5} {flag}"
            )
            for row in data["failing"]:
                print(
                    f"        FAIL {row['tag']:<6} {row['ratio']:<5} < {row['required']} "
                    f"{row['fg']} on {row['bg']} {row['sample']!r}"
                )
            for off in data["offenders"]:
                print(f"        OFFENDER {off}")
    print(f"summary: {json.dumps(result['summary'])}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", default="")
    ap.add_argument("--panel", default=None, help="Render/measure this page instead of a fresh panel.")
    ap.add_argument("--shots", default=None, help="Directory for screenshots.")
    ap.add_argument("--json", default=None, help="Write the raw report here.")
    args = ap.parse_args()

    panel = Path(args.panel) if args.panel else None
    if panel is None:
        from gigaforge.ui.snapshot import NODE_ORDER
        from gigaforge.ui.webui import render_html

        sys.path.insert(0, str(ROOT / "scripts"))
        import _render_panel  # noqa: PLC0415

        panel = Path(f"/tmp/panel-{args.label or 'current'}.html")
        panel.write_text(render_html(_render_panel.snapshot()), encoding="utf-8")
        del NODE_ORDER

    shots = Path(args.shots) if args.shots else None
    if shots:
        shots.mkdir(parents=True, exist_ok=True)

    result = audit(panel, shots=shots, label=args.label)
    print_report(result)
    if args.json:
        Path(args.json).write_text(json.dumps(result, indent=2), encoding="utf-8")
    return 0 if result["summary"]["failing_pairs"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
