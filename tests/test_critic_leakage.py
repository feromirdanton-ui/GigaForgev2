"""The leakage screen must catch a lookup table and pass an honest harness.

**The defect this pins.**  GigaForge improves itself against a fixed set of
tasks.  A harness measured on a fixed set will eventually learn the set, and the
failure is silent: a green bench and a worse product.  There was no mechanism
that could even ask the question, so "did we just hardcode the fixture?" was
answered by reading the diff and hoping.

**The other half of the test.**  A screen that rejects honest code is worse than
no screen, because it gets switched off.  So every "catches" test is paired with
a "passes" test on the same shape of code written correctly.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gigaforge.harness.critic import screen, screen_diff  # noqa: E402


# ── it must catch leakage ────────────────────────────────────────────────────

def test_a_task_name_in_a_branch_is_leakage() -> None:
    """The canonical shape: a lookup table keyed by the fixture's name."""
    code = '''
def build(goal):
    if goal == "cyberpunk landing page":
        return _the_page_we_know_works()
    return _generic(goal)
'''
    report = screen(code, goal="build a cyberpunk landing page",
                    task_names=["cyberpunk landing page"])
    assert report.leaked
    kinds = {f.kind for f in report.findings}
    assert "task_name" in kinds or "suite_logic" in kinds, kinds


def test_a_goal_literal_hardcoded_is_leakage() -> None:
    """A distinctive value from the goal, where the harness should derive it."""
    code = 'ACCENT = "#00ffcc"\n'
    report = screen(code, goal="build a page with #00ffcc accents")
    assert report.leaked
    assert any(f.kind == "goal_literal" for f in report.findings), report.to_dict()


def test_suite_specific_markers_are_leakage() -> None:
    for code in (
        "if task == 'build_page':\n    pass\n",
        "if name in {'a', 'b'}:\n    pass\n",
        "expected = expected_output\n",
        "data = load('golden.json')\n",
    ):
        report = screen(code, goal="do the thing")
        assert report.leaked, code
        assert any(f.kind == "suite_logic" for f in report.findings), code


def test_a_removed_leak_is_not_a_leak() -> None:
    """Screening the whole diff would reject the change that fixes a leak."""
    diff = (
        "--- a/h.py\n"
        "+++ b/h.py\n"
        "@@ -1,3 +1,2 @@\n"
        "-    if goal == 'cyberpunk landing page':\n"
        "-        return _known()\n"
        "+    return _generic(goal)\n"
    )
    report = screen_diff(diff, goal="build a cyberpunk landing page",
                         task_names=["cyberpunk landing page"])
    assert report.clean, report.to_dict()


# ── it must NOT catch honest code ────────────────────────────────────────────

def test_an_honest_harness_passes() -> None:
    """The code a real harness writes: generic, derived, no task names."""
    code = '''
def build_page(goal: str, palette: list[str]) -> str:
    """Build a page for any goal.

    This docstring may name the task -- that is documentation, not a lookup.
    """
    sections = plan_sections(goal)
    accent = palette[0] if palette else derive_accent(goal)
    return render(sections, accent=accent)
'''
    report = screen(code, goal="build a cyberpunk landing page",
                    task_names=["cyberpunk landing page"])
    assert report.clean, report.to_dict()


def test_a_comment_may_name_the_task() -> None:
    """Documentation is allowed to say why the code exists."""
    code = (
        "# The cyberpunk landing page fixture is why this branch exists.\n"
        "if not sections:\n"
        "    sections = default_sections()\n"
    )
    report = screen(code, goal="build a cyberpunk landing page",
                    task_names=["cyberpunk landing page"])
    assert report.clean, report.to_dict()


def test_common_words_are_not_leakage() -> None:
    """A goal says 'build a landing page'; mentioning 'page' is the job."""
    code = 'def render_page(sections):\n    return "<html>" + sections + "</html>"\n'
    report = screen(code, goal="build a landing page")
    assert report.clean, report.to_dict()


def test_short_literals_are_not_leakage() -> None:
    """A six-character floor keeps ordinary identifiers out of the screen."""
    code = 'MODE = "fast"\nNAME = "x"\n'
    report = screen(code, goal="run fast")
    assert report.clean, report.to_dict()


# ── the screen is wired into acceptance ──────────────────────────────────────

def test_acceptance_rejects_a_leaked_child_diff() -> None:
    """The screen runs before the verdict, so a leaked diff is never merged."""
    from gigaforge.core.acceptance import WorkItem, check_acceptance

    class _Result:
        ok = True
        error = ""
        diff = (
            "--- a/h.py\n"
            "+++ b/h.py\n"
            "@@ -1,2 +1,4 @@\n"
            " def build(goal):\n"
            "+    if goal == 'cyberpunk landing page':\n"
            "+        return _known()\n"
            "     return _generic(goal)\n"
        )

    item = WorkItem(id="cyberpunk landing page", goal="build a cyberpunk landing page",
                    owns_paths=("h.py",))
    verdict = check_acceptance(_Result(), item)
    assert not verdict.accepted
    assert any("encodes the task" in r for r in verdict.reasons), verdict.reasons


def test_acceptance_still_accepts_an_honest_child_diff() -> None:
    from gigaforge.core.acceptance import WorkItem, check_acceptance

    class _Result:
        ok = True
        error = ""
        diff = (
            "--- a/h.py\n"
            "+++ b/h.py\n"
            "@@ -1,2 +1,3 @@\n"
            " def build(goal):\n"
            "+    sections = plan_sections(goal)\n"
            "     return _generic(goal)\n"
        )

    item = WorkItem(id="cyberpunk landing page", goal="build a cyberpunk landing page",
                    owns_paths=("h.py",))
    verdict = check_acceptance(_Result(), item)
    assert verdict.accepted, verdict.reasons
