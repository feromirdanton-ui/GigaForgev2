"""Contrast is part of the verdict, not a report beside it.

Measured live in ``--subagents`` mode: a card with ``h3 rgb(232,246,255)`` on
``rgb(255,255,255)`` -- 1.10 against a 3.0 floor, white on white -- and the run
settled ``accepted``.  ``wcag.py`` existed, had tests, and was called by nothing,
so the defect it describes had no effect on anything.

These tests drive the real check on real files.  The positive control is the
point: a check that fails everything is as useless as one that passes
everything, so every "this is rejected" test is paired with "this is not".
"""
from __future__ import annotations

from pathlib import Path

import pytest

from gigaforge.core.pipeline import TaskPipeline
from gigaforge.harness.browser_probe import chromium_available, unavailable_reason

PLAYWRIGHT = pytest.mark.skipif(
    not chromium_available(),
    reason=f"no launchable Chromium: {unavailable_reason()}",
)

WHITE_ON_WHITE = """<!doctype html>
<html><head><style>
  html,body{margin:0;font-family:system-ui}
  .card{background:#ffffff;padding:24px}
  .card h3,.card p{color:#e8f6ff}
</style></head><body>
<section class="features"><div class="card">
  <h3>Comfortable cat play areas</h3>
  <p>Relax and watch our furry friends play in a calm, spacious room.</p>
</div></section>
</body></html>
"""

LEGIBLE = """<!doctype html>
<html><head><style>
  html,body{margin:0;font-family:system-ui;background:#0f172a;color:#f8fafc}
  .card{background:#111c33;padding:24px}
  .card h3,.card p{color:#f8fafc}
</style></head><body>
<section class="features"><div class="card">
  <h3>Comfortable cat play areas</h3>
  <p>Relax and watch our furry friends play in a calm, spacious room.</p>
</div></section>
</body></html>
"""


class _Validation:
    def __init__(self, files: list[str]) -> None:
        self.accepted = True
        self.changed_files = files


class _Ctx:
    def __init__(self, kind: str = "artifact") -> None:
        self.run_id = "test-run"
        self.state = type("S", (), {"goal": "Build a landing page"})()
        self.extras: dict = {"task_kind": {"kind": kind, "confidence": 0.9}}


class _Stub(TaskPipeline):
    def __init__(self, root: Path) -> None:  # noqa: D107 - bypass the constructor
        self.config = type("C", (), {"repo_root": str(root)})()
        self.events: list[tuple[str, dict]] = []

    def _event(self, name: str, **payload) -> None:
        self.events.append((name, payload))

    def _repo_root(self) -> Path:
        return Path(self.config.repo_root)


@PLAYWRIGHT
def test_white_on_white_is_never_accepted_unreadable(tmp_path: Path) -> None:
    """The defect the live run shipped: unreadable, and accepted.

    **The invariant is not "rejected" -- it is "not accepted unreadable".**  A
    failing contrast is a colour, and a colour is computable, so the gate now
    repairs before it judges.  Either outcome is correct: the page is repaired
    until it passes, or it is rejected with the ratio.  What must never happen
    is the third outcome -- the run is accepted and the page is still
    unreadable.
    """
    page = tmp_path / "index.html"
    page.write_text(WHITE_ON_WHITE, encoding="utf-8")
    pipeline = _Stub(tmp_path)
    unmet = pipeline._check_wcag(_Ctx(), ["index.html"])

    if unmet:
        # Rejected: the message must carry the number and name the text, so the
        # repair loop knows what to change.
        assert "1.10" in unmet[0] or "< 3.0" in unmet[0] or "< 4.5" in unmet[0]
        assert "Comfortable cat play areas" in unmet[0] or "cat" in unmet[0].lower()
    else:
        # Repaired: the file on disk must now actually pass.
        from gigaforge.harness.wcag import measure

        after = measure(page)
        assert not after.failures, (
            "the gate accepted the page without rejecting it and without "
            f"repairing it: min ratio {after.min_ratio}"
        )
        assert after.min_ratio >= 3.0


@PLAYWRIGHT
def test_a_legible_page_passes(tmp_path: Path) -> None:
    """The positive control: the check must not fail a page that is fine."""
    (tmp_path / "index.html").write_text(LEGIBLE, encoding="utf-8")
    pipeline = _Stub(tmp_path)
    assert pipeline._check_wcag(_Ctx(), ["index.html"]) == []


@PLAYWRIGHT
def test_the_measurement_is_recorded_on_the_context(tmp_path: Path) -> None:
    """A rejected run has to carry the number that rejected it."""
    (tmp_path / "index.html").write_text(WHITE_ON_WHITE, encoding="utf-8")
    pipeline = _Stub(tmp_path)
    ctx = _Ctx()
    pipeline._check_wcag(ctx, ["index.html"])
    assert "wcag" in ctx.extras
    assert ctx.extras["wcag"]["min_ratio"] > 0


def test_a_code_change_is_not_measured_for_contrast(tmp_path: Path) -> None:
    """A code change has no page; measuring one would fail a run for a file it
    never wrote."""
    (tmp_path / "index.html").write_text(WHITE_ON_WHITE, encoding="utf-8")
    pipeline = _Stub(tmp_path)
    assert pipeline._check_wcag(_Ctx(kind="code"), ["index.html"]) == []


def test_a_change_with_no_html_is_not_measured(tmp_path: Path) -> None:
    pipeline = _Stub(tmp_path)
    assert pipeline._check_wcag(_Ctx(), ["db.py"]) == []


def test_a_page_outside_the_repository_is_ignored(tmp_path: Path) -> None:
    """A path that escapes the repo root is not this run's artifact."""
    outside = tmp_path.parent / "outside.html"
    outside.write_text(WHITE_ON_WHITE, encoding="utf-8")
    repo = tmp_path / "repo"
    repo.mkdir()
    pipeline = _Stub(repo)
    assert pipeline._check_wcag(_Ctx(), ["../outside.html"]) == []


def test_the_gate_is_wired_into_settle() -> None:
    """The defect was that the check existed and nothing called it."""
    import inspect

    source = inspect.getsource(TaskPipeline._node_settle)
    assert "_check_wcag" in source
    # And that its failure reaches the verdict, not only the log.
    idx = source.index("_check_wcag")
    tail = source[idx:idx + 600]
    assert "accepted = False" in tail
