"""Functional check of the control room's interactive states.

Contrast and overflow are measured by ``gigaforge wcag``; this checks the
things a colour probe cannot see: that the tab strip is a real ARIA tablist
(``aria-selected`` follows the click, arrows move between tabs), that a
keyboard focus ring is actually painted, that the refresh button reports
``aria-busy`` while it works, and that the page logs no console error.

The page behaves differently by scheme, and both behaviours are checked:

* ``file://`` -- there is no ``api/snapshot`` to poll, so refresh/auto must be
  disabled rather than looking live and ignoring the click.
* ``http://`` -- the served panel polls, so refresh must set ``aria-busy``
  while the request is in flight.

    python scripts/ui_state_check.py /tmp/panel_after.html
"""
from __future__ import annotations

import functools
import http.server
import json
import socketserver
import sys
import threading
import time
from pathlib import Path


def _serve(page: Path) -> tuple[str, socketserver.TCPServer]:
    """Serve *page* plus a stub ``api/snapshot`` on a free local port."""
    body = page.read_bytes()
    snapshot = json.dumps({"run": {"run_id": "stub"}, "kpis": {}, "steps": []}).encode()

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):  # keep the console clean
            return

        def handle_one_request(self):
            # The page closes the socket as soon as it has the snapshot, which
            # surfaces as a BrokenPipe traceback from the stub.  It is the test
            # harness, not the page, so it is swallowed here.
            try:
                super().handle_one_request()
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True

        def do_GET(self):  # noqa: N802
            if self.path.startswith("/api/snapshot"):
                # A real snapshot takes a moment; without a delay the fetch
                # resolves before the test can observe ``aria-busy``, and the
                # check would fail on a page that is behaving correctly.
                time.sleep(0.4)
                payload, ctype = snapshot, "application/json"
            else:
                payload, ctype = body, "text/html; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    httpd = socketserver.TCPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{httpd.server_address[1]}/", httpd


def _check(view, problems: list[str], *, served: bool) -> None:
    # 1. tablist semantics
    if view.get_attribute(".tabs", "role") != "tablist":
        problems.append("tabs role is not 'tablist'")
    if view.get_attribute("#tab-overview", "aria-selected") != "true":
        problems.append("overview tab is not aria-selected on load")

    # 2. click moves selection and the panel
    view.click("#tab-trace")
    if view.get_attribute("#tab-trace", "aria-selected") != "true":
        problems.append("clicking Trace did not set aria-selected")
    if view.get_attribute("#tab-overview", "aria-selected") != "false":
        problems.append("overview stayed aria-selected after switching")
    if view.is_visible("#view-overview"):
        problems.append("overview panel still visible after switching to Trace")
    if not view.is_visible("#view-trace"):
        problems.append("trace panel not visible after switching")

    # 3. arrow-key navigation (ARIA tabs pattern)
    view.focus("#tab-trace")
    view.keyboard.press("ArrowRight")
    if view.get_attribute("#tab-diff", "aria-selected") != "true":
        problems.append("ArrowRight did not move to the next tab")
    view.keyboard.press("Home")
    if view.get_attribute("#tab-overview", "aria-selected") != "true":
        problems.append("Home did not move to the first tab")

    # 4. a visible focus ring on keyboard focus
    view.focus("#tab-overview")
    outline = view.evaluate(
        "() => { const s = getComputedStyle(document.getElementById('tab-overview'));"
        " return s.outlineStyle + ' ' + s.outlineWidth; }"
    )
    if "none" in outline or outline.strip().endswith("0px"):
        problems.append(f"no focus outline on the tab: {outline!r}")

    # 5. refresh: busy while working when served, disabled when not
    if served:
        view.click("#refresh")
        if view.get_attribute("#refresh", "aria-busy") != "true":
            problems.append("served refresh did not set aria-busy")
    else:
        if not view.is_disabled("#refresh"):
            problems.append("file:// refresh is not disabled")
        if not view.is_disabled("#auto"):
            problems.append("file:// auto-refresh is not disabled")


def main(page: str) -> int:
    from playwright.sync_api import sync_playwright

    problems: list[str] = []
    path = Path(page)
    url, httpd = _serve(path)
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            try:
                for served, target in ((False, path.resolve().as_uri()), (True, url)):
                    view = browser.new_page(viewport={"width": 1280, "height": 900})
                    errors: list[str] = []
                    view.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
                    view.on("pageerror", lambda e: errors.append(str(e)))
                    view.goto(target, timeout=30000)
                    _check(view, problems, served=served)
                    if errors:
                        problems.append(f"console errors ({'http' if served else 'file'}): " + "; ".join(errors[:3]))
                    view.close()
            finally:
                browser.close()
    finally:
        httpd.shutdown()

    if problems:
        print("FAIL")
        for p in problems:
            print("  -", p)
        return 1
    print("PASS: tablist semantics, arrow keys, focus ring, aria-busy/disabled, no console errors")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "/tmp/panel_after.html"))
