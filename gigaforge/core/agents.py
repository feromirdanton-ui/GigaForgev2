"""Sub-agents with context and filesystem isolation (M4–M5).

Two kinds of isolation, because they solve different problems:

**Context isolation.**  Each sub-agent gets its own :class:`TaskState`: a fresh
scratchpad, its own plan, its own history.  The parent's reasoning does not leak
in, so a sub-agent cannot inherit a wrong assumption and — more importantly —
cannot pollute the parent by writing into a shared buffer.  What crosses the
boundary is a *goal* on the way in and a *result* on the way out, nothing else.

**Filesystem isolation.**  Each sub-agent works in its own ``git worktree`` on its
own branch.  Two sub-agents editing the same repository concurrently would
otherwise interleave their writes and produce a diff neither of them authored.
A worktree gives each one a real checkout, so its diff means what it says.

Aggregation is *always* in spawn order, never completion order.  Completion order
is nondeterministic, and a run whose output depends on which thread happened to
finish first cannot be reproduced or tested.
"""
from __future__ import annotations

import concurrent.futures as futures
import hashlib
import logging
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from .config import CoreConfig, SubAgentConfig
from .contracts import (
    Clock,
    RunContext,
    SubAgentResult,
    SubAgentSpec,
    SystemClock,
)
from .errors import WorktreeError

SubAgentFn = Callable[[SubAgentSpec, RunContext], Any]
log = logging.getLogger(__name__)


# ── worktrees ────────────────────────────────────────────────────────────────
@dataclass
class Worktree:
    """A checked-out worktree on its own branch."""

    name: str
    path: str
    branch: str
    base: str = ""
    created: bool = False
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "path": self.path,
            "branch": self.branch,
            "base": self.base,
            "created": self.created,
            "error": self.error,
        }


class WorktreeManager:
    """Creates and removes ``git worktree`` checkouts for sub-agents.

    Falls back to a plain directory copy when the target is not a git repository
    or ``git`` is unavailable, so the sub-agent machinery keeps working in a
    packaged/tarball deployment.  The fallback is reported in
    :meth:`Worktree.to_dict` via ``branch == ""`` — the caller is never misled
    into thinking it has git-level isolation when it does not.
    """

    def __init__(
        self,
        repo_root: str = ".",
        *,
        config: Optional[SubAgentConfig] = None,
        git_bin: str = "git",
        timeout: float = 30.0,
    ) -> None:
        self.repo_root = str(Path(repo_root).resolve())
        self.config = config or SubAgentConfig()
        self.git_bin = git_bin
        self.timeout = timeout
        root = self.config.worktree_root or os.path.join(self.repo_root, ".gigaforge", "worktrees")
        self.root = str(Path(root).resolve())
        self._created: dict[str, Worktree] = {}
        self._lock = threading.RLock()

    # ── git plumbing ─────────────────────────────────────────────────────────
    def _run(self, *args: str, cwd: Optional[str] = None) -> str:
        try:
            proc = subprocess.run(
                [self.git_bin, *args],
                cwd=cwd or self.repo_root,
                capture_output=True, text=True, timeout=self.timeout,
            )
        except FileNotFoundError as exc:
            raise WorktreeError(
                f"git binary {self.git_bin!r} not found",
                detail={"hint": "set GF_CORE__subagents__use_worktrees=false"},
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise WorktreeError(
                f"git {' '.join(args)} timed out after {self.timeout}s"
            ) from exc
        if proc.returncode != 0:
            raise WorktreeError(
                f"git {' '.join(args)} failed ({proc.returncode})",
                detail={"stderr": (proc.stderr or "")[:800]},
            )
        return proc.stdout

    def is_repo(self) -> bool:
        try:
            out = self._run("rev-parse", "--is-inside-work-tree")
        except WorktreeError:
            return False
        return out.strip() == "true"

    def head(self) -> str:
        try:
            return self._run("rev-parse", "HEAD").strip()
        except WorktreeError:
            return ""

    # ── lifecycle ────────────────────────────────────────────────────────────
    def create(self, name: str, *, base: str = "", branch: Optional[str] = None) -> Worktree:
        """Create a worktree named ``name``.

        Branch names are namespaced (``gigaforge/<name>``) so a run cannot
        clobber a branch the user cares about, and the path is always under
        ``worktree_root`` so cleanup is a bounded operation.
        """
        safe = _slug(name)
        branch_name = branch or f"gigaforge/{safe}-{int(time.time())}"
        path = os.path.join(self.root, safe)
        if not self.is_repo():
            return self._copy_fallback(name, path)

        with self._lock:
            Path(self.root).mkdir(parents=True, exist_ok=True)
            if os.path.lexists(path):
                raise WorktreeError(
                    f"worktree destination already exists: {path}",
                    detail={"hint": "choose a unique sub-agent name or remove its owned worktree"},
                )
            args = ["worktree", "add", "-b", branch_name, path]
            if base:
                args.append(base)
            elif self.head():
                args.append(self.head())
            self._run(*args)
            worktree = Worktree(name=name, path=path, branch=branch_name,
                                base=base or self.head(), created=True)
            self._created[safe] = worktree
            return worktree

    def _copy_fallback(self, name: str, path: str) -> Worktree:
        safe = _slug(name)
        Path(self.root).mkdir(parents=True, exist_ok=True)
        if os.path.lexists(path):
            raise WorktreeError(
                f"worktree destination already exists: {path}",
                detail={"hint": "choose a unique sub-agent name or remove its owned worktree"},
            )
        shutil.copytree(
            self.repo_root, path,
            ignore=shutil.ignore_patterns(".git", "node_modules", ".venv", "__pycache__",
                                          ".gigaforge", ".pytest_cache"),
        )
        worktree = Worktree(name=name, path=path, branch="", base="",
                            created=True, error="not a git repo: used a directory copy")
        with self._lock:
            self._created[safe] = worktree
        return worktree

    def remove(self, name: str) -> bool:
        """Remove a worktree, ignoring failures.

        Cleanup must never fail a run: a leaked directory is an annoyance, a
        raised exception at the end of successful work is a bug.
        """
        safe = _slug(name)
        with self._lock:
            worktree = self._created.pop(safe, None)
        if worktree is None:
            return False
        root = Path(self.root).resolve()
        path_obj = Path(worktree.path).resolve()
        try:
            relative = path_obj.relative_to(root)
        except ValueError:
            log.warning("refusing to remove worktree outside configured root: %s", path_obj)
            return False
        if not relative.parts:
            log.warning("refusing to remove worktree root itself: %s", path_obj)
            return False
        path = str(path_obj)
        removed = False
        if worktree and worktree.branch:
            try:
                self._run("worktree", "remove", "--force", path)
                removed = True
            except WorktreeError:
                pass
        if os.path.exists(path):
            shutil.rmtree(path, ignore_errors=True)
        if worktree and worktree.branch:
            try:
                self._run("branch", "-D", worktree.branch)
            except WorktreeError:
                pass
        return removed or not os.path.exists(path)

    def remove_all(self) -> int:
        with self._lock:
            names = list(self._created)
        return sum(1 for n in names if self.remove(n))

    def active(self) -> list[Worktree]:
        with self._lock:
            return list(self._created.values())

    def diff(self, name: str) -> str:
        """The sub-agent's changes as a unified diff against its base."""
        safe = _slug(name)
        with self._lock:
            worktree = self._created.get(safe)
        if worktree is None or not worktree.branch:
            return ""
        try:
            return self._run("diff", worktree.base or "HEAD", cwd=worktree.path)
        except WorktreeError:
            return ""


def _slug(name: str) -> str:
    original = str(name or "agent")
    keep = [c if (c.isalnum() or c in "-_") else "-" for c in original]
    out = "".join(keep).strip("-").lower()
    if not out:
        out = "agent"
    # Sanitizing punctuation can collapse distinct agent names into one folder
    # (for example, "review lead" and "review/lead"). Preserve readability and
    # append a stable suffix whenever normalization changed the identity.
    if original.strip().lower() != out:
        suffix = hashlib.sha256(original.encode("utf-8")).hexdigest()[:8]
        out = f"{out}-{suffix}"
    if out.casefold() in {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)),
                          *(f"lpt{i}" for i in range(1, 10))}:
        out = f"agent-{out}"
    return out


# ── sub-agent runner ─────────────────────────────────────────────────────────
@dataclass
class SubAgentHandle:
    """Bookkeeping for one spawned sub-agent."""

    index: int
    spec: SubAgentSpec
    worktree: Optional[Worktree] = None
    result: Optional[SubAgentResult] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "name": self.spec.name,
            "goal": self.spec.goal,
            "phase": self.spec.phase,
            "worktree": self.worktree.to_dict() if self.worktree else None,
            "result": self.result.to_dict() if self.result else None,
        }


class SubAgentRunner:
    """Fan out sub-agents into isolated worktrees and aggregate in spawn order."""

    def __init__(
        self,
        run_fn: SubAgentFn,
        *,
        config: Optional[CoreConfig] = None,
        worktrees: Optional[WorktreeManager] = None,
        metrics: Any = None,
        trace: Any = None,
        clock: Optional[Clock] = None,
        max_workers: Optional[int] = None,
    ) -> None:
        self.run_fn = run_fn
        self.config = config or CoreConfig()
        self.settings: SubAgentConfig = self.config.subagents
        self.worktrees = worktrees if worktrees is not None else (
            WorktreeManager(self.config.repo_root, config=self.settings)
            if self.settings.use_worktrees else None
        )
        self.metrics = metrics
        self.trace = trace
        self.clock: Clock = clock or SystemClock()
        self.max_workers = max(1, int(max_workers or self.settings.max_parallel))

    # ── spawn ────────────────────────────────────────────────────────────────
    def spawn(
        self,
        specs: Sequence[SubAgentSpec],
        *,
        parent_ctx: Optional[RunContext] = None,
        cleanup: Optional[bool] = None,
        parallel: bool = True,
    ) -> list[SubAgentResult]:
        """Run every spec and return results **in spawn order**.

        ``results[i]`` always corresponds to ``specs[i]``, whatever order the
        workers actually finish in.
        """
        if not specs:
            return []
        started = self.clock.now()
        cleanup = self.settings.keep_worktrees is False if cleanup is None else cleanup
        handles: list[SubAgentHandle] = []
        for index, spec in enumerate(specs):
            worktree: Optional[Worktree] = None
            if self.worktrees is not None:
                try:
                    worktree = self.worktrees.create(spec.name)
                except WorktreeError as exc:
                    # A worktree that cannot be created is reported on the
                    # handle, not raised: one broken sub-agent must not take
                    # down the whole fan-out.
                    worktree = Worktree(name=spec.name, path="", branch="", created=False,
                                        error=str(exc))
            handles.append(SubAgentHandle(index=index, spec=spec, worktree=worktree))

        self._emit("subagents.spawn", count=len(handles), parallel=parallel,
                   worktrees=self.worktrees is not None)

        if parallel and len(handles) > 1:
            self._run_parallel(handles, parent_ctx)
        else:
            for handle in handles:
                handle.result = self._run_one(handle, parent_ctx)

        results: list[SubAgentResult] = []
        for handle in handles:
            if handle.result is None:
                handle.result = SubAgentResult(
                    name=handle.spec.name, spawn_index=handle.index, ok=False,
                    error="sub-agent produced no result",
                )
            results.append(handle.result)

        if cleanup and self.worktrees is not None:
            for handle in handles:
                self.worktrees.remove(handle.spec.name)

        self._emit("subagents.finish", count=len(results),
                   ok=sum(1 for r in results if r.ok),
                   duration_ms=round((self.clock.now() - started) * 1000.0, 3))
        return results

    def spawn_one(
        self,
        spec: SubAgentSpec,
        *,
        parent_ctx: Optional[RunContext] = None,
        index: int = 0,
        cleanup: bool = True,
    ) -> SubAgentResult:
        """Run exactly one spec in its own worktree.

        This is the single-spec form of :meth:`spawn`, and it exists so the
        LangGraph fan-out can dispatch one graph branch per spec while still
        using *this* runner's isolation rules.  The graph previously drove the
        A2A runner (``agents/subagents.py``), which takes ``SubAgentSpawn`` — a
        type the product never builds.  Measured: with the default
        ``GIGAFORGE_SUBAGENT_ENGINE=langgraph`` every fan-out raised
        ``'SubAgentSpec' object has no attribute 'payload'``, the exception was
        swallowed by ``_subagent_diff``'s ``except Exception``, and
        ``--subagents`` silently ran with no sub-agents at all.

        Worktree creation and cleanup live here rather than in the graph: the
        runner owns them, and a graph node that created its own worktree would
        be a second, divergent implementation of the isolation rules.
        """
        worktree: Optional[Worktree] = None
        if self.worktrees is not None:
            try:
                worktree = self.worktrees.create(spec.name)
            except WorktreeError as exc:
                worktree = Worktree(name=spec.name, path="", branch="", created=False,
                                    error=str(exc))
        handle = SubAgentHandle(index=index, spec=spec, worktree=worktree)
        try:
            handle.result = self._run_one(handle, parent_ctx)
        finally:
            if cleanup and self.worktrees is not None:
                self.worktrees.remove(spec.name)
        return handle.result or SubAgentResult(
            name=spec.name, spawn_index=index, ok=False,
            error="sub-agent produced no result",
        )

    def _run_parallel(
        self, handles: list[SubAgentHandle], parent_ctx: Optional[RunContext]
    ) -> None:
        with futures.ThreadPoolExecutor(max_workers=self.max_workers,
                                        thread_name_prefix="subagent") as pool:
            future_map = {
                pool.submit(self._run_one, handle, parent_ctx): handle
                for handle in handles
            }
            for future in futures.as_completed(future_map):
                handle = future_map[future]
                try:
                    handle.result = future.result()
                except BaseException as exc:  # noqa: BLE001 - recorded per handle
                    handle.result = SubAgentResult(
                        name=handle.spec.name, spawn_index=handle.index, ok=False,
                        error=f"{type(exc).__name__}: {exc}"[:1000],
                    )

    def _run_one(
        self, handle: SubAgentHandle, parent_ctx: Optional[RunContext]
    ) -> SubAgentResult:
        spec = handle.spec
        started = self.clock.now()
        worktree = handle.worktree
        if self.trace is not None:
            self.trace.event("subagent.start", name=spec.name, spawn_index=handle.index,
                             phase=spec.phase)

        if worktree is not None and not worktree.created:
            self._emit("subagents.no_worktree", name=spec.name, error=worktree.error)
            if self.trace is not None:
                self.trace.event("subagent.finish", name=spec.name, spawn_index=handle.index,
                                 ok=False, error=worktree.error,
                                 duration_ms=round((self.clock.now() - started) * 1000.0, 3))
            return SubAgentResult(
                name=spec.name, spawn_index=handle.index, ok=False,
                error=f"worktree unavailable: {worktree.error}",
                worktree="", branch="",
            )

        # Context isolation: a fresh RunContext with its own artifacts bag and a
        # child TaskState.  The parent's extras never cross.
        ctx = self._isolated_context(handle, parent_ctx, worktree)
        try:
            produced = self.run_fn(spec, ctx)
        except BaseException as exc:  # noqa: BLE001 - recorded, not raised
            error = f"{type(exc).__name__}: {exc}"[:1000]
            if self.trace is not None:
                self.trace.event("subagent.finish", name=spec.name, spawn_index=handle.index,
                                 ok=False, error=error,
                                 duration_ms=round((self.clock.now() - started) * 1000.0, 3))
            return SubAgentResult(
                name=spec.name, spawn_index=handle.index, ok=False,
                error=error,
                worktree=worktree.path if worktree else "",
                branch=worktree.branch if worktree else "",
                metrics={"duration_ms": (self.clock.now() - started) * 1000.0},
            )

        result = self._normalise_result(handle, produced, worktree)
        result.metrics.setdefault("duration_ms", (self.clock.now() - started) * 1000.0)

        if self.metrics is not None:
            self.metrics.incr("core.subagent.runs", 1,
                              {"name": spec.name, "ok": str(result.ok)})
            self.metrics.observe("core.subagent.duration_ms",
                                 result.metrics["duration_ms"], {"name": spec.name})
        if self.trace is not None:
            self.trace.event("subagent.finish", name=spec.name, spawn_index=handle.index,
                             ok=result.ok, worktree=result.worktree,
                             duration_ms=round(result.metrics["duration_ms"], 3))
        return result

    def _isolated_context(
        self,
        handle: SubAgentHandle,
        parent_ctx: Optional[RunContext],
        worktree: Optional[Worktree],
    ) -> RunContext:
        spec = handle.spec
        if parent_ctx is None:
            ctx = RunContext(run_id=f"subagent-{handle.index}", phase=spec.phase,
                             node=spec.name, clock=self.clock, metrics=self.metrics,
                             trace=self.trace)
            if self.settings.isolate_context:
                ctx.state = _isolated_state(None, spec)
            return ctx
        ctx = parent_ctx.child(
            phase=spec.phase,
            node=spec.name,
            artifacts={},
            # The extras bag is where an "isolation leak" would hide, so it is
            # deliberately emptied for the sub-agent.
            extras={"spawn_index": handle.index, "subagent": spec.name},
        )
        if worktree is not None and worktree.path:
            ctx.extras["worktree"] = worktree.path
            ctx.extras["worktree_branch"] = worktree.branch
        if self.settings.isolate_context and not self.settings.inherit_state:
            ctx.state = _isolated_state(parent_ctx, spec)
        return ctx

    def _normalise_result(
        self, handle: SubAgentHandle, produced: Any, worktree: Optional[Worktree]
    ) -> SubAgentResult:
        spec = handle.spec
        base = SubAgentResult(
            name=spec.name, spawn_index=handle.index, ok=False,
            worktree=worktree.path if worktree else "",
            branch=worktree.branch if worktree else "",
        )
        if produced is None:
            base.error = "sub-agent returned nothing"
            return base
        if isinstance(produced, SubAgentResult):
            produced.spawn_index = handle.index
            produced.name = produced.name or spec.name
            if worktree is not None:
                produced.worktree = produced.worktree or worktree.path
                produced.branch = produced.branch or worktree.branch
            return produced
        if isinstance(produced, str):
            base.summary = produced[:4000]
            base.ok = True
            return base
        if isinstance(produced, Mapping):
            base.ok = bool(produced.get("ok", True))
            base.diff = str(produced.get("diff", ""))
            base.summary = str(produced.get("summary", ""))[:4000]
            base.usage = produced.get("usage")
            base.error = str(produced.get("error", ""))
            base.validate = produced.get("validate")
            extra = produced.get("metrics")
            if isinstance(extra, Mapping):
                base.metrics.update(extra)
            return base
        base.summary = str(produced)[:4000]
        base.ok = True
        return base

    # ── aggregation ──────────────────────────────────────────────────────────
    @staticmethod
    def aggregate(
        results: Sequence[SubAgentResult],
        *,
        owns_paths: Mapping[str, Sequence[str]] | None = None,
        root: Path | str = ".",
        config: Any = None,
    ) -> dict[str, Any]:
        """Merge sub-agent results **in spawn order**, through the acceptance gate.

        This method had no caller in the product: the pipeline concatenated the
        diffs itself with ``"\\n".join(...)``, which is how two children creating
        the same file merged to ``ok: True`` with one child's work silently gone.
        It now delegates to :func:`gigaforge.core.acceptance.merge_diffs`, so the
        merge has exactly one implementation and this method is the reporting
        view of it rather than a second, divergent one.

        ``owns_paths`` maps a child's name to the paths it was assigned; a child
        that wrote outside them is rejected with a reason instead of merged.
        """
        from .acceptance import merge_diffs

        ordered = sorted(results, key=lambda r: r.spawn_index)
        entries = [
            {
                "name": r.name,
                "diff": r.diff,
                "owns_paths": tuple((owns_paths or {}).get(r.name, ())),
            }
            for r in ordered
            if r.ok and r.diff
        ]
        report = merge_diffs(entries, root=root, config=config)
        return {
            "count": len(ordered),
            "ok": sum(1 for r in ordered if r.ok),
            "failed": [r.name for r in ordered if not r.ok],
            "order": [r.name for r in ordered],
            "summaries": {r.name: r.summary for r in ordered if r.summary},
            "diff": report.diff,
            "accepted": list(report.accepted),
            "rejected": list(report.rejected),
            "conflicts": list(report.conflicts),
            "errors": {r.name: r.error for r in ordered if r.error},
            "usage": _merge_usage([r.usage for r in ordered if r.usage]),
            "results": [r.to_dict() for r in ordered],
        }

    def _emit(self, kind: str, **payload: Any) -> None:
        if self.metrics is not None:
            self.metrics.incr(f"core.{kind}", 1)
        if self.trace is not None:
            self.trace.event(kind, **payload)


def _isolated_state(parent_ctx: RunContext, spec: SubAgentSpec) -> Any:
    """A fresh TaskState for a sub-agent, sharing only the immutable prefix.

    The prefix is shared on purpose: it is the stable, cacheable part, and
    re-deriving it per sub-agent would defeat prefix caching for no benefit.  The
    scratchpad, plan, history and counters are all new.
    """
    from .state import TaskState

    parent_state = getattr(parent_ctx, "state", None)
    state = TaskState(
        run_id=f"{getattr(parent_ctx, 'run_id', '') or 'root'}:{spec.name}",
        goal=spec.goal,
        prefix=getattr(parent_state, "prefix", None),
        slice_hash=getattr(parent_state, "slice_hash", ""),
        git_sha=getattr(parent_state, "git_sha", ""),
    )
    return state


def _merge_usage(usages: Sequence[Any]) -> dict[str, int]:
    merged = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "calls": 0}
    for usage in usages:
        if isinstance(usage, Mapping):
            data = usage
        elif hasattr(usage, "to_dict"):
            data = usage.to_dict()
        else:
            continue
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            merged[key] += int(data.get(key, 0) or 0)
        merged["calls"] += 1
    return merged


__all__ = [
    "WorktreeManager",
    "Worktree",
    "SubAgentRunner",
    "SubAgentHandle",
    "SubAgentSpec",
    "SubAgentResult",
    "SubAgentFn",
]
