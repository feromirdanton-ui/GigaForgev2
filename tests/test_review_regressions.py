"""Regressions for the defects the live runs found.

Every test here failed before its fix and passes after.  They are grouped by
the *shape* of the defect rather than by file, because the same shape appeared
in several places and the point of the group is that the shape is now covered
everywhere it occurred.

The rule these encode: a check that cannot change a verdict is decoration, and
a check that rejects correct work is worse than no check at all.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from gigaforge.agents.constitutional import check_preserve_public_apis
from gigaforge.core.acceptance import (
    WorkItem,
    allocate_work,
    check_acceptance,
    merge_diffs,
    paths_of,
)
from gigaforge.harness.goal_contract import (
    check_artifacts,
    extract_requirements,
    strip_comments,
)
from gigaforge.harness.plan_guard import _has_target
from gigaforge.l0.toolchain import python_bin, resolve_tool


# --------------------------------------------------------------------------
# The contract must reject a skeleton and accept a real page.
# --------------------------------------------------------------------------

_SKELETON = """<!doctype html><html><head><style>
body{background:#000;color:#fff}
h1{color:cyan;text-shadow:0 0 8px magenta}
.grid{display:grid}
@keyframes glitch{0%{opacity:1}}
</style></head><body>
<h1>GigaForge</h1>
<div class="grid"><div>Feature Card 1</div><div>Feature Card 2</div></div>
<footer>Footer Content</footer>
</body></html>"""

_REAL = """<!doctype html><html><head><style>
body{background:#05010d;color:#e8f7ff;font-family:system-ui}
h1{color:cyan;text-shadow:0 0 12px magenta,0 0 24px rgba(255,0,255,.6)}
.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:1.5rem}
@keyframes glitch{0%{transform:translate(0)}50%{transform:translate(-2px,1px)}}
</style></head><body>
<header><h1 class="glitch">GigaForge</h1>
<p>A harness that takes on the work with a deterministically correct answer so
the model can spend its tokens on judgement instead of bookkeeping.</p></header>
<section class="hero"><h2>Deterministic by default</h2>
<p>Hashing, diffing, cache invalidation and loop detection are computations, not
reasoning. The harness performs them; the model decides what to build.</p></section>
<div class="grid">
<div><h3>Ownership</h3><p>Every sub-agent owns a disjoint set of paths, so two
children cannot overwrite each other's work.</p></div>
<div><h3>Acceptance</h3><p>A diff is checked against its assignment before it is
merged, and a conflict is a rejection with a reason.</p></div>
<div><h3>Recovery</h3><p>A rejected run is salvaged from its own trajectory
rather than discarded.</p></div>
</div>
<footer><p>GigaForge — built for long tasks, not for chat.</p></footer>
</body></html>"""

_GOAL = (
    "Create a cyberpunk landing page about the GigaForge agent harness. "
    "Requirements: neon cyan and magenta palette, a hero section, a glitch "
    "effect on the title, a feature grid, and a footer. Self-contained."
)


def test_contract_rejects_a_skeleton_that_satisfies_every_structural_check(tmp_path):
    """The measured defect: every structural check passed on a wireframe.

    The skeleton has cyan and magenta in real CSS declarations, a hero, a glitch
    keyframe, a grid, a footer, no external assets and 1539 bytes — and its
    entire body is "Feature Card 1", "Feature Card 2", "Footer Content".  It
    settled ``accepted: true``.
    """
    (tmp_path / "index.html").write_text(_SKELETON, encoding="utf-8")
    verdict = check_artifacts(
        extract_requirements(_GOAL), tmp_path, files=["index.html"]
    )
    assert not verdict.ok, "a placeholder page must not satisfy a design brief"
    assert any("placeholders" in u for u in verdict.unmet), verdict.unmet


def test_contract_accepts_a_real_page(tmp_path):
    """The other half: a page that does the work must not be rejected."""
    (tmp_path / "index.html").write_text(_REAL, encoding="utf-8")
    verdict = check_artifacts(
        extract_requirements(_GOAL), tmp_path, files=["index.html"]
    )
    assert verdict.ok, verdict.unmet


def test_contract_recognises_rgba_as_the_colour_it_is(tmp_path):
    """A false negative rejects correct work, which is as bad as a false pass.

    Measured: a page whose only magenta was
    ``text-shadow: 2px 2px 4px rgba(255, 0, 255, 0.5)`` was rejected for
    "colour 'magenta'".  The colour was there, in the form a designer writes it.
    """
    page = _REAL.replace("magenta,0 0 24px rgba(255,0,255,.6)", "rgba(255, 0, 255, 0.5)")
    (tmp_path / "index.html").write_text(page, encoding="utf-8")
    verdict = check_artifacts(
        extract_requirements(_GOAL), tmp_path, files=["index.html"]
    )
    assert "colour 'magenta'" in verdict.met, verdict.unmet


def test_contract_ignores_a_colour_named_in_a_comment(tmp_path):
    """A comment is not a declaration: the browser does not apply it."""
    page = _REAL.replace("color:cyan", "color:#fff").replace(
        "text-shadow:0 0 12px magenta,0 0 24px rgba(255,0,255,.6)",
        "/* background: cyan; abandoned */",
    )
    (tmp_path / "index.html").write_text(page, encoding="utf-8")
    verdict = check_artifacts(
        extract_requirements(_GOAL), tmp_path, files=["index.html"]
    )
    assert "colour 'cyan'" in verdict.unmet
    assert "colour 'magenta'" in verdict.unmet


def test_contract_checks_only_the_files_the_run_wrote(tmp_path):
    """A run that wrote nothing must not pass on a file already in the tree."""
    (tmp_path / "old.html").write_text(_REAL, encoding="utf-8")
    verdict = check_artifacts(extract_requirements(_GOAL), tmp_path, files=[])
    assert not verdict.ok
    assert "no artifact was produced" in verdict.unmet


def test_contract_min_size_is_per_file_not_summed(tmp_path):
    """Five 300-byte files are not a 1200-byte page."""
    for index in range(5):
        (tmp_path / f"part{index}.html").write_text("x" * 300, encoding="utf-8")
    verdict = check_artifacts(
        extract_requirements(_GOAL), tmp_path,
        files=[f"part{i}.html" for i in range(5)],
    )
    assert any("at least" in u for u in verdict.unmet)


def test_strip_comments_keeps_urls_intact():
    """A ``//`` inside a URL is not a comment."""
    text = 'a{background:url(https://example.com/x.png)}'
    assert "https://example.com/x.png" in strip_comments(text)


# --------------------------------------------------------------------------
# Acceptance and merge: ownership is exclusive, acceptance precedes merge.
# --------------------------------------------------------------------------

def _create_diff(path: str, body: str) -> str:
    return (
        f"--- /dev/null\n+++ b/{path}\n"
        f"@@ -0,0 +1,{len(body.splitlines())} @@\n"
        + "\n".join(f"+{line}" for line in body.splitlines())
        + "\n"
    )


def test_two_children_creating_the_same_file_is_a_conflict_not_a_silent_overwrite(tmp_path):
    """The measured defect: the merge reported ok and lost one child's work."""
    (tmp_path / "README.md").write_text("# x\n", encoding="utf-8")
    report = merge_diffs(
        [
            {"name": "a", "diff": _create_diff("index.html", "<h1>A</h1>")},
            {"name": "b", "diff": _create_diff("index.html", "<h1>B</h1>")},
        ],
        root=tmp_path,
    )
    assert report.accepted == ["a"]
    assert report.conflicts, "the second writer must be reported, not applied"
    assert "index.html" in report.conflicts[0]["paths"]


def test_a_rejected_child_contributes_nothing():
    """``_subagent_diff`` read ``item.diff`` and never ``item.ok``."""

    class Result:
        ok = False
        diff = _create_diff("a.py", "x = 1")
        error = "review rejected the change"

    item = WorkItem(id="w", goal="g", owns_paths=("a.py",))
    verdict = check_acceptance(Result(), item)
    assert not verdict.accepted
    assert "not accepted" in verdict.reasons[0]


def test_a_child_writing_outside_its_assignment_is_rejected():
    """Measured: a child told to touch only ``a.html`` created ``b.html`` too."""

    class Result:
        ok = True
        diff = _create_diff("b.html", "<p>b</p>")
        error = ""

    item = WorkItem(id="w", goal="g", owns_paths=("a.html",))
    verdict = check_acceptance(Result(), item)
    assert not verdict.accepted
    assert "outside its assignment" in verdict.reasons[0]


def test_allocate_work_gives_disjoint_ownership():
    items = allocate_work("fix a.py and b.py", ["a.py", "b.py"])
    assert len(items) == 2
    assert set(items[0].owns_paths).isdisjoint(items[1].owns_paths)


def test_allocate_work_keeps_a_coordinated_change_together():
    """A source file and its test are one unit: splitting them fails both."""
    items = allocate_work("fix a.py and test_a.py", ["a.py", "test_a.py"], coordinated=True)
    assert len(items) == 1
    assert set(items[0].owns_paths) == {"a.py", "test_a.py"}


def test_paths_of_reads_a_creation_diff():
    assert paths_of(_create_diff("x/y.py", "a = 1")) == ["x/y.py"]


# --------------------------------------------------------------------------
# A def that is removed and re-added is not a removal.
# --------------------------------------------------------------------------

def test_editing_a_function_body_does_not_remove_its_public_api():
    """Measured: changing ``return a + b`` to ``return a * b`` was rejected."""
    diff = (
        "--- a/calc.py\n+++ b/calc.py\n"
        "@@ -1,2 +1,2 @@\n"
        "-def mul(a, b):\n"
        "-    return a + b\n"
        "+def mul(a, b):\n"
        "+    return a * b\n"
    )
    # ``.violated``, not ``.ok``: ``ok`` is a classmethod on PrincipleResult, so
    # ``result.ok`` is a bound method and always truthy.  Production code reads
    # ``.violated``; a test that reads ``.ok`` asserts nothing.
    assert not check_preserve_public_apis(diff, None).violated


def test_actually_deleting_a_public_function_is_still_caught():
    diff = (
        "--- a/calc.py\n+++ b/calc.py\n"
        "@@ -1,2 +0,0 @@\n"
        "-def mul(a, b):\n"
        "-    return a * b\n"
    )
    assert check_preserve_public_apis(diff, None).violated


# --------------------------------------------------------------------------
# The plan guard: a symbol-editing tool must name a symbol.
# --------------------------------------------------------------------------

def test_edit_function_with_only_a_path_has_no_target():
    """The exact step the guard was written to reject, accepted by the guard."""
    step = {"tool": "edit_function", "args": {"path": "react.py"}}
    assert not _has_target(step)


def test_edit_function_with_a_symbol_has_a_target():
    step = {"tool": "edit_function", "args": {"name": "run_react"}}
    assert _has_target(step)


def test_edit_file_with_a_location_has_a_target():
    """Measured: ``location`` was not recognised and the only step was dropped."""
    step = {"tool": "edit_file", "args": {"location": "index.html"}}
    assert _has_target(step)


# --------------------------------------------------------------------------
# A missing binary is a property of the machine, not a failure of the work.
# --------------------------------------------------------------------------

def test_resolve_tool_reports_a_missing_binary_without_raising():
    tool = resolve_tool("definitely-not-a-real-binary-xyz")
    assert not tool.found
    assert "not found" in tool.reason


def test_python_bin_resolves_and_gives_a_bare_argv_name():
    """A PATH-resolving sandbox rejects an absolute path in argv."""
    tool = python_bin()
    assert tool.found
    assert "/" not in tool.argv_name


def test_format_code_degrades_when_ruff_is_absent(tmp_path, monkeypatch):
    """``lint()`` was fixed for this; ``format_code()`` one function below was not.

    **This test was not hermetic.**  It patched ``lint_module.resolve_tool``, but
    ``format_code`` imports ``resolve_tool`` from ``gigaforge.l0.toolchain``
    *inside* the function, so the patch never took effect.  On a machine with
    ruff installed the real formatter ran, rewrote ``x=1`` to ``x = 1``, and the
    assertion ``text == "x=1\\n"`` failed — the suite reported 795 passed and 1
    failed depending on what happened to be installed.  A test whose result
    depends on the machine is not a test of the code.

    The patch now targets the module the function actually reads, so the test
    exercises the degradation path whether or not ruff is present.
    """
    from gigaforge.l0 import lint as lint_module
    from gigaforge.l0 import toolchain

    monkeypatch.setattr(
        toolchain,
        "resolve_tool",
        lambda name, override="": toolchain.ResolvedTool(
            name=name, reason="not installed"
        ),
        raising=True,
    )
    target = tmp_path / "x.py"
    target.write_text("x=1\n", encoding="utf-8")
    ok, text = lint_module.format_code(target)
    assert ok, "a missing formatter must not fail the file"
    assert text == "x=1\n", "the file must be returned unchanged, not reformatted"


def test_format_code_uses_the_real_formatter_when_it_is_present(tmp_path):
    """The other half of the same contract: present ruff must actually format.

    Without this, the test above could pass by never formatting at all.
    """
    from gigaforge.l0 import lint as lint_module
    from gigaforge.l0.toolchain import resolve_tool

    if not resolve_tool("ruff").found:
        pytest.skip("ruff is not installed on this machine")
    target = tmp_path / "y.py"
    target.write_text("y=1\n", encoding="utf-8")
    ok, text = lint_module.format_code(target)
    assert ok
    assert text == "y = 1\n", "ruff format should have normalised the spacing"


# --------------------------------------------------------------------------
# The status must follow the verdict.
# --------------------------------------------------------------------------

def test_a_rejected_artifact_cannot_report_succeeded():
    """Measured: ``status=succeeded accepted=False`` on a rejected page."""
    from gigaforge.core.pipeline import PipelineResult

    result = PipelineResult(
        run_id="r", goal="g", status="rejected", accepted=False,
    )
    assert result.status == "rejected"
    assert not result.accepted


def test_contract_unmet_travels_with_the_result():
    from gigaforge.core.pipeline import PipelineResult

    result = PipelineResult(
        run_id="r", goal="g", status="rejected", accepted=False,
        contract_unmet=["colour 'magenta'"],
    )
    assert result.to_dict()["contract_unmet"] == ["colour 'magenta'"]
