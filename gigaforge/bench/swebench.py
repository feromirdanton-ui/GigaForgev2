"""SWE-bench Verified as an external yardstick for GigaForge.

**Why this exists.**  The internal bench is seven fixtures written by the same
person who wrote the harness, so it measures the harness against its author's
imagination.  SWE-bench Verified is 500 real GitHub issues with real
maintainer-written tests: a task is solved only if the FAIL_TO_PASS tests go
green and the PASS_TO_PASS tests stay green.  That is the one number that can
be compared with other harnesses.

**What is deliberately NOT here.**  The official SWE-bench harness runs each
instance in a Docker image built per repository, and grades with a container
that has the exact dependency set of 2022.  That is the right thing for a
leaderboard and the wrong thing for this sandbox: no Docker, 4 GB of disk, and
a 90-minute run.  So this adapter keeps the *mechanism* — real repo, real
issue, real tests, real grading — and drops the container:

* the repo is cloned at ``base_commit`` (shallow, one branch);
* the model is given the problem statement and the repo, and must produce a
  diff;
* the diff is applied, the ``test_patch`` is applied, and the named tests are
  run with the repo's own pytest;
* an instance passes only if every FAIL_TO_PASS test passes and no
  PASS_TO_PASS test regresses.

The honest limitation, stated in the report rather than hidden: a repository
whose 2022 dependencies cannot be installed in this sandbox is reported as
``environment_error``, not as a model failure.  Those instances are excluded
from the pass rate and counted separately, because silently scoring them as
failures would understate the harness and silently scoring them as passes
would overstate it.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

__all__ = [
    "SweInstance",
    "SweResult",
    "load_instances",
    "run_instance",
    "run_swebench",
    "summarise",
]

#: Where the parquet copy of the dataset lives when it has been fetched.
DEFAULT_DATASET = Path("/tmp/swe.parquet")
DATASET_URL = (
    "https://huggingface.co/api/datasets/princeton-nlp/SWE-bench_Verified"
    "/parquet/default/test/0.parquet"
)


@dataclass
class SweInstance:
    instance_id: str
    repo: str
    base_commit: str
    problem_statement: str
    patch: str
    test_patch: str
    fail_to_pass: list[str] = field(default_factory=list)
    pass_to_pass: list[str] = field(default_factory=list)
    difficulty: str = ""


@dataclass
class SweResult:
    instance_id: str
    repo: str
    status: str  # passed | failed | environment_error | no_diff | timeout
    detail: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    latency_s: float = 0.0
    diff_chars: int = 0
    fail_to_pass_passed: int = 0
    fail_to_pass_total: int = 0
    pass_to_pass_regressed: int = 0
    #: The pytest the tests actually ran under.  "Gradable" is a claim about an
    #: environment, and without this number it is unprovable: the venv fix was
    #: reported as done while ``--system-site-packages`` still let a system
    #: pytest satisfy the repo's declared requirement.
    pytest_version: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "instance_id": self.instance_id,
            "repo": self.repo,
            "status": self.status,
            "detail": self.detail[:400],
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "latency_s": round(self.latency_s, 1),
            "diff_chars": self.diff_chars,
            "fail_to_pass": f"{self.fail_to_pass_passed}/{self.fail_to_pass_total}",
            "pass_to_pass_regressed": self.pass_to_pass_regressed,
            "pytest_version": self.pytest_version,
        }


def _as_list(value: Any) -> list[str]:
    """``FAIL_TO_PASS`` arrives as a JSON string in the parquet copy."""
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return [str(v) for v in parsed]
        except json.JSONDecodeError:
            return [value]
    return []


def load_instances(
    path: Path | str = DEFAULT_DATASET,
    *,
    limit: int | None = None,
    repos: Sequence[str] | None = None,
    instance_ids: Sequence[str] | None = None,
) -> list[SweInstance]:
    """Read the Verified split into typed instances.

    ``repos`` and ``instance_ids`` filter before ``limit`` is applied, so
    ``limit=3, repos=["pytest-dev/pytest"]`` means three pytest instances
    rather than "three instances, if any happen to be pytest".
    """
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(
            f"{path} is missing; fetch it with: curl -sL -o {path} {DATASET_URL}"
        )
    import pyarrow.parquet as pq  # local import: only this module needs it

    rows = pq.read_table(str(path)).to_pylist()
    out: list[SweInstance] = []
    for row in rows:
        repo = str(row.get("repo") or "")
        iid = str(row.get("instance_id") or "")
        if repos and repo not in repos:
            continue
        if instance_ids and iid not in instance_ids:
            continue
        out.append(
            SweInstance(
                instance_id=iid,
                repo=repo,
                base_commit=str(row.get("base_commit") or ""),
                problem_statement=str(row.get("problem_statement") or ""),
                patch=str(row.get("patch") or ""),
                test_patch=str(row.get("test_patch") or ""),
                fail_to_pass=_as_list(row.get("FAIL_TO_PASS")),
                pass_to_pass=_as_list(row.get("PASS_TO_PASS")),
                difficulty=str(row.get("difficulty") or ""),
            )
        )
        if limit is not None and len(out) >= limit:
            break
    return out


class _Deadline:
    """One wall-clock budget for a whole instance, enforced at every step.

    **Why this exists.**  ``run_instance(timeout_s=900)`` accepted the argument
    and never read it: the real limits were hard-coded per step (clone 600,
    venv 300, install 900 -- called twice on the env-error path --, tests
    600 x 2).  Worst case was ~3900s against a stated budget of 900s, so a run
    could not "not fit in the budget" -- the budget did not exist.  That is why
    the symbol-index fix was never re-verified: the run was killed by the
    caller, not by its own limit, and the report could only say "did not
    finish".

    A deadline makes the failure a fact the code can name: when the budget is
    gone, the step returns 124 and the instance is reported as ``timeout`` --
    a distinct status, because "the environment is broken" and "we ran out of
    time" are different claims and only one of them is about the model.
    """

    def __init__(self, budget_s: int) -> None:
        self.budget_s = int(budget_s)
        self.started = time.monotonic()
        self.end = self.started + max(1, int(budget_s))

    def remaining(self) -> float:
        return max(0.0, self.end - time.monotonic())

    @property
    def expired(self) -> bool:
        return self.remaining() <= 0.0

    def timeout(self, default: int) -> int:
        """``default``, capped by what is left of the budget.

        Never returns 0: a zero timeout would make every subprocess fail
        instantly and read as a broken command rather than an exhausted budget.
        """
        return max(1, int(min(default, self.remaining())))

    def elapsed(self) -> float:
        return time.monotonic() - self.started


def _run(
    cmd: Sequence[str],
    cwd: Path,
    timeout: int = 300,
    *,
    deadline: "_Deadline | None" = None,
) -> tuple[int, str]:
    if deadline is not None:
        if deadline.expired:
            return 124, f"deadline exhausted before: {cmd[0]}"
        timeout = deadline.timeout(timeout)
    try:
        proc = subprocess.run(
            list(cmd), cwd=cwd, capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s"
    except FileNotFoundError:
        return 127, f"command not found: {cmd[0]}"
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def _clone(
    instance: SweInstance, dest: Path, *, depth: int = 1,
    deadline: "_Deadline | None" = None,
) -> tuple[bool, str]:
    """Clone the repo at ``base_commit``.

    A shallow clone of the default branch usually does not contain the commit,
    so the fallback is a full clone followed by a checkout — slower, but the
    alternative is reporting a model failure for a fetch problem.
    """
    url = f"https://github.com/{instance.repo}.git"
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    code, out = _run(
        ["git", "clone", "--quiet", "--filter=blob:none", "--no-checkout", url, str(dest)],
        cwd=dest.parent,
        timeout=600,
    )
    if code != 0:
        return False, f"clone failed: {out[-300:]}"
    code, out = _run(
        ["git", "checkout", "--quiet", instance.base_commit],
        cwd=dest, timeout=300, deadline=deadline,
    )
    if code != 0:
        return False, f"checkout {instance.base_commit[:8]} failed: {out[-300:]}"
    return True, ""


def _apply(diff: str, cwd: Path) -> tuple[bool, str]:
    if not diff.strip():
        return False, "empty diff"
    proc = subprocess.run(
        ["git", "apply", "--whitespace=nowarn", "-"],
        cwd=cwd,
        input=diff,
        capture_output=True,
        text=True,
    )
    if proc.returncode == 0:
        return True, ""
    # A model diff often carries the wrong hunk offsets; ``-C1`` is the same
    # tolerance the internal bench's repair path buys, and it is still a real
    # apply (the content must match).
    proc = subprocess.run(
        ["git", "apply", "--whitespace=nowarn", "-C1", "-"],
        cwd=cwd,
        input=diff,
        capture_output=True,
        text=True,
    )
    if proc.returncode == 0:
        return True, "applied with -C1"
    return False, (proc.stderr or proc.stdout or "diff did not apply")[-300:]


def _worktree_diff(cwd: Path) -> str:
    """The change that is actually on disk, as a patch.

    SWE-bench grades a *repository*, not a model's claim about one.  Reading
    ``run.final_diff`` graded the claim: measured live on
    ``astropy__astropy-12907`` the run reported a 1 851-char diff whose hunks
    could not anchor (the model had guessed line numbers), while the worktree
    held a different, valid 2-line edit the episode's ``write_file`` had made.
    The bench then said "corrupt patch" about a patch nobody had applied.

    ``git diff`` plus the untracked files is the only description of the tree
    that cannot disagree with it.
    """
    parts: list[str] = []
    code, out = _run(["git", "diff", "--no-color", "--binary"], cwd=cwd, timeout=120)
    if code == 0 and out.strip():
        parts.append(out)
    code, out = _run(["git", "ls-files", "--others", "--exclude-standard"], cwd=cwd, timeout=60)
    if code == 0:
        for rel in [line.strip() for line in out.splitlines() if line.strip()]:
            path = cwd / rel
            if not path.is_file():
                continue
            try:
                body = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            lines = body.splitlines()
            parts.append(
                f"diff --git a/{rel} b/{rel}\n"
                f"new file mode 100644\n"
                f"--- /dev/null\n"
                f"+++ b/{rel}\n"
                f"@@ -0,0 +1,{len(lines)} @@\n"
                + "".join(f"+{line}\n" for line in lines)
            )
    return "".join(parts)


def _pytest_node_ids(tests: Iterable[str]) -> list[str]:
    """SWE-bench names tests as ``path::name``; pytest wants the same string."""
    return [t for t in tests if t.strip()]


def _run_tests(
    nodes: Sequence[str], cwd: Path, timeout: int = 600, python: str = "",
    *, deadline: "_Deadline | None" = None,
) -> tuple[bool, str]:
    """Run the named tests with the *instance's* interpreter.

    ``python`` is the per-instance venv (see :func:`_make_venv`).  Running the
    tests with the sandbox's own interpreter was the second half of the same
    isolation defect: even with a clean install, ``python -m pytest`` resolved
    to whatever pytest the sandbox had, not the one the repo declares.
    """
    py = python or sys.executable
    if not nodes:
        return True, "no tests named"
    # Django names its tests as dotted paths (``auth_tests.test_validators.
    # UsernameValidatorsTests.test_ascii_validator``) and runs them through its
    # own ``tests/runtests.py``; pytest answers "file or directory not found"
    # for every one of them.  Measured on django__django-10097 and -10554: both
    # were reported as environment errors while the model's diff was fine.
    runtests = cwd / "tests" / "runtests.py"
    if runtests.is_file() and all("::" not in n for n in nodes):
        cmd = [py, str(runtests), "--verbosity", "1", *nodes]
        code, out = _run(cmd, cwd=cwd, timeout=timeout, deadline=deadline)
        return code == 0, out[-1500:]
    cmd = [py, "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider", *nodes]
    code, out = _run(cmd, cwd, timeout=timeout, deadline=deadline)
    return code == 0, out[-1500:]


#: A test run that never reached the tests is not a model failure.  Measured on
#: ``astropy__astropy-12907``: the model's diff was applied, the maintainer's
#: test patch was applied, and pytest died in ``conftest.py`` on
#: ``ModuleNotFoundError: No module named 'hypothesis'``.  The bench reported
#: ``failed`` — a model failure — for a missing dependency, which is exactly the
#: number SWE-bench's own harness excludes as an environment error.
_ENV_FAILURE_MARKERS = (
    "ModuleNotFoundError",
    "ImportError while loading conftest",
    "No module named",
    "error: command 'gcc' failed",
    "ERROR: file or directory not found",
    "no tests ran",
    "INTERNALERROR",
)


def _looks_like_environment_failure(output: str) -> bool:
    """True when pytest never got as far as running the named tests."""
    text = output or ""
    if "collected 0 items" in text and "error" in text.lower():
        return True
    return any(marker in text for marker in _ENV_FAILURE_MARKERS)


def _venv_command(venv: Path) -> list[str]:
    """The command that builds an instance's private interpreter.

    A separate function so the *absence* of ``--system-site-packages`` is a
    testable fact rather than a claim about a docstring.  The flag was there to
    keep the sandbox's prebuilt wheels visible, and it quietly undid the
    isolation: pip treats a system package as satisfying a declared
    requirement, so a repo asking for ``pytest>=6.0`` against a system
    ``pytest 8.x`` got "already satisfied" and ran on a version it never
    declared.  That is the same class of defect as the leak this venv exists to
    close, only silent -- a wrong environment instead of a crash.
    """
    return [sys.executable, "-m", "venv", str(venv)]


def _make_venv(dest: Path, *, deadline: "_Deadline | None" = None) -> tuple[str, str]:
    """A private interpreter for one instance, or ``("", reason)``.

    **Why this is not optional.**  Measured live: ``_install_test_requirements``
    ran ``pip install -e .`` with ``sys.executable`` — the *sandbox's* python.
    Installing ``pytest-dev__pytest-7571`` therefore replaced the system
    ``pytest`` with pytest 6.0-dev, and every later instance in the same run
    imported ``_pytest`` from that checkout.  The probe run showed it plainly:
    four ``psf/requests`` instances all failed with a traceback pointing into
    ``/tmp/gigaforge_swe/pytest-dev__pytest-7571/src/_pytest/...``.  One
    instance's dependency set had become every instance's dependency set, and
    the bench reported it as four environment errors.

    SWE-bench's own harness gets this for free: each instance is a container.
    Without containers, a venv per instance is the same isolation at a fraction
    of the cost.

    **No ``--system-site-packages``.**  It was there to keep the sandbox's
    prebuilt wheels visible, and it quietly undid the isolation: pip treats a
    system package as satisfying a declared requirement, so a repo asking for
    ``pytest>=6.0`` against a system ``pytest 8.x`` got "already satisfied" and
    ran on a version it never declared.  That is the same class of defect as
    the leak this venv exists to close, only silent -- a wrong environment
    instead of a crash.  The venv is now genuinely private; declared
    dependencies are installed with ``--ignore-installed`` so the version in
    ``install_requires`` always wins.
    """
    venv = dest.parent / f"{dest.name}.venv"
    if venv.exists():
        shutil.rmtree(venv, ignore_errors=True)
    code, out = _run(
        _venv_command(venv),
        cwd=dest.parent,
        timeout=300,
        deadline=deadline,
    )
    if code != 0:
        return "", f"venv failed: {out[-200:]}"
    python = str(venv / "bin" / "python")
    if not Path(python).is_file():
        return "", "venv has no bin/python"
    return python, ""


def _pytest_version(python: str, cwd: Path, *, deadline: "_Deadline | None" = None) -> str:
    """The pytest the tests will actually run under, or ``""``.

    Recorded on every result because "gradable" is a claim about an
    environment.  The venv fix was reported as complete while
    ``--system-site-packages`` still let a system pytest satisfy the repo's
    declared requirement -- the run was gradable, but not on the version the
    repo asked for, and nothing in the result said so.
    """
    code, out = _run(
        [python or sys.executable, "-m", "pytest", "--version"],
        cwd=cwd, timeout=60, deadline=deadline,
    )
    if code != 0 or not (out or "").strip():
        return ""
    return out.strip().splitlines()[0][:80]


def _install_test_requirements(
    cwd: Path, python: str = "", *, deadline: "_Deadline | None" = None
) -> str:
    """Best-effort install of the repo's own test dependencies.

    SWE-bench's reference harness builds a per-instance Docker image from the
    repo's spec.  There is no Docker here, so the closest honest equivalent is
    a per-instance venv (see :func:`_make_venv`) plus the repo's declared
    requirements — and a report of what could not be installed, because an
    instance that still cannot import its own conftest is an environment error,
    not a wrong answer.

    ``python`` is the instance's interpreter.  It defaults to the sandbox's own
    only for the direct-call path in tests; the real run always passes a venv.
    """
    py = python or sys.executable
    notes: list[str] = []
    for name in ("requirements-dev.txt", "requirements_test.txt", "test-requirements.txt",
                 "requirements/tests.txt", "dev-requirements.txt"):
        path = cwd / name
        if not path.is_file():
            continue
        code, out = _run(
            [py, "-m", "pip", "install", "--quiet", "--no-input", "-r", str(path)],
            cwd=cwd,
            timeout=600,
            deadline=deadline,
        )
        notes.append(f"{name}: {'ok' if code == 0 else out[-200:]}")
    # ``hypothesis`` is imported by astropy's conftest and is not in every
    # requirements file; it is a test-only dependency with no build cost.
    code, _ = _run([py, "-c", "import hypothesis"], cwd=cwd, timeout=60, deadline=deadline)
    if code != 0:
        code, out = _run(
            [py, "-m", "pip", "install", "--quiet", "--no-input", "hypothesis"],
            cwd=cwd,
            timeout=300,
            deadline=deadline,
        )
        notes.append(f"hypothesis: {'ok' if code == 0 else out[-200:]}")

    # The repo itself has to be importable: astropy's ``conftest.py`` imports
    # ``astropy.__version__``, which only exists after the package is installed
    # (setuptools_scm writes ``version.py`` at build time).  Without this the
    # tests die on "could not determine astropy package version" — an
    # environment error that reads like a broken model change.
    #
    # **Dependencies are installed, not skipped.**  The earlier ``--no-deps``
    # was added so astropy 5.1's setup.py (which imports the removed
    # ``setuptools.dep_util``) would not abort the install.  It also meant the
    # repo's own runtime dependencies were never installed: measured on
    # ``pytest-dev__pytest-7571``, ``pip install --no-deps -e .`` succeeded and
    # the tests then died on ``ModuleNotFoundError: No module named 'py'`` —
    # a dependency the repo declares in ``install_requires``.  Inside a venv
    # the real install is safe, so it is tried first and ``--no-deps`` is only
    # the fallback for a repo whose build is genuinely broken.
    code, out = _run(
        [py, "-m", "pip", "install", "--quiet", "--no-input",
         "--ignore-installed", "--no-build-isolation", "-e", "."],
        cwd=cwd,
        timeout=900,
        deadline=deadline,
    )
    notes.append(f"editable install: {'ok' if code == 0 else 'failed'}")
    if code != 0:
        code, out = _run(
            [py, "-m", "pip", "install", "--quiet", "--no-input",
             "--no-deps", "--no-build-isolation", "-e", "."],
            cwd=cwd,
            timeout=900,
            deadline=deadline,
        )
        notes.append(f"editable install (no-deps): {'ok' if code == 0 else 'failed'}")
    if code != 0:
        code, out = _run(
            [py, "-c",
             "import pathlib, setuptools_scm;"
             "v = setuptools_scm.get_version(root='.');"
             "pathlib.Path('astropy/version.py').write_text("
             "f'version = {v!r}\\n__version__ = {v!r}\\n')"],
            cwd=cwd,
            timeout=120,
            deadline=deadline,
        )
        notes.append(f"version.py: {'ok' if code == 0 else out[-200:]}")
    return "; ".join(notes)


def run_instance(
    instance: SweInstance,
    *,
    work_root: Path,
    timeout_s: int = 900,
    keep: bool = False,
) -> SweResult:
    """Solve one instance end to end and grade it the way SWE-bench does."""
    started = time.monotonic()
    work_root = Path(work_root)
    # ``run_instance`` is public and is called directly by tests and by the
    # debug path; ``run_swebench`` creates the root, but a direct call did not,
    # and ``git clone`` then failed with "command not found: git" because the
    # *cwd* did not exist — an error that reads like a missing binary.
    work_root.mkdir(parents=True, exist_ok=True)
    dest = work_root / instance.instance_id.replace("/", "__")
    result = SweResult(instance_id=instance.instance_id, repo=instance.repo, status="failed")

    # One budget for the whole instance, enforced at every subprocess.  Before
    # this, ``timeout_s`` was accepted and never read: the real limits were
    # hard-coded per step and summed to ~3900s against a stated 900s.
    deadline = _Deadline(timeout_s)

    def _cleanup() -> None:
        """Remove the checkout and its venv.

        Called from the ``finally`` *and* from every early return.  The early
        returns used to skip it: a failed clone left a partial checkout, a
        failed venv left the checkout behind, and on a 4 GB disk a handful of
        those filled the volume -- which then made later instances fail as
        environment errors, blaming the model for a full disk.
        """
        if keep:
            return
        shutil.rmtree(dest, ignore_errors=True)
        shutil.rmtree(dest.parent / f"{dest.name}.venv", ignore_errors=True)

    ok, err = _clone(instance, dest, deadline=deadline)
    if not ok:
        result.status = "timeout" if deadline.expired else "environment_error"
        result.detail = err
        result.latency_s = time.monotonic() - started
        _cleanup()
        return result

    # The instance's own interpreter, built before anything is installed.
    # Without it every ``pip install`` in this function wrote into the sandbox's
    # site-packages and the *next* instance inherited it (measured: pytest
    # 6.0-dev from one checkout broke four unrelated ``psf/requests`` runs).
    python, venv_err = _make_venv(dest, deadline=deadline)
    if not python:
        result.status = "timeout" if deadline.expired else "environment_error"
        result.detail = venv_err
        result.latency_s = time.monotonic() - started
        _cleanup()
        return result
    install_note = _install_test_requirements(dest, python, deadline=deadline)
    result.pytest_version = _pytest_version(python, dest, deadline=deadline)

    try:
        # The harness solves the issue; it never sees the gold patch.
        from gigaforge.core.config import CoreConfig
        from gigaforge.core.pipeline import TaskPipeline
        from gigaforge.core.sync_adapter import SyncChatClient
        from gigaforge.client.client import GigaClient
        from gigaforge.client.config import ClientConfig
        from gigaforge.firewall import ContextFirewall
        from gigaforge.firewall.config import FirewallConfig
        from gigaforge.l0.config import L0Config
        from gigaforge.l0.indexer import Indexer
        from gigaforge.l0.store import L0Store

        cfg = CoreConfig(
            db_path=str(dest.parent / f"{dest.name}.db"),
            trace_path=str(dest.parent / f"{dest.name}.jsonl"),
            repo_root=str(dest),
        )
        l0 = L0Config.from_env(repo_root=str(dest), db_path=str(dest.parent / f"{dest.name}.l0.db"))
        store = L0Store(config=l0)
        indexer = Indexer(store, config=l0)
        indexer.index_full()
        firewall = ContextFirewall(store, config=FirewallConfig(repo_root=str(dest)))
        client = SyncChatClient(GigaClient(ClientConfig.from_env()))
        pipeline = TaskPipeline(config=cfg, client=client, firewall=firewall, indexer=indexer)
        try:
            run = pipeline.run(instance.problem_statement)
        finally:
            pipeline.close()
            store.close()

        diff = _worktree_diff(dest)
        result.diff_chars = len(diff)
        result.tokens_in = int(getattr(client, "usage_in", 0) or 0)
        result.tokens_out = int(getattr(client, "usage_out", 0) or 0)
        if not diff.strip():
            result.status = "no_diff"
            result.detail = str(getattr(run, "error", "") or "the run produced no diff")[:300]
            return result

        # The tree already holds the change, so there is nothing to apply: the
        # diff above is a *description* of it, kept for the report.  Applying it
        # would be a second edit of an already-edited file.
        applied, apply_note = True, "graded from the worktree"

        # The test patch is the maintainer's own test: it is what makes the
        # grade real, and it is applied *after* the model's change.
        test_ok, test_note = _apply(instance.test_patch, dest)
        if not test_ok:
            result.status = "environment_error"
            result.detail = f"test patch did not apply: {test_note}"
            return result

        f2p_ok, f2p_out = _run_tests(
            _pytest_node_ids(instance.fail_to_pass), dest, python=python,
            deadline=deadline,
        )
        if not f2p_ok and _looks_like_environment_failure(f2p_out):
            # The tests never ran: install what the repo declares and retry once.
            install_note = _install_test_requirements(dest, python, deadline=deadline)
            f2p_ok, f2p_out = _run_tests(
                _pytest_node_ids(instance.fail_to_pass), dest, python=python,
                deadline=deadline,
            )
            if not f2p_ok and _looks_like_environment_failure(f2p_out):
                result.status = "environment_error"
                result.detail = (
                    f"tests could not run ({install_note or 'no requirements file'}): "
                    f"{f2p_out[-300:]}"
                )
                return result
        result.fail_to_pass_total = len(instance.fail_to_pass)
        result.fail_to_pass_passed = len(instance.fail_to_pass) if f2p_ok else 0
        if not f2p_ok:
            # An exhausted budget is not a wrong answer.  Reporting it as
            # ``failed`` is how "the run did not fit" became "the model failed".
            result.status = "timeout" if deadline.expired else "failed"
            result.detail = f"FAIL_TO_PASS did not pass: {f2p_out[-400:]}"
            return result

        p2p_ok, p2p_out = _run_tests(
            _pytest_node_ids(instance.pass_to_pass), dest, python=python,
            deadline=deadline,
        )
        if not p2p_ok:
            result.status = "failed"
            result.pass_to_pass_regressed = 1
            result.detail = f"PASS_TO_PASS regressed: {p2p_out[-400:]}"
            return result

        result.status = "passed"
        result.detail = apply_note
        return result
    except Exception as exc:  # noqa: BLE001 - one instance must not kill the run
        result.status = "environment_error"
        result.detail = f"{type(exc).__name__}: {exc}"
        return result
    finally:
        result.latency_s = time.monotonic() - started
        _cleanup()


def run_swebench(
    instances: Sequence[SweInstance],
    *,
    work_root: Path | str = "/tmp/gigaforge_swe",
    timeout_s: int = 900,
    keep: bool = False,
    progress: Any = None,
) -> list[SweResult]:
    root = Path(work_root)
    root.mkdir(parents=True, exist_ok=True)
    results: list[SweResult] = []
    for i, instance in enumerate(instances, 1):
        if progress:
            progress(f"[{i}/{len(instances)}] {instance.instance_id}")
        results.append(run_instance(instance, work_root=root, timeout_s=timeout_s, keep=keep))
    return results


def summarise(results: Sequence[SweResult]) -> dict[str, Any]:
    """The honest summary: a pass rate over *gradable* instances, plus the rest.

    An instance whose environment could not be built is not a model failure and
    not a pass.  Reporting one blended number would hide which of the two it
    was, so the counts are separate and the rate is over the gradable set.
    """
    total = len(results)
    passed = sum(1 for r in results if r.status == "passed")
    env = sum(1 for r in results if r.status == "environment_error")
    gradable = total - env
    by_status: dict[str, int] = {}
    for r in results:
        by_status[r.status] = by_status.get(r.status, 0) + 1
    return {
        "total": total,
        "passed": passed,
        "gradable": gradable,
        "environment_errors": env,
        "pass_rate": round(passed / gradable, 3) if gradable else 0.0,
        "by_status": by_status,
        "tokens_in": sum(r.tokens_in for r in results),
        "tokens_out": sum(r.tokens_out for r in results),
        "median_latency_s": sorted(r.latency_s for r in results)[len(results) // 2]
        if results
        else 0.0,
    }
