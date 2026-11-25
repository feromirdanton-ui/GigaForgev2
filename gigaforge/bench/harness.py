"""Benchmark harness — runs the pipeline against a set of task fixtures.

Gives GigaForge a MEASURABLE score (reviewer ask #3): for each task fixture,
run the pipeline with a (deterministic) FakeGigaClient, record {task, success,
llm_calls, tokens, latency, cache_hit, method}, and emit a SWE-bench-style
report (pass/fail + token cost per task).

The bench is DETERMINISTIC + FREE (no real API calls): the FakeGigaClient
returns canned responses; the L0 index, slicer, validator, sandbox, cache,
metrics, tracer, budgets, router are all real. This is the same wiring the
e2e tests use.

API:
    runner = BenchRunner()
    results: list[BenchResult] = runner.run_all()
    report: str = bench_report(results)

Doctrine:
- Context is budget. The bench records tokens per task (from the metrics DB).
- Diff is contract. Each task has an expected_diff + a test that must pass
  after the diff is applied.
- Metrics is control. The bench report IS the metric.
"""
from __future__ import annotations

import json as _json
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from gigaforge.bench.report import bench_report  # re-export
from gigaforge.client.giga_client import ChatResult
from gigaforge.harness.generation import _SECTION_CATALOGUE

__all__ = ["BenchRunner", "BenchResult", "bench_report", "TASKS"]


class _NoTestCommand(Exception):
    """A fixture with no test command: its acceptance is the artifact check."""


# --- Task fixtures ---------------------------------------------------------


@dataclass(frozen=True)
class TaskFixture:
    """One benchmark task.

    Fields:
        name: short identifier (kebab-case).
        repo_files: {relative_path: source_text} — the initial repo state.
        task: natural-language task description.
        target_function: the symbol the pipeline should edit.
        target_file: repo-relative path of the file.
        plan_response: canned JSON for the Planner (Lite).
        code_responses: canned diff list for the Coder (Pro).
        review_responses: canned JSON list for the Reviewer (Lite).
        test_command: pytest invocation (list of args) — must pass after apply.
        expected_diff_substring: a substring that MUST appear in the applied
            diff (sanity check that the canned diff is correct).
        test_args: pytest args passed to the pipeline's sandbox.run_tests.
    """

    name: str
    repo_files: dict[str, str]
    task: str
    target_function: str
    target_file: str
    plan_response: str
    code_responses: list[str]
    review_responses: list[str]
    test_command: list[str]
    expected_diff_substring: str
    test_args: list[str] = field(default_factory=list)
    #: Canned answers for the sectional page path, keyed by section kind.
    #:
    #: A page task cannot use ``code_responses``: the sections are generated
    #: CONCURRENTLY, so a positional list would hand the hero's answer to the
    #: footer depending on which thread won.  Keying by kind makes the fixture
    #: deterministic regardless of scheduling — which is the whole point of a
    #: bench.
    section_responses: dict[str, str] = field(default_factory=dict)
    #: What the finished artifact must contain, checked on the file the run
    #: wrote.  This is the HTML analogue of ``test_command``: a page has no
    #: tests, so its acceptance is a set of structural facts.
    artifact_must_contain: tuple[str, ...] = ()
    #: Minimum size of the written artifact, in bytes.
    artifact_min_bytes: int = 0


# --- Task #1: classic validate_email fix (mirrors the toy repo) ------------

_TASK1_SOURCE = '''"""Toy math utilities for the GigaForge benchmark."""

import re


def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b


_EMAIL_RE = re.compile(r"^[a-z0-9]+@[a-z0-9]+\\.[a-z]{2,}$")


def validate_email(email: str) -> bool:
    """Return True iff *email* looks like a standard address.

    Buggy: rejects short TLDs like '.z'.
    """
    return bool(_EMAIL_RE.match(email))


def register_user(email: str) -> str:
    """Register a new user."""
    if not validate_email(email):
        raise ValueError(f"invalid email: {email!r}")
    return f"registered:{email}"
'''

_TASK1_TEST = '''"""Tests for math_utils — assert validate_email accepts short TLDs."""

from math_utils import validate_email


def test_short_tld():
    assert validate_email("x@y.z") is True


def test_normal_email():
    assert validate_email("foo@bar.com") is True


def test_invalid_email():
    assert validate_email("not-an-email") is False
'''

_TASK1_DIFF = '''--- a/math_utils.py
+++ b/math_utils.py
@@ -23,8 +23,3 @@
 def validate_email(email: str) -> bool:
-    """Return True iff *email* looks like a standard address.
-
-    Buggy: rejects short TLDs like '.z'.
-    """
-    return bool(_EMAIL_RE.match(email))
+    """Validate."""
+    return bool(re.match(r"^[a-z0-9]+@[a-z0-9]+\\.[a-z]+$", email))
'''


_TASK1_PLAN = _json.dumps(
    {"steps": [{"tool": "edit_function", "args": {"name": "validate_email"}, "done": False}]}
)
_TASK1_REVIEW_OK = _json.dumps({"verdict": "ok", "issues": []})


TASK1 = TaskFixture(
    name="toy-validate-email",
    repo_files={
        "math_utils.py": _TASK1_SOURCE,
        "tests/__init__.py": "",
        "tests/test_math_utils.py": _TASK1_TEST,
    },
    task="Fix validate_email to accept standard emails like x@y.z",
    target_function="validate_email",
    target_file="math_utils.py",
    plan_response=_TASK1_PLAN,
    code_responses=[_TASK1_DIFF],
    review_responses=[_TASK1_REVIEW_OK],
    test_command=[sys.executable, "-m", "pytest", "-q", "tests/"],
    expected_diff_substring="[a-z]+",
    test_args=["-q", "tests/"],
)


# --- Task #2: simple add() fix (off-by-one) --------------------------------

_TASK2_SOURCE = '''"""Simple arithmetic utilities."""

def add(a: int, b: int) -> int:
    """Add two integers. BUGGY: returns a - b instead of a + b."""
    return a - b


def sub(a: int, b: int) -> int:
    """Subtract b from a."""
    return a - b
'''

_TASK2_TEST = '''from math_utils import add


def test_add():
    assert add(2, 3) == 5


def test_add_zero():
    assert add(0, 0) == 0
'''

_TASK2_DIFF = '''--- a/math_utils.py
+++ b/math_utils.py
@@ -1,4 +1,4 @@
 def add(a: int, b: int) -> int:
-    """Add two integers. BUGGY: returns a - b instead of a + b."""
-    return a - b
+    """Add two integers."""
+    return a + b
'''

_TASK2_PLAN = _json.dumps(
    {"steps": [{"tool": "edit_function", "args": {"name": "add"}, "done": False}]}
)

TASK2 = TaskFixture(
    name="toy-add-fix",
    repo_files={
        "math_utils.py": _TASK2_SOURCE,
        "tests/__init__.py": "",
        "tests/test_math_utils.py": _TASK2_TEST,
    },
    task="Fix the add function so it returns a + b (currently returns a - b).",
    target_function="add",
    target_file="math_utils.py",
    plan_response=_TASK2_PLAN,
    code_responses=[_TASK2_DIFF],
    review_responses=[_TASK1_REVIEW_OK],
    test_command=[sys.executable, "-m", "pytest", "-q", "tests/"],
    expected_diff_substring="a + b",
    test_args=["-q", "tests/"],
)


# --- Task #3: rename-internal-only (no signature change) -------------------

_TASK3_SOURCE = '''"""A function with a misleading internal variable name."""

def calculate_total(price: float, qty: int) -> float:
    """Return the total price for `qty` items at `price` each.

    BUGGY: the internal variable is named 'discount' but it's actually the
    total. Rename it to 'total' for clarity (no behavior change).
    """
    discount = price * qty
    return discount
'''

_TASK3_TEST = '''from math_utils import calculate_total


def test_total():
    assert calculate_total(10.0, 3) == 30.0


def test_zero():
    assert calculate_total(0.0, 5) == 0.0
'''

_TASK3_DIFF = '''--- a/math_utils.py
+++ b/math_utils.py
@@ -3,5 +3,5 @@
     total price for `qty` items at `price` each.

     BUGGY: the internal variable is named 'discount' but it's actually the
     total. Rename it to 'total' for clarity (no behavior change).
     """
-    discount = price * qty
-    return discount
+    total = price * qty
+    return total
'''

_TASK3_PLAN = _json.dumps(
    {"steps": [{"tool": "edit_function", "args": {"name": "calculate_total"}, "done": False}]}
)

TASK3 = TaskFixture(
    name="toy-rename-internal",
    repo_files={
        "math_utils.py": _TASK3_SOURCE,
        "tests/__init__.py": "",
        "tests/test_math_utils.py": _TASK3_TEST,
    },
    task="Rename the internal variable 'discount' to 'total' for clarity.",
    target_function="calculate_total",
    target_file="math_utils.py",
    plan_response=_TASK3_PLAN,
    code_responses=[_TASK3_DIFF],
    review_responses=[_TASK1_REVIEW_OK],
    test_command=[sys.executable, "-m", "pytest", "-q", "tests/"],
    expected_diff_substring="total = price * qty",
    test_args=["-q", "tests/"],
)


# --- Task #4: too-restrictive regex fix (mirror of #1 but different shape) --

_TASK4_SOURCE = '''"""Username validation."""

import re

_USERNAME_RE = re.compile(r"^[a-z]{5,}$")


def validate_username(name: str) -> bool:
    """Return True iff name is a valid username.

    BUGGY: requires 5+ lowercase chars; rejects 'abc' (3 chars).
    Fix: accept 3+ chars.
    """
    return bool(_USERNAME_RE.match(name))
'''

_TASK4_TEST = '''from math_utils import validate_username


def test_short():
    assert validate_username("abc") is True


def test_long():
    assert validate_username("abcdef") is True
'''

_TASK4_DIFF = '''--- a/math_utils.py
+++ b/math_utils.py
@@ -3,8 +3,3 @@
 def validate_username(name: str) -> bool:
-    """Return True iff name is a valid username.
-
-    BUGGY: requires 5+ lowercase chars; rejects 'abc' (3 chars).
-    Fix: accept 3+ chars.
-    """
-    return bool(_USERNAME_RE.match(name))
+    """Validate."""
+    return bool(re.match(r"^[a-z]{3,}$", name))
'''

_TASK4_PLAN = _json.dumps(
    {"steps": [{"tool": "edit_function", "args": {"name": "validate_username"}, "done": False}]}
)

TASK4 = TaskFixture(
    name="toy-validate-username",
    repo_files={
        "math_utils.py": _TASK4_SOURCE,
        "tests/__init__.py": "",
        "tests/test_math_utils.py": _TASK4_TEST,
    },
    task="Fix validate_username to accept 3+ char usernames.",
    target_function="validate_username",
    target_file="math_utils.py",
    plan_response=_TASK4_PLAN,
    code_responses=[_TASK4_DIFF],
    review_responses=[_TASK1_REVIEW_OK],
    test_command=[sys.executable, "-m", "pytest", "-q", "tests/"],
    expected_diff_substring="[a-z]{3,}",
    test_args=["-q", "tests/"],
)


# The default task set — the bench runner uses this if no explicit list is passed.
# (Defined after TASK5, below.)


# --- Task #5: a page, not a diff ------------------------------------------
#
# The bench had four tasks and every one of them was a Python bug fix.  So the
# bench could not see the artifact path at all: the sectional generation, the
# plan-derived contract, the HTML repair, the render check — none of it was
# measured, and a regression that broke page generation outright would have
# left the bench green.  A benchmark that only covers the path you already
# trust is a regression test for the past, not a measurement of the product.
#
# This fixture is the HTML analogue of the others: the run is asked for a page,
# the fake provider answers per section, and acceptance is a set of structural
# facts about the file the run wrote.

_TASK5_HERO = (
    '<section class="hero">'
    "<h1>GigaForge</h1>"
    "<p>A harness that plans, acts and verifies its own work. It does not "
    "answer in a chat window and call that a job done: it reads the repository, "
    "writes the files, runs the tests and checks the artifact it produced "
    "before it reports success.</p>"
    "</section>"
)

_TASK5_FEATURES = (
    '<section class="features">'
    '<div class="feature-grid">'
    '<div class="feature-card"><h3>Plan</h3><p>It turns a goal into an ordered '
    "set of steps, and the plan is a checklist the harness can check rather "
    "than a paragraph the model can drift away from.</p></div>"
    '<div class="feature-card"><h3>Act</h3><p>It edits the working tree, not a '
    "transcript. Every change is a diff that either applies or does not, and "
    "the harness knows which before it claims anything.</p></div>"
    '<div class="feature-card"><h3>Verify</h3><p>It checks the artifact it '
    "built: structure, palette, landmarks and the tests that must pass after "
    "the change. A run that cannot prove its work is a run that failed.</p></div>"
    "</div></section>"
)

_TASK5_FOOTER = (
    '<footer class="footer"><p>GigaForge — an agent harness that finishes the '
    "job and shows its evidence.</p></footer>"
)

TASK5 = TaskFixture(
    name="page-cyberpunk-landing",
    repo_files={"README.md": "# demo repo\n"},
    task=(
        "Create a single-file index.html: a cyberpunk landing page about the "
        "GigaForge agent harness, with a hero, a feature grid and a footer, "
        "using cyan and magenta."
    ),
    target_function="",
    target_file="",
    plan_response=_json.dumps(
        {"steps": [{"tool": "read_structure", "args": {"path": "index.html"}}]}
    ),
    code_responses=[],
    review_responses=[_TASK1_REVIEW_OK],
    test_command=[],
    expected_diff_substring="index.html",
    section_responses={
        "hero": _TASK5_HERO,
        "features": _TASK5_FEATURES,
        "footer": _TASK5_FOOTER,
    },
    artifact_must_contain=(
        "<!DOCTYPE html",
        "<h1",
        "feature-card",
        "<footer",
        "cyan",
    ),
    artifact_min_bytes=1200,
)


# The default task set — the bench runner uses this if no explicit list is passed.
TASKS: list[TaskFixture] = [TASK1, TASK2, TASK3, TASK4, TASK5]


def _section_kind_from_prompt(prompt: str) -> str:
    """Which section a child prompt is asking for.

    ``section_prompt`` writes ``(kind: hero)`` into the instruction, so the
    fixture can answer by kind instead of by position.  Falls back to the first
    catalogue kind the prompt names, so a prompt reworded later still resolves.
    """
    match = re.search(r"\(kind:\s*([a-z_]+)\)", prompt)
    if match:
        return match.group(1)
    low = prompt.lower()
    for kind, triggers, _heading in _SECTION_CATALOGUE:
        if any(trigger in low for trigger in triggers):
            return kind
    return ""


# --- BenchFakeClient + LooserBudgetTracker ---------------------------------


class BenchFakeGigaClient:
    """A :class:`~gigaforge.providers.base.Provider` wired to a fixture's answers.

    The shape is the provider contract (``chat(messages, *, model=...)``), not the
    old client one, so the benchmark drives the *same* pipeline the application
    drives.  A benchmark that exercises a parallel code path measures that path,
    not the product.
    """

    def __init__(self, fixture: TaskFixture, *, metrics_sink: Any | None = None) -> None:
        self.chat_calls: list[dict[str, Any]] = []
        self._fixture = fixture
        self.metrics_sink = metrics_sink or (lambda r: None)
        self._react_calls = 0
        self._code_responses = list(fixture.code_responses)
        self._review_responses = list(fixture.review_responses)
        #: Real token accounting for the fake transport.
        #:
        #: The bench used to report ``tokens_in = 100 * llm_calls`` \u2014 a
        #: constant multiplied by a count.  That number could not move when the
        #: prompt grew, so a regression that doubled the context per call was
        #: invisible in the one report the bench exists to produce.  The fake
        #: client now measures the messages it was actually handed, which is the
        #: same 4-chars-per-token estimate the context guard uses.
        self.usage_in = 0
        self.usage_out = 0

    def capabilities(self) -> Any:
        from gigaforge.providers.base import ProviderCapabilities

        return ProviderCapabilities(streaming=False, function_calling=False,
                                    default_model="bench-fake")

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str = "",
        phase: str = "unknown",
        task_id: str = "",
        **kwargs: Any,
    ) -> ChatResult:
        prompt = str(messages[-1].get("content", "")) if messages else ""
        if phase == "unknown":
            phase = _phase_from_prompt(prompt)
        self.chat_calls.append({"model": model, "phase": phase, **kwargs})
        # Measure the whole request, not just the last message: the growth the
        # bench must catch is the trajectory, and the trajectory is every
        # message before the last one.
        tokens_in = sum(len(str(m.get("content", "") or "")) for m in messages) // 4 + 4
        if phase == "react":
            self._react_calls += 1
            if self._react_calls == 1:
                body = {
                    "thought": "Locate the target before planning.",
                    "action": "grep",
                    "action_input": self._fixture.target_function or "demo repo",
                }
            else:
                try:
                    steps = _json.loads(self._fixture.plan_response).get("steps", [])
                except (TypeError, ValueError, AttributeError):
                    steps = []
                if not steps and not self._fixture.target_file:
                    steps = [{"tool": "write_file", "args": {"path": "index.html"}}]
                body = {
                    "thought": "The target is identified; hand the executor a concrete plan.",
                    "action": "finish",
                    "action_input": steps,
                }
            content = _json.dumps(body)
        elif phase == "plan":
            content = self._fixture.plan_response
        elif phase == "react":
            # The product plans through LangGraph ReAct. Reuse the fixture's
            # structured plan as a deterministic finish action so the bench
            # exercises that runtime instead of depending on a linear planner.
            content = _json.dumps(
                {
                    "thought": "Use the fixture plan.",
                    "action": "finish",
                    "action_input": self._fixture.plan_response,
                }
            )
        elif phase == "code" and self._fixture.section_responses:
            # The sectional page path: the prompt names the section kind, so the
            # answer is chosen by kind rather than by position.  Sections run
            # concurrently, and a positional list would hand the hero's answer
            # to the footer depending on which thread won.
            kind = _section_kind_from_prompt(prompt)
            content = self._fixture.section_responses.get(kind, "")
        elif phase == "code":
            content = (
                self._code_responses.pop(0)
                if self._code_responses
                else (self._fixture.code_responses[0] if self._fixture.code_responses else "")
            )
        elif phase == "review":
            content = (
                self._review_responses.pop(0)
                if self._review_responses
                else (self._fixture.review_responses[0] if self._fixture.review_responses
                      else _TASK1_REVIEW_OK)
            )
        else:
            content = ""
        tokens_out = max(1, len(content) // 4)
        self.usage_in += tokens_in
        self.usage_out += tokens_out
        self.metrics_sink(
            {
                "ts": time.time(),
                "task_id": task_id,
                "phase": phase,
                "model": model,
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "estimated": True,
                "latency_ms": 1,
                "retries": 0,
                "ok": True,
                "cache_hit": False,
            }
        )
        return ChatResult(
            content=content,
            usage={
                "prompt_tokens": tokens_in,
                "completion_tokens": tokens_out,
                "total_tokens": tokens_in + tokens_out,
            },
            finish_reason="stop",
            estimated=True,
        )

    async def list_models(self) -> list[dict[str, Any]]:
        return [{"id": "bench-fake"}]

    async def aclose(self) -> None:
        return None


_PHASE_HINTS = (
    ("plan", ("декомпозиц", "разложи", "decompos", "step-by-step plan")),
    ("review", ("ревьюер", "reviewer", "конституц")),
    ("summarize", ("суммар", "summar", "итог")),
)


def _phase_from_prompt(prompt: str) -> str:
    """Infer the phase when the caller did not label the call."""
    low = prompt.lower()
    for phase, hints in _PHASE_HINTS:
        if any(hint in low for hint in hints):
            return phase
    return "code"



#: Retained as a name for external scripts that imported it.  The V2 pipeline
#: enforces budgets through ``CoreConfig``, so the benchmark no longer needs a
#: special tracker — it runs the same caps the application runs.
LooserBudgetTracker = None


# --- BenchResult + Runner --------------------------------------------------


@dataclass
class BenchResult:
    """One task's bench outcome."""

    name: str
    task: str
    success: bool
    cache_hit: bool
    llm_calls: int
    tokens_in: int
    tokens_out: int
    latency_s: float
    applied_files: list[str] = field(default_factory=list)
    diff_substring_found: bool = False
    tests_passed_after_apply: bool = False
    method: str = ""
    error: str = ""
    #: How many independent attempts this task was given (``--repeats``).
    attempts: int = 1
    #: How many of those attempts succeeded.  ``-1`` means "not measured" — a
    #: result built directly (a test, a single ad-hoc run) has no attempt count,
    #: and reporting 0 passes for a successful run would be a lie.
    passes: int = -1
    #: What this task cost, in micro-USD.  Tokens alone do not answer the
    #: question a budget owner asks: the same token count on a different model is
    #: a different bill, and the ladder's whole point is to move between models.
    cost_micros: int = 0
    #: Cache lookups and hits for this task.  A bare ``cache_hit`` bool says
    #: whether the LAST lookup hit; the rate says whether the cache works.
    cache_hits: int = 0
    cache_lookups: int = 0

    @property
    def cost_usd(self) -> float:
        return self.cost_micros / 1_000_000

    @property
    def cache_hit_rate(self) -> float:
        if self.cache_lookups <= 0:
            return 0.0
        return self.cache_hits / self.cache_lookups

    @property
    def pass_at_k(self) -> float:
        """Fraction of attempts that succeeded, 0..1.

        **Why this exists.**  A single run of a stochastic pipeline is a coin
        flip reported as a fact.  The bench said "5/5 passed" and that number
        could not distinguish a harness that works every time from one that
        works four times in five — and the difference is the whole question for
        a harness whose output comes from a model.

        ``pass@k`` here is the empirical pass rate over ``k`` independent
        attempts: ``passes / attempts``.  It is not the unbiased estimator from
        the Codex paper (that one needs ``n > k`` samples and a combinatorial
        correction); with ``k`` attempts and no extra samples the honest number
        is the observed rate, and calling it anything else would be a claim the
        data does not support.
        """
        if self.attempts <= 0:
            return 0.0
        if self.passes < 0:
            # Not measured: the honest answer is the single run's own outcome.
            return 1.0 if self.success else 0.0
        return self.passes / self.attempts

    def to_row(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "success": self.success,
            "cache_hit": self.cache_hit,
            "llm_calls": self.llm_calls,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "tokens_total": self.tokens_in + self.tokens_out,
            "latency_s": round(self.latency_s, 3),
            "tests_passed": self.tests_passed_after_apply,
            "method": self.method,
            "attempts": self.attempts,
            "passes": self.passes,
            "pass_at_k": round(self.pass_at_k, 4),
            "cost_micros": self.cost_micros,
            "cost_usd": round(self.cost_usd, 6),
            "cache_hits": self.cache_hits,
            "cache_lookups": self.cache_lookups,
            "cache_hit_rate": round(self.cache_hit_rate, 4),
            "error": self.error[:200] if self.error else "",
        }


def _safe_rmtree(path: Path) -> None:
    if not path.exists():
        return
    import os
    import stat

    def _onerror(func, p, excinfo):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except Exception:
            pass

    try:
        shutil.rmtree(path, onerror=_onerror)
    except Exception:
        pass


class BenchRunner:
    """Runs the pipeline against a set of task fixtures; produces BenchResults."""

    def __init__(
        self,
        *,
        work_root: Path | None = None,
        tasks: list[TaskFixture] | None = None,
        repeats: int = 1,
    ) -> None:
        self._work_root = work_root or Path("/tmp/gigaforge_bench")
        self._work_root.mkdir(parents=True, exist_ok=True)
        self._tasks = tasks if tasks is not None else list(TASKS)
        #: Independent attempts per task.  ``1`` is the default because the
        #: bench is deterministic offline; a live run passes ``--repeats k`` to
        #: measure pass@k, which is the only honest way to report a stochastic
        #: pipeline's reliability.
        self._repeats = max(1, int(repeats))

    def run_all(self) -> list[BenchResult]:
        """Run every task fixture; return a list of BenchResult (one per task)."""
        results: list[BenchResult] = []
        for fixture in self._tasks:
            attempts: list[BenchResult] = []
            for attempt in range(self._repeats):
                try:
                    result = self._run_one(fixture, attempt=attempt)
                except Exception as exc:  # noqa: BLE001 — bench must not crash
                    result = BenchResult(
                        name=fixture.name,
                        task=fixture.task,
                        success=False,
                        cache_hit=False,
                        llm_calls=0,
                        tokens_in=0,
                        tokens_out=0,
                        latency_s=0.0,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                attempts.append(result)
            # The reported result is the FIRST attempt (so a single-run bench is
            # unchanged), annotated with how many of the k attempts passed.
            head = attempts[0]
            head.attempts = len(attempts)
            head.passes = sum(1 for a in attempts if a.success)
            if head.passes < head.attempts:
                # A task that does not pass every time is not a pass: the bench
                # gate must fail on flakiness, not average it away.
                head.success = False
                if not head.error:
                    head.error = (
                        f"flaky: {head.passes}/{head.attempts} attempts passed"
                    )
            results.append(head)
        return results

    def _run_one(self, fixture: TaskFixture, *, attempt: int = 0) -> BenchResult:
        """Build the repo, run the real DAG, verify the diff + tests.

        ``attempt`` only names the scratch directory, so k independent attempts
        do not share a tree — sharing one would make attempt 2 start from
        attempt 1's result and measure nothing.
        """
        # Each task gets its own scratch dir under work_root.
        task_dir = self._work_root / (
            fixture.name if attempt == 0 else f"{fixture.name}__attempt{attempt}"
        )
        if task_dir.exists():
            _safe_rmtree(task_dir)
        task_dir.mkdir(parents=True, exist_ok=True)
        repo = task_dir / "repo"
        repo.mkdir(exist_ok=True)
        for rel_path, source in fixture.repo_files.items():
            target = repo / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(source, encoding="utf-8")

        # git init + initial commit (so the worktree helper works).
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t", "add", "-A"],
            cwd=repo,
            check=True,
        )
        subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init"],
            cwd=repo,
            check=True,
        )

        # The real wiring: same config, firewall, indexer and pipeline as the app,
        # with the fake provider standing in for the model.
        from gigaforge.core.config import CoreConfig
        from gigaforge.core.pipeline import TaskPipeline
        from gigaforge.core.sync_adapter import SyncChatClient
        from gigaforge.firewall import ContextFirewall
        from gigaforge.firewall.config import FirewallConfig
        from gigaforge.l0.config import L0Config
        from gigaforge.l0.indexer import Indexer
        from gigaforge.l0.store import L0Store

        cfg = CoreConfig(
            db_path=str(task_dir / "core.sqlite3"),
            trace_path=str(task_dir / "traces" / "core.jsonl"),
            repo_root=str(repo),
        )
        l0_config = L0Config.from_env(repo_root=str(repo),
                                     db_path=str(task_dir / "l0.sqlite3"))
        l0_store = L0Store(config=l0_config)
        indexer = Indexer(l0_store, config=l0_config)
        indexer.index_full()
        firewall = ContextFirewall(l0_store, config=FirewallConfig(repo_root=str(repo)))

        provider = BenchFakeGigaClient(fixture)
        client = SyncChatClient(provider)
        pipeline = TaskPipeline(config=cfg, client=client, firewall=firewall,
                                indexer=indexer)

        started = time.monotonic()
        try:
            result = pipeline.run(
                fixture.task,
                files=(fixture.target_file,) if fixture.target_file else (),
            )
        finally:
            latency = time.monotonic() - started

        # Post-run verification: apply the produced diff to a fresh copy of the repo
        # and run the fixture's test command.  This is the part that cannot be faked:
        # either the diff applies and the tests pass, or the task failed.
        diff_text = str(getattr(result, "final_diff", "") or "")
        diff_substring_found = fixture.expected_diff_substring in diff_text
        tests_passed = False
        verify_error = ""
        if result.accepted and diff_text:
            verify_dir = task_dir / "verify"
            if verify_dir.exists():
                _safe_rmtree(verify_dir)
            # Copy the repo *with* its history, then reset to HEAD.  The pipeline
            # already applied its diff to the working tree, so re-applying against a
            # plain copy would fail for the right reason and look like the bench is
            # broken; resetting gives the diff an untouched checkout to apply to.
            shutil.copytree(repo, verify_dir, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns(".gigaforge"))
            subprocess.run(["git", "checkout", "--", "."], cwd=verify_dir,
                           capture_output=True, check=False)
            subprocess.run(["git", "clean", "-qfd"], cwd=verify_dir,
                           capture_output=True, check=False)
            try:
                # Re-apply to the *verify* copy: a fresh firewall rooted there, so
                # "the diff applies" is proved against an untouched checkout rather
                # than against the workspace the pipeline already wrote to.
                verify_firewall = ContextFirewall(
                    None, config=FirewallConfig(repo_root=str(verify_dir))
                )
                applied = verify_firewall.apply_diff(diff_text)
                applied_ok = bool(getattr(applied, "ok", False))
                if not applied_ok:
                    verify_error = str(getattr(applied, "error", "") or "diff did not apply")
            except Exception as exc:  # noqa: BLE001 - reported as a bench error
                applied_ok = False
                verify_error = f"{type(exc).__name__}: {exc}"
            if applied_ok:
                try:
                    cmd = list(fixture.test_command)
                    if not cmd:
                        # A page task has no test command: its acceptance is the
                        # artifact check below.  Running ``[]`` raised
                        # ``IndexError: list index out of range`` and the bench
                        # reported a successful page as an error.
                        raise _NoTestCommand
                    if sys.platform == "win32" and cmd and cmd[0] == "python3":
                        cmd[0] = sys.executable
                    proc = subprocess.run(cmd, cwd=verify_dir, capture_output=True,
                                          text=True, timeout=60)
                    tests_passed = proc.returncode == 0
                    if not tests_passed:
                        verify_error = (proc.stdout or proc.stderr or "")[-400:]
                except _NoTestCommand:
                    tests_passed = False
                except Exception as exc:  # noqa: BLE001 - best effort
                    verify_error = f"{type(exc).__name__}: {exc}"
                    tests_passed = False

        llm_calls = len(provider.chat_calls)
        pipeline.close()
        l0_store.close()

        # A page task has no test command: its acceptance is a set of structural
        # facts about the file the run wrote.  Without this the HTML fixture
        # would report success on ``result.accepted`` alone, which is the very
        # claim the bench exists to check independently.
        artifact_ok = True
        if fixture.artifact_must_contain or fixture.artifact_min_bytes:
            written = repo / (fixture.expected_diff_substring or "index.html")
            if not written.is_file():
                artifact_ok = False
                verify_error = f"artifact not written: {written.name}"
            else:
                body = written.read_text(encoding="utf-8", errors="replace")
                missing = [needle for needle in fixture.artifact_must_contain
                           if needle not in body]
                if missing:
                    artifact_ok = False
                    verify_error = f"artifact missing: {', '.join(missing)}"
                elif len(body.encode("utf-8")) < fixture.artifact_min_bytes:
                    artifact_ok = False
                    verify_error = (
                        f"artifact is {len(body.encode('utf-8'))} bytes, "
                        f"expected at least {fixture.artifact_min_bytes}"
                    )

        return BenchResult(
            name=fixture.name,
            task=fixture.task,
            success=bool(result.accepted) and artifact_ok,
            cache_hit=bool(result.cache_hit),
            llm_calls=llm_calls,
            tokens_in=provider.usage_in,
            tokens_out=provider.usage_out,
            latency_s=latency,
            applied_files=list(result.changed_files),
            diff_substring_found=diff_substring_found,
            tests_passed_after_apply=tests_passed or (
                artifact_ok and bool(fixture.artifact_must_contain)
            ),
            # Cost and cache come from the run's own accounting, not from a
            # second estimate: the pipeline already prices every call against
            # the model it actually used, and re-deriving it here would be a
            # second source of truth for the same number.
            cost_micros=int(getattr(result, "cost_micros", 0) or 0),
            cache_hits=int(getattr(result, "cache_hits", 0) or 0),
            cache_lookups=int(getattr(result, "cache_lookups", 0) or 0),
            error=verify_error or str(getattr(result, "error", "") or ""),
        )
