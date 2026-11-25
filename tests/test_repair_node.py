"""The repair must be reachable, and the diff must match the tree.

Two defects, both measured:

* **N2 — the repair was unreachable.**  It sat at the top of
  ``_check_goal_contract``, after ``if not requirements: return None``.
  ``extract_requirements`` returns zero requirements for "Create a single-file
  index.html", "Build a landing page" and "Make a website for my bakery" — so
  for three of the four ordinary ways to ask for a page, the repair never ran
  and a broken page passed.
* **N8 — ``final_diff`` described the pre-repair file.**  The repair wrote the
  file on disk, but the diff had already been computed.  Measured: the tree had
  1 section where ``final_diff`` had 4.

The tests drive the real node with a real file on disk, because the defect was
about *reachability on the product path*, not about the repair function — that
one was already covered by 265 lines of tests and still never ran.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from gigaforge.core.pipeline import TaskPipeline, _merge_repaired_diff

BROKEN_PAGE = (
    "<!DOCTYPE html>\n<html><head><style>body{color:#fff;background:#000}</style></head>\n"
    "<body>\n"
    "body{color:#fff;background:#000}\n"
    '<section class="hero"><h1>GigaForge</h1></section>\n'
    '<section class="features"><div class="feature-card"><h2>Plan</h2></div></section>\n'
    '<section class="features"><div class="feature-card"><h2>Plan</h2></div></section>\n'
    "</body></html>\n"
)


class _Validation:
    def __init__(self, files: list[str]) -> None:
        self.accepted = True
        self.changed_files = files


class _Ctx:
    def __init__(self, goal: str = "Create a single-file index.html") -> None:
        self.run_id = "test-run"
        self.state = type("S", (), {"goal": goal, "artifacts": {}, "add_note": lambda *a, **k: None})()
        self.extras: dict = {}


class _Stub(TaskPipeline):
    def __init__(self, root: Path) -> None:  # noqa: D107 - bypass the constructor
        self.config = type("C", (), {"repo_root": str(root)})()
        self.events: list[tuple[str, dict]] = []

    def _event(self, name: str, **payload) -> None:
        self.events.append((name, payload))

    def _repo_root(self) -> Path:
        return Path(self.config.repo_root)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    (tmp_path / "README.md").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp_path, check=True)
    return tmp_path


class TestRepairIsReachable:
    def test_repair_runs_for_a_goal_with_no_requirements(self, repo: Path) -> None:
        """'Create a single-file index.html' yields zero requirements — the old
        early return meant the repair never ran for it."""
        from gigaforge.harness.goal_contract import extract_requirements

        assert extract_requirements("Create a single-file index.html") == []

        (repo / "index.html").write_text(BROKEN_PAGE, encoding="utf-8")
        pipeline = _Stub(repo)
        ctx = _Ctx("Create a single-file index.html")
        out = pipeline._node_repair(ctx, {"validation": _Validation(["index.html"])})

        assert out["repair_report"]["files"] == ["index.html"]
        assert out["repair_report"]["actions"], "the repair did nothing"
        page = (repo / "index.html").read_text(encoding="utf-8")
        assert page.count('class="features"') == 1, "the duplicate section survived"
        assert "body{color:#fff;background:#000}\n<section" not in page

    def test_repair_runs_for_a_bare_landing_page_goal(self, repo: Path) -> None:
        (repo / "index.html").write_text(BROKEN_PAGE, encoding="utf-8")
        pipeline = _Stub(repo)
        ctx = _Ctx("Build a landing page")
        out = pipeline._node_repair(ctx, {"validation": _Validation(["index.html"])})
        assert out["repair_report"]["actions"]

    def test_repair_is_a_noop_on_a_clean_page(self, repo: Path) -> None:
        clean = (
            "<!DOCTYPE html><html><head><style>body{color:#fff}</style></head>"
            '<body><section class="hero"><h1>Hi</h1></section></body></html>'
        )
        (repo / "index.html").write_text(clean, encoding="utf-8")
        pipeline = _Stub(repo)
        out = pipeline._node_repair(_Ctx(), {"validation": _Validation(["index.html"])})
        assert out["repair_report"]["actions"] == []
        assert (repo / "index.html").read_text(encoding="utf-8") == clean

    def test_repair_ignores_non_html_files(self, repo: Path) -> None:
        (repo / "db.py").write_text("X = 1\n", encoding="utf-8")
        pipeline = _Stub(repo)
        out = pipeline._node_repair(_Ctx(), {"validation": _Validation(["db.py"])})
        assert out["repair_report"]["files"] == []

    def test_repair_node_is_wired_before_settle(self) -> None:
        import inspect

        source = inspect.getsource(TaskPipeline.build)
        assert 'name="repair"' in source
        assert source.index('name="repair"') < source.index('name="settle"')
        # settle must depend on it, or the DAG may run them in either order.
        assert '"repair_report"' in source

    def test_settle_rejects_duplicate_ids_and_nested_heads(self, repo: Path) -> None:
        from types import SimpleNamespace

        (repo / "index.html").write_text(
            '<html><head><head><title>Club</title></head></head><body>'
            '<section id="models"></section><section id="models"></section>'
            "</body></html>",
            encoding="utf-8",
        )
        pipeline = _Stub(repo)
        pipeline._check_goal_contract = lambda _ctx, _files: None
        pipeline._check_render = lambda _ctx, _files: []
        pipeline._check_code_contract = lambda _ctx, _files: []
        ctx = _Ctx()

        result = pipeline._node_settle(
            ctx,
            {
                "validation": _Validation(["index.html"]),
                "proposal": SimpleNamespace(diff="validated diff"),
                "review_verdict": {"verdict": "ok"},
            },
        )

        assert result["accepted"] is False
        assert any("id 'models' appears 2 times" in item
                   for item in ctx.extras["contract_unmet"])


class TestExplicitRequirementsSurvivePlanContracts:
    def test_contract_failures_are_passed_to_the_repair_loop(
        self, repo: Path,
    ) -> None:
        from types import SimpleNamespace

        goal = (
            "Create index.html with cyan and magenta colors, reduced-motion "
            "support, a working membership form with validation and a success "
            "state, and working responsive mobile navigation."
        )
        (repo / "index.html").write_text(
            "<!DOCTYPE html><html><head><title>Club</title></head>"
            "<body><h1>Club</h1><p>" + "Community events. " * 40 + "</p></body></html>",
            encoding="utf-8",
        )
        pipeline = _Stub(repo)
        pipeline.router = object()
        pipeline._file_context = lambda _goal: "index.html current content"
        feedback: list[str] = []

        class _FixLoop:
            def run(self, _task, **kwargs):
                feedback.append(kwargs["tests_output"])
                return SimpleNamespace(
                    accepted=True,
                    diff="validated repair diff",
                    to_dict=lambda: {"accepted": True},
                )

        pipeline.fixloop = _FixLoop()
        ctx = _Ctx(goal)
        ctx.extras.update(self_debug=False, max_fix_iterations=2)

        out = pipeline._node_fix(
            ctx, {"validation": _Validation(["index.html"])}
        )

        assert out["fixed_diff"] and "index.html" in out["fixed_diff"]
        assert len(feedback) == 2
        assert feedback and "reduced-motion support" in feedback[0]
        assert "working responsive mobile navigation" in feedback[0]
        assert "a working form with validation and a success state" in feedback[0]
        assert "@media (prefers-reduced-motion: reduce)" in feedback[0]
        assert "aria-controls" in feedback[0]
        assert "rgba(0, 255, 255, 0.18)" in feedback[0]
        assert "rgba(255, 0, 255, 0.16)" in feedback[0]
        assert "Bind submit handling to the actual <form>" in feedback[0]
        assert "do not append markup or styles after </html>" in feedback[0]

    def test_plan_contract_also_checks_explicit_user_requirements(
        self, repo: Path
    ) -> None:
        goal = (
            "Create model cards (snnTorch, Brian2, NEST, Norse) with reduced-motion "
            "support and a working join form with client-side validation and a "
            "success state."
        )
        page = (
            "<!DOCTYPE html><html><head><style>body{color:#fff}</style></head>"
            "<body><section class='hero'><h1>Neural club</h1>"
            + "<p>Community learning and events for spiking neural network "
            "enthusiasts.</p>" * 35
            + "</section></body></html>"
        )
        (repo / "index.html").write_text(page, encoding="utf-8")
        pipeline = _Stub(repo)
        ctx = _Ctx(goal)
        ctx.extras["generation"] = {"sections": ["hero"]}
        ctx.extras["generation_plan"] = {
            "path": "index.html",
            "sections": ["hero"],
            "palette": [],
            "title": "Neural club",
        }

        verdict = pipeline._check_goal_contract(ctx, ["index.html"])

        assert verdict is not None
        assert "include 'snnTorch'" in verdict.unmet
        assert "reduced-motion support" in verdict.unmet
        assert "a working form with validation and a success state" in verdict.unmet

    def test_text_artifact_does_not_inherit_the_html_page_contract(
        self, repo: Path
    ) -> None:
        goal = (
            "Create a 700-900 word guide with at least 3,000 characters in "
            "welcome.txt."
        )
        (repo / "welcome.txt").write_text("Welcome! " * 20, encoding="utf-8")
        pipeline = _Stub(repo)
        ctx = _Ctx(goal)
        ctx.extras["generation"] = {"sections": ["header", "features"]}
        ctx.extras["generation_plan"] = {
            "path": "index.html",
            "sections": ["header", "features"],
            "palette": ["cyan"],
            "title": "Welcome",
        }

        verdict = pipeline._check_goal_contract(ctx, ["welcome.txt"])

        assert verdict is not None
        assert any(
            item.startswith("between 700 and 900 words") for item in verdict.unmet
        )
        assert any(
            item.startswith("at least 3000 characters") for item in verdict.unmet
        )
        assert not any("section 'header'" in item for item in verdict.unmet)
        assert not any("a headline (<h1>)" in item for item in verdict.unmet)
        assert "colour 'cyan'" not in verdict.unmet

    def test_goal_feedback_does_not_become_text_artifact_requirements(
        self, repo: Path
    ) -> None:
        original_goal = (
            "Create a 700-900 word guide with at least 3,000 characters in "
            "welcome.txt."
        )
        goal_with_feedback = (
            original_goal
            + "\n\nLONG-LIVED GOAL MODE — CONTINUE THE EXISTING TASK\n"
            + "Previous cycle feedback: diff could not be parsed: hunk without "
            "a file header and no path hint."
        )
        (repo / "welcome.txt").write_text("Welcome! " * 20, encoding="utf-8")
        pipeline = _Stub(repo)
        ctx = _Ctx(goal_with_feedback)
        ctx.extras["generation"] = {"sections": ["header", "features"]}

        verdict = pipeline._check_goal_contract(ctx, ["welcome.txt"])

        assert verdict is not None
        assert any(
            item.startswith("between 700 and 900 words") for item in verdict.unmet
        )
        assert any(
            item.startswith("at least 3000 characters") for item in verdict.unmet
        )
        assert not any("section 'header'" in item for item in verdict.unmet)
        assert not any("a headline (<h1>)" in item for item in verdict.unmet)


class TestReActUsageAccounting:
    def test_plan_ledger_is_included_in_pipeline_usage(self, tmp_path: Path) -> None:
        from types import SimpleNamespace

        from gigaforge.core.state import TaskState

        class Ledger:
            tokens_in = 120
            tokens_out = 30
            llm_calls = 3
            cost_micros = 24
            compactions = 0
            retries = 0

            def write(self, path: Path) -> None:
                Path(path).write_text("{}", encoding="utf-8")

        class Strategy:
            planning_goal = ""
            planning_kwargs = {}

            def plan_under_harness(self, *_args, **_kwargs):
                self.planning_goal = _args[0]
                self.planning_kwargs = _kwargs
                return (
                    SimpleNamespace(
                        steps=["inspect: index.html"],
                        structured=[{"tool": "inspect", "args": {"path": "index.html"}}],
                        localization={},
                        strategy="react",
                        score=1.0,
                        judge_used=False,
                        to_dict=lambda: {},
                    ),
                    Ledger(),
                )

        pipeline = object.__new__(TaskPipeline)
        pipeline.router = object()
        pipeline.plan_strategy = Strategy()
        pipeline.config = SimpleNamespace(trace_path=str(tmp_path / "traces" / "core.jsonl"))
        pipeline._event = lambda *_args, **_kwargs: None
        state = TaskState(run_id="test-run", goal="Inspect the page")
        ctx = SimpleNamespace(
            run_id="test-run",
            state=state,
            extras={
                "files": ["index.html"],
                "focus_paths": ["src/app.py"],
                "use_subagents": True,
            },
        )

        pipeline._node_plan(ctx, {"slice": None})

        assert state.total_tokens() == 150
        assert state.total_calls() == 3
        assert state.phase_usages["react"]["calls"] == 3
        assert state.cost_micros == 24
        assert state.phase_cost_micros["react"] == 24
        assert "use these paths directly; do not search for them" in pipeline.plan_strategy.planning_goal
        assert "do not read or edit the target files in this stage" in pipeline.plan_strategy.planning_goal
        assert "Finish immediately with a concise executable plan" in pipeline.plan_strategy.planning_goal
        assert pipeline.plan_strategy.planning_kwargs["read_only"] is True
        assert "- index.html" in pipeline.plan_strategy.planning_goal
        assert "- src/app.py" in pipeline.plan_strategy.planning_goal


def test_independent_subagent_targets_skip_unneeded_model_planning(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from gigaforge.core.state import TaskState

    for name in ("temperature.py", "distance.py", "duration.py"):
        (tmp_path / name).write_text("value = 1\n", encoding="utf-8")
    goal = (
        "Improve temperature.py, distance.py, and duration.py independently "
        "while preserving their public functions."
    )

    class Strategy:
        name = "react"

        def plan_under_harness(self, *_args, **_kwargs):
            raise AssertionError("independent subagent work should not call the LLM planner")

    pipeline = object.__new__(TaskPipeline)
    pipeline.router = object()
    pipeline.plan_strategy = Strategy()
    pipeline.config = SimpleNamespace(
        repo_root=str(tmp_path),
        trace_path=str(tmp_path / ".gigaforge" / "traces" / "core.jsonl"),
    )
    pipeline.firewall = SimpleNamespace(config=None)
    pipeline._event = lambda *_args, **_kwargs: None
    state = TaskState(run_id="subagent-plan", goal=goal)
    ctx = SimpleNamespace(
        run_id="subagent-plan",
        state=state,
        extras={"use_subagents": True},
    )

    result = pipeline._node_plan(ctx, {"slice": None})

    assert result["plan_meta"]["strategy"] == "subagents"
    assert all(
        name in result["plan"][0]
        for name in ("temperature.py", "distance.py", "duration.py")
    )
    assert ctx.extras["episode_written_files"] == []


def test_subagent_assigns_new_function_to_the_related_module(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from gigaforge.core.pipeline import _added_function_definitions

    (tmp_path / "temperature.py").write_text(
        "def celsius_to_fahrenheit(value): return value\n",
        encoding="utf-8",
    )
    (tmp_path / "distance.py").write_text(
        "def kilometers_to_miles(value): return value\n",
        encoding="utf-8",
    )
    (tmp_path / "duration.py").write_text(
        "def seconds_to_hms(value): return str(value)\n",
        encoding="utf-8",
    )
    pipeline = object.__new__(TaskPipeline)
    pipeline.config = SimpleNamespace(repo_root=str(tmp_path))

    owners = pipeline._subagent_symbol_owners(
        "Add the public function hms_to_seconds(hms: str) -> int.",
        ["temperature.py", "distance.py", "duration.py"],
    )

    assert owners == {"hms_to_seconds": "duration.py"}
    assert _added_function_definitions(
        "diff --git a/temperature.py b/temperature.py\n"
        "+++ b/temperature.py\n"
        "@@ -1,0 +1 @@\n"
        "+def hms_to_seconds(hms: str) -> int:\n"
    ) == [("temperature.py", "hms_to_seconds")]


class TestTaskBudgetCallLimit:
    def test_model_call_output_is_limited_by_remaining_task_budget(self) -> None:
        from types import SimpleNamespace

        class State:
            def total_tokens(self) -> int:
                return 500

            def messages(self, *, extra):
                return [{"role": "user", "content": "x" * 300}]

        class Router:
            config = SimpleNamespace(
                budget_for=lambda _phase: SimpleNamespace(max_output_tokens=800)
            )
            max_tokens = None

            def complete(self, _phase, _messages, **kwargs):
                self.max_tokens = kwargs["max_tokens"]
                return SimpleNamespace(content="bounded")

        pipeline = object.__new__(TaskPipeline)
        pipeline.router = Router()
        ctx = SimpleNamespace(state=State(), extras={"budget_tokens": 1000})

        assert pipeline._ask("code", "write", ctx) == "bounded"
        assert pipeline.router.max_tokens == 396

    def test_call_is_refused_when_the_prompt_exceeds_remaining_budget(self) -> None:
        from types import SimpleNamespace

        from gigaforge.core.pipeline import PipelineError

        class State:
            def total_tokens(self) -> int:
                return 950

            def messages(self, *, extra):
                return [{"role": "user", "content": "x" * 300}]

        class Router:
            called = False

            def complete(self, *_args, **_kwargs):
                self.called = True
                return SimpleNamespace(content="unbounded")

        pipeline = object.__new__(TaskPipeline)
        pipeline.router = Router()
        ctx = SimpleNamespace(state=State(), extras={"budget_tokens": 1000})

        with pytest.raises(PipelineError, match="budget exhausted"):
            pipeline._ask("code", "write", ctx)
        assert not pipeline.router.called


class TestDiffMatchesTheTree:
    def test_final_diff_is_recomputed_after_repair(self, repo: Path) -> None:
        (repo / "index.html").write_text(BROKEN_PAGE, encoding="utf-8")
        pipeline = _Stub(repo)
        ctx = _Ctx()
        pipeline._node_repair(ctx, {"validation": _Validation(["index.html"])})
        recomputed = ctx.extras.get("repaired_diff") or ""
        assert recomputed, "the diff was not recomputed after the repair"
        # The recomputed diff must describe the repaired file, not the broken one.
        assert recomputed.count('class="features"') == 1

    def test_settle_uses_the_repaired_diff(self, repo: Path) -> None:
        (repo / "index.html").write_text(BROKEN_PAGE, encoding="utf-8")
        pipeline = _Stub(repo)
        ctx = _Ctx()
        pipeline._node_repair(ctx, {"validation": _Validation(["index.html"])})
        stale = "diff --git a/index.html b/index.html\n--- a/index.html\n+++ b/index.html\n"
        out = pipeline._node_settle(ctx, {
            "validation": _Validation(["index.html"]),
            "proposal": type("P", (), {"diff": stale})(),
            "review_verdict": {},
        })
        assert out["final_diff"] != stale
        assert out["final_diff"].count('class="features"') == 1


class TestMergeRepairedDiff:
    def test_replaces_only_the_repaired_file(self) -> None:
        original = (
            "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x\n+y\n"
            "diff --git a/index.html b/index.html\n--- a/index.html\n+++ b/index.html\n@@ -1 +1 @@\n-old\n"
        )
        repaired = (
            "diff --git a/index.html b/index.html\n--- a/index.html\n+++ b/index.html\n"
            "@@ -1 +1 @@\n+new\n"
        )
        merged = _merge_repaired_diff(original, repaired)
        assert "+y" in merged, "an unrelated file's hunk was lost"
        assert "+new" in merged
        assert "-old" not in merged

    def test_empty_repaired_keeps_the_original(self) -> None:
        assert _merge_repaired_diff("diff --git a/x b/x\n", "") == "diff --git a/x b/x\n"

    def test_empty_original_returns_the_repaired(self) -> None:
        assert _merge_repaired_diff("", "diff --git a/x b/x\n") == "diff --git a/x b/x\n"
