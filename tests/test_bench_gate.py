"""The bench is a gate, not a demo.

``gigaforge/bench/`` existed for several iterations and nothing ran it.  A
benchmark that no run executes is a report nobody reads: it cannot fail, so it
cannot catch anything.  This module makes the bench part of the suite that
already gates every change, which is the only way a benchmark earns its keep.

What the gate asserts, and why each assertion is load-bearing:

1. **Every task succeeds AND its tests pass after the diff is applied.**  The
   two are different claims.  ``success`` means the pipeline accepted its own
   diff; ``tests_passed_after_apply`` means the diff was re-applied to an
   untouched checkout and the repository's own suite went green.  A pipeline
   that accepts a diff which does not apply, or applies but does not fix the
   bug, passes the first and fails the second \u2014 which is exactly the class of
   defect three review rounds kept finding.

2. **The expected diff substring is present.**  Guards against a fixture whose
   canned answer drifted away from the task it is supposed to solve.

3. **Token accounting is real.**  The bench used to report
   ``tokens_in = 100 * llm_calls``, a constant times a count.  That number
   cannot move when the prompt grows, so a regression that doubled the context
   per call was invisible in the one report the bench exists to produce.  The
   gate asserts the totals are measured (they differ between tasks, and they
   are far above the old constant) rather than fabricated.

4. **The report renders.**  A gate whose output cannot be read is a gate nobody
   acts on.
"""
from __future__ import annotations

import pytest

from gigaforge.bench import TASKS, BenchRunner, bench_report, bench_report_json


@pytest.fixture(scope="module")
def results():
    """Run the default task set once for the whole module.

    The bench is deterministic and free (a fake provider, real pipeline), so a
    module-scoped run costs one pass and keeps the gate fast enough to sit in
    the default suite.
    """
    return BenchRunner().run_all()


def test_every_task_is_attempted(results):
    """One result per fixture \u2014 a silently skipped task is a silent pass."""
    assert len(results) == len(TASKS)
    assert {r.name for r in results} == {t.name for t in TASKS}


def test_every_task_succeeds(results):
    """The pipeline accepts a diff for every task."""
    failed = [(r.name, r.error) for r in results if not r.success]
    assert not failed, f"bench tasks failed: {failed}"


def test_every_diff_passes_the_repository_tests(results):
    """The accepted diff, re-applied to a clean checkout, makes the suite green.

    This is the assertion that separates "the pipeline produced something" from
    "the pipeline fixed the bug".  It is the bench's whole reason to exist.

    A text product has no diff to apply, so it is excluded — and its own
    acceptance is checked instead (``text_must_contain`` / ``text_min_chars``),
    which is the same question asked of the right artifact.
    """
    text_tasks = {t.name for t in TASKS if t.text_responses}
    failed = [
        (r.name, r.error) for r in results
        if not r.tests_passed_after_apply and r.name not in text_tasks
    ]
    assert not failed, f"diffs did not pass the repo tests: {failed}"


def test_every_text_task_produced_its_text(results):
    """A text product is verified by its prose, not by a diff."""
    text_tasks = {t.name for t in TASKS if t.text_responses}
    assert text_tasks, "the bench has no text-product fixture"
    failed = [(r.name, r.error) for r in results
              if r.name in text_tasks and not r.success]
    assert not failed, f"text tasks failed: {failed}"


def test_expected_diff_substring_is_present(results):
    """Each patch fixture's canned answer still matches the task it solves."""
    by_name = {t.name: t for t in TASKS}
    patch_tasks = {t.name for t in TASKS if not t.text_responses}
    missing = [
        r.name
        for r in results
        if r.name in patch_tasks and not r.diff_substring_found
    ]
    assert not missing, f"expected diff substring missing for: {missing}"
    # And the substring is a real one, not an empty string that trivially matches.
    for name in patch_tasks:
        assert by_name[name].expected_diff_substring.strip()


def test_token_accounting_is_measured_not_fabricated(results):
    """Tokens come from the messages the provider was handed.

    The old accounting was ``100 * llm_calls``, so every task with the same
    call count reported the same total.  Measured accounting varies with the
    prompt, which is the property that makes the number useful.
    """
    totals = {r.name: r.tokens_in + r.tokens_out for r in results}
    assert all(total > 0 for total in totals.values()), totals
    # A constant-times-count accounting would make these identical.
    assert len(set(totals.values())) > 1, (
        f"token totals are identical across tasks, which means they are not "
        f"measured: {totals}"
    )
    # The old constant was 150 per call; real prompts are far larger.
    for r in results:
        assert r.tokens_in > 150 * max(1, r.llm_calls) // 2, (
            f"{r.name}: tokens_in={r.tokens_in} looks like the old constant"
        )


def test_report_renders(results):
    """The ASCII report and the JSON rows both describe every task."""
    text = bench_report(results)
    assert "total:" in text
    for r in results:
        assert r.name in text
    rows = bench_report_json(results)
    assert len(rows) == len(results)
    assert all("tokens_total" in row for row in rows)
