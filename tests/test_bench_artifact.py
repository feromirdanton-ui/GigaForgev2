"""The bench must measure the artifact path, not only the code path.

**The defect.**  The bench had four tasks and every one of them was a Python
bug fix.  So the bench could not see the sectional generation, the plan-derived
contract, the HTML repair or the render check at all: a regression that broke
page generation outright would have left the bench green.  A benchmark that
only covers the path you already trust is a regression test for the past, not a
measurement of the product.

It also was not wired to CI, so even those four tasks only ran when someone
remembered to run them.

These tests pin the fixture, the section-aware fake provider, and the CLI's
``--out`` contract that CI gates on.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from gigaforge.bench.harness import (
    TASKS,
    BenchFakeGigaClient,
    BenchRunner,
    TaskFixture,
    _section_kind_from_prompt,
)


class TestTheTaskSetCoversArtifacts:
    def test_there_is_an_html_task(self) -> None:
        page_tasks = [t for t in TASKS if t.section_responses]
        assert page_tasks, "the bench has no page task: the artifact path is unmeasured"

    def test_the_page_task_has_structural_acceptance(self) -> None:
        page = next(t for t in TASKS if t.section_responses)
        assert page.artifact_must_contain, "a page task needs structural facts"
        assert page.artifact_min_bytes > 0

    def test_the_page_task_has_no_test_command(self) -> None:
        """A page has no pytest run; its acceptance is the artifact check."""
        page = next(t for t in TASKS if t.section_responses)
        assert page.test_command == []

    def test_every_code_task_still_has_a_test_command(self) -> None:
        for task in TASKS:
            if not task.section_responses:
                assert task.test_command, f"{task.name} has no way to be verified"


class TestSectionAwareFakeProvider:
    def test_the_kind_is_read_from_the_prompt(self) -> None:
        prompt = "Write ONE section of a single-file landing page: the Hero section (kind: hero)."
        assert _section_kind_from_prompt(prompt) == "hero"

    def test_an_unknown_prompt_falls_back_to_a_catalogue_word(self) -> None:
        assert _section_kind_from_prompt("write the footer please") == "footer"

    def test_an_unrecognisable_prompt_returns_empty(self) -> None:
        assert _section_kind_from_prompt("do something") == ""

    def test_answers_are_chosen_by_kind_not_position(self) -> None:
        """Concurrent sections must not get each other's answers.

        The sections run in a ThreadPoolExecutor, so a positional list would
        hand the hero's answer to the footer depending on which thread won.
        """
        import asyncio

        fixture = TaskFixture(
            name="t",
            repo_files={},
            task="t",
            target_function="",
            target_file="",
            plan_response="{}",
            code_responses=[],
            review_responses=[],
            test_command=[],
            expected_diff_substring="",
            section_responses={"hero": "HERO", "footer": "FOOTER"},
        )
        provider = BenchFakeGigaClient(fixture)

        async def ask(kind: str) -> str:
            result = await provider.chat(
                [{"role": "user", "content": f"... (kind: {kind})"}], phase="code"
            )
            return result.content

        hero = asyncio.run(ask("hero"))
        footer = asyncio.run(ask("footer"))
        # Ask for the hero again: a positional list would now be empty.
        hero_again = asyncio.run(ask("hero"))
        assert hero == "HERO"
        assert footer == "FOOTER"
        assert hero_again == "HERO"


class TestTheBenchRuns:
    def test_all_tasks_pass(self, tmp_path: Path) -> None:
        """The bench is the product's own measurement: it must be green."""
        results = BenchRunner(work_root=tmp_path).run_all()
        failed = [(r.name, r.error) for r in results if not r.success]
        assert not failed, f"bench failures: {failed}"
        assert len(results) == len(TASKS)

    def test_the_page_task_writes_a_real_page(self, tmp_path: Path) -> None:
        results = BenchRunner(work_root=tmp_path).run_all()
        page = next(r for r in results if r.name == "page-cyberpunk-landing")
        assert page.success
        written = tmp_path / "page-cyberpunk-landing" / "repo" / "index.html"
        assert written.is_file()
        body = written.read_text(encoding="utf-8")
        assert body.count("<footer") == 1
        assert body.count("<h1") == 1
        assert body.count("<style") == 1


class TestTheCliContractCiGatesOn:
    def test_out_writes_json_and_exits_zero(self, tmp_path: Path) -> None:
        out = tmp_path / "bench.json"
        proc = subprocess.run(
            [sys.executable, "-m", "gigaforge.cli", "bench", "--out", str(out)],
            capture_output=True, text=True, timeout=600,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        rows = json.loads(out.read_text(encoding="utf-8"))
        assert isinstance(rows, list) and len(rows) == len(TASKS)
        assert all(row["success"] for row in rows)
