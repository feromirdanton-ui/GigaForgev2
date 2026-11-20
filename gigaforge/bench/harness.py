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
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from gigaforge.bench.report import bench_report  # re-export
from gigaforge.client.giga_client import ChatResult

__all__ = ["BenchRunner", "BenchResult", "bench_report", "TASKS"]


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
TASKS: list[TaskFixture] = [TASK1, TASK2, TASK3, TASK4]


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
        if phase == "plan":
            content = self._fixture.plan_response
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
    ) -> None:
        self._work_root = work_root or Path("/tmp/gigaforge_bench")
        self._work_root.mkdir(parents=True, exist_ok=True)
        self._tasks = tasks if tasks is not None else list(TASKS)

    def run_all(self) -> list[BenchResult]:
        """Run every task fixture; return a list of BenchResult (one per task)."""
        results: list[BenchResult] = []
        for fixture in self._tasks:
            try:
                result = self._run_one(fixture)
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
            results.append(result)
        return results

    def _run_one(self, fixture: TaskFixture) -> BenchResult:
        """Build the repo, run the real DAG, verify the diff + tests."""
        # Each task gets its own scratch dir under work_root.
        task_dir = self._work_root / fixture.name
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
                    if sys.platform == "win32" and cmd and cmd[0] == "python3":
                        cmd[0] = sys.executable
                    proc = subprocess.run(cmd, cwd=verify_dir, capture_output=True,
                                          text=True, timeout=60)
                    tests_passed = proc.returncode == 0
                    if not tests_passed:
                        verify_error = (proc.stdout or proc.stderr or "")[-400:]
                except Exception as exc:  # noqa: BLE001 - best effort
                    verify_error = f"{type(exc).__name__}: {exc}"
                    tests_passed = False

        llm_calls = len(provider.chat_calls)
        pipeline.close()
        l0_store.close()

        return BenchResult(
            name=fixture.name,
            task=fixture.task,
            success=bool(result.accepted),
            cache_hit=bool(result.cache_hit),
            llm_calls=llm_calls,
            tokens_in=provider.usage_in,
            tokens_out=provider.usage_out,
            latency_s=latency,
            applied_files=list(result.changed_files),
            diff_substring_found=diff_substring_found,
            tests_passed_after_apply=tests_passed,
            error=verify_error or str(getattr(result, "error", "") or ""),
        )
