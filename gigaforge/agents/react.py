"""ReAct (Reason→Act→Observe) loop — an alternative planner.

The ReAct loop is the Aider/SWE-agent/OpenHands pattern: instead of the static
Planner emitting a one-shot plan, the agent explores the repo (grep, read
structure, run tests) and *reasons* about what to do next. Each step:

1. **Reason** — an L1 Lite LLM call emits JSON ``{thought, action, action_input}``.
   ``thought`` is the model's free-form reasoning (≤1 sentence); ``action`` is
   the tool name; ``action_input`` is the structured arguments dict (or a string
   the tool interprets).
2. **Act** — the runner looks up ``action`` in its Tool registry and invokes it.
   Tools are bounded: ``grep``, ``read_structure``, ``run_tests``, ``finish``.
3. **Observe** — the tool's raw output is routed through the Firewall
   (``compress(...)``) so the next reasoning step never sees raw 5000-line
   pytest output. The compressed observation is appended to the trajectory.

Bounded by ``max_steps`` (default 5, env ``GIGAFORGE_REACT_MAX_STEPS``).
Terminates on: ``finish`` action (returns a plan + localization), step cap,
or :class:`BudgetExceeded` (which propagates as STOP).

Doctrine:
- Context is budget. Reasoning steps run on the L1 Lite model. Tool output is
  Firewall-compressed before any LLM sees it. Every LLM call is budget-checked
  + traced + metrics-recorded.
- Diff is contract. ReAct does NOT produce diffs; it produces a plan + a
  *localization* (target_function, target_file). The Coder + DiffValidator
  take it from there.
- No infinite loops. ``max_steps`` caps the loop. Each step has its own
  bounded budget check.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from gigaforge.agents.planner import json_repair
from gigaforge.client.errors import BudgetExceeded
from gigaforge.client.giga_client import ChatResult, GigaClient
from gigaforge.config import Settings, get_settings
from gigaforge.core.budgets import BudgetTracker
from gigaforge.core.context_window_guard import ContextOverflowError
from gigaforge.core.router import ModelRouter
from gigaforge.core.task_state import PlanStep, TaskState
from gigaforge.core.trace import Tracer
from gigaforge.firewall import compress
from gigaforge.firewall.tokens import estimate_tokens
from gigaforge.l0.index import L0Index

log = logging.getLogger(__name__)

__all__ = [
    "ReActRunner",
    "ReActResult",
    "Tool",
    "ToolRegistry",
    "ToolResult",
    "DEFAULT_REACT_MAX_STEPS",
    "FINISH_ACTION",
]

# Bounded loop cap. Env override for ops; default 5 = enough to grep +
# read_structure + run_tests + finish.
DEFAULT_REACT_MAX_STEPS = 5

# The special action that terminates the loop with a synthesized plan.
FINISH_ACTION = "finish"

#: Budget/routing phase for a ReAct episode.
#:
#: It used to be ``"plan"``, which is a *single-call* phase with a 2 000-token
#: input cap.  A ReAct episode is a loop: the harness prompt alone is ~1 100
#: tokens, so the second step of a live episode died with ``BudgetExceeded``
#: and the run fell back to the linear plan with zero steps.  ``"react"`` has
#: its own, larger cap (``GIGAFORGE_BUDGET_REACT_IN``).
_REACT_PHASE = "react"

# Tool output hard cap (tokens). Each observation fits comfortably under the
# Lite model's input budget. The Firewall compressors may produce smaller
# payloads, but we cap the worst case here as defence-in-depth.
_OBS_TOKEN_BUDGET = 1500

#: How many times one episode may compact its own trajectory.  Two is enough
#: for a normal run (the first compaction buys most of the window back); the
#: cap exists so a pathological trajectory cannot oscillate between "compact"
#: and "overflow" until the step budget runs out.
_MAX_COMPACTIONS = 2

# L1 Lite prompt — short and bounded. Asks for ONE JSON object per step.
_REACT_PROMPT = (
    "Ты агент в цикле ReAct (Reason→Act→Observe). Изучи репозиторий и "
    "сформируй план редактирования.\n"
    "На каждом шаге верни РОВНО ОДИН JSON-объект вида:\n"
    '{"thought":"...","action":"<tool>","action_input":"<str>"}\n'
    "Доступные инструменты (action):\n"
    '- "grep"      — поиск по репозиторию; action_input — регулярное выражение.\n'
    '- "read_structure" — скелет файла; action_input — относительный путь.\n'
    '- "run_tests" — запустить pytest; action_input — аргументы (или пустая строка).\n'
    '- "finish"    — завершить, подготовить план редактирования.\n'
    '  Для finish: action_input = JSON-строка вида\n'
    '  \'[{"tool":"edit_function","args":{"name":"<symbol>"}}]\'.\n'
    "Поле \"thought\" — короткое рассуждение (≤1 предложения).\n"
    "Не пиши ничего вне JSON. Никакого markdown. Только JSON."
)


class Tool(Protocol):
    """Bounded tool protocol — invoked by the ReAct runner.

    The minimal local protocol (not the ADAPT PluginRegistry). When the ADAPT
    agent lands its PluginRegistry, ReAct should consume that instead — the
    shape is intentionally compatible (an async ``run(input) -> str`` method).
    """

    name: str

    async def run(self, action_input: str, *, ctx: ReActContext) -> str:
        """Execute the tool; return raw output (the runner compresses it)."""
        ...


@dataclass
class ToolResult:
    """One tool invocation result. ``raw`` is compressed before LLM sees it."""

    action: str
    raw: str
    compressed: dict[str, Any] = field(default_factory=dict)
    ok: bool = True
    error: str | None = None


@dataclass
class ReActContext:
    """Per-ReAct-run context: repo_root + worktree + index + sandbox."""

    repo_root: Path
    worktree: Path
    index: L0Index
    sandbox: Any  # SandboxExecutor — typed loosely to avoid circular import


@dataclass
class ReActResult:
    """Final result of a ReAct run."""

    plan: list[PlanStep] = field(default_factory=list)
    localization: dict[str, str] = field(default_factory=dict)
    steps: list[dict[str, Any]] = field(default_factory=list)
    finished: bool = False
    finish_reason: str = ""  # "finish_action" | "max_steps" | "budget_exceeded" | "error"
    llm_calls: int = 0
    #: The episode's own trajectory.  Carried out of the run so a caller can
    #: salvage a plan from what was actually observed when the model's finish
    #: payload is unusable — the guard drops an untargeted edit, and without
    #: this the run reports zero steps despite having the answer in hand.
    trajectory: list[dict[str, Any]] = field(default_factory=list)


# --- Built-in tools ---------------------------------------------------------


def _clean_pattern(raw: str) -> str:
    """Strip the quoting a model puts around a grep pattern.

    Measured on a live run: the model asked for ``grep`` with the argument
    ``"TODO"`` \u2014 quotes included.  ``rg`` then searched for the literal
    six-character string ``"TODO"``, found nothing, and the episode spent its
    whole step budget retrying case variants of a pattern that was never
    actually searched for.  A pattern is a pattern, not a shell word.
    """
    text = (raw or "").strip()
    for quote in ('"', "'", "`"):
        if len(text) >= 2 and text.startswith(quote) and text.endswith(quote):
            text = text[1:-1].strip()
            break
    return text


class GrepTool:
    """Repo grep, ripgrep when it is installed and pure Python otherwise.

    **Why the fallback exists.**  The tool shelled out to ``rg`` unconditionally.
    On a machine without ripgrep the subprocess raised ``FileNotFoundError``,
    which the handler turned into ``"ERROR: grep failed: ..."`` \u2014 and on a
    machine *with* it, an empty result was returned as an empty string.  In a
    live episode that combination was indistinguishable from "the marker is not
    in the repo": the model retried case variants of a pattern it never actually
    searched for, burned its whole step budget, and the plan came back empty.
    Verified in this sandbox: ``which rg`` -> not found.

    A harness tool must not depend on a binary it does not install.  ``rg`` is
    still preferred when present (it is much faster and honours ``.gitignore``);
    the Python path walks the worktree with the same skip rules.
    """

    name = "grep"

    #: Directories never worth walking.  ``rg`` skips these via ``.gitignore``;
    #: the Python path has to do it explicitly, or it greps vendored code and
    #: ``.git`` objects and returns thousands of irrelevant lines.
    _SKIP_DIRS = frozenset(
        {
            ".git", ".venv", "venv", "node_modules", "__pycache__",
            ".pytest_cache", ".ruff_cache", ".mypy_cache", "dist", "build",
            ".eggs", "site-packages",
        }
    )
    #: Files larger than this are skipped: a model does not need a 40 MB dump.
    _MAX_BYTES = 2_000_000

    async def run(self, action_input: str, *, ctx: ReActContext) -> str:
        import asyncio
        import re
        import shutil

        if not action_input or not action_input.strip():
            return "ERROR: grep needs a pattern"
        pattern = _clean_pattern(action_input)
        if not pattern:
            return "ERROR: grep needs a pattern"

        if shutil.which("rg"):
            try:
                proc = await asyncio.create_subprocess_exec(
                    # ``--smart-case``: a model that asks for "TODO" means the
                    # marker, not the exact byte sequence.
                    "rg", "-n", "--no-heading", "--smart-case", pattern,
                    str(ctx.worktree),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout_b, _ = await asyncio.wait_for(proc.communicate(), timeout=20)
                out = (stdout_b or b"").decode("utf-8", errors="replace")
                if out.strip():
                    return out
                return f"(no matches for {pattern!r} under {ctx.worktree})"
            except Exception:  # noqa: BLE001 - fall through to the pure-Python path
                pass

        return await asyncio.to_thread(self._py_grep, pattern, ctx.worktree, re)

    def _py_grep(self, pattern: str, root: Path, re_module: Any) -> str:
        """Pure-Python grep. Case-insensitive, honouring the skip list."""
        try:
            rx = re_module.compile(pattern, re_module.IGNORECASE)
        except re_module.error:
            # A malformed regex is a model mistake, not a crash: search literally.
            rx = re_module.compile(re_module.escape(pattern), re_module.IGNORECASE)

        hits: list[str] = []
        for path in root.rglob("*"):
            if len(hits) >= 200:
                hits.append("... (truncated at 200 matches)")
                break
            if not path.is_file():
                continue
            if any(part in self._SKIP_DIRS for part in path.parts):
                continue
            try:
                if path.stat().st_size > self._MAX_BYTES:
                    continue
                text = path.read_text(encoding="utf-8", errors="ignore")
            except (OSError, ValueError):
                continue
            for lineno, line in enumerate(text.splitlines(), start=1):
                if rx.search(line):
                    hits.append(f"{path.relative_to(root)}:{lineno}:{line[:300]}")

        if not hits:
            return f"(no matches for {pattern!r} under {root})"
        return "\n".join(hits)


class ReadStructureTool:
    """L0 read_structure — returns skeletons (no bodies) of a file."""

    name = "read_structure"

    async def run(self, action_input: str, *, ctx: ReActContext) -> str:
        if not action_input or not action_input.strip():
            return "ERROR: read_structure needs a file path"
        rel = action_input.strip()
        path = ctx.worktree / rel
        if not path.exists():
            return f"ERROR: file not found: {rel}"
        # Re-index the file to ensure the L0 knows about it.
        try:
            ctx.index.index_file(path)
        except Exception:  # noqa: BLE001 — best-effort
            pass
        skel = ctx.index.read_structure(str(path))
        if not skel:
            return f"(empty file: {rel})"
        lines = []
        for s in skel:
            lines.append(f"{s.start_line:4d} {s.kind:8s} {s.qname}  ::  {s.signature}")
        return "\n".join(lines)


class RunTestsTool:
    """Pytest via the existing SandboxExecutor (allowlist-enforced)."""

    name = "run_tests"

    async def run(self, action_input: str, *, ctx: ReActContext) -> str:
        # action_input may be "" or "-x tests/test_foo.py"; split on whitespace.
        raw = (action_input or "").strip()
        args = raw.split() if raw else None
        try:
            exec = await ctx.sandbox.run_tests(args)
        except Exception as exc:  # noqa: BLE001 — best-effort
            return f"ERROR: run_tests failed: {exc}"
        # Return raw_output — the runner compresses via Firewall.
        return exec.raw_output or "(no output)"


class FinishTool:
    """Sentinel 'finish' tool — never actually called; the runner intercepts
    ``action == "finish"`` before invoking the registry."""

    name = "finish"

    async def run(self, action_input: str, *, ctx: ReActContext) -> str:  # pragma: no cover
        return action_input or ""


class ToolRegistry:
    """Bounded registry of tools available to ReAct.

    Built-in tools (grep, read_structure, run_tests) are pre-registered.
    The ADAPT agent's PluginRegistry, when it lands, can be plugged in here
    via :meth:`register` — TODO integration with ADAPT.
    """

    def __init__(self, *, sandbox: Any | None = None) -> None:
        self._tools: dict[str, Tool] = {}
        # Built-ins are registered lazily — they need a sandbox for run_tests.
        self._sandbox = sandbox

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools.keys())

    def install_defaults(self, *, sandbox: Any) -> None:
        """Install the built-in grep/read_structure/run_tests tools."""
        self._sandbox = sandbox
        self.register(GrepTool())
        self.register(ReadStructureTool())
        self.register(RunTestsTool())
        self.register(FinishTool())


# --- The runner -------------------------------------------------------------


class ReActRunner:
    """Bounded ReAct (Reason→Act→Observe) loop over the L1 Lite model.

    Each iteration issues exactly ONE LLM call (Lite) and at most ONE tool
    invocation. Every LLM call is budget-checked (phase="react") and
    traced. Tool output is compressed through the Firewall before the next
    iteration's prompt sees it.

    On ``finish``, the runner parses the model-supplied plan (the JSON-encoded
    ``action_input``) into :class:`PlanStep` objects, and the pipeline uses
    them as it would a static Planner's output.

    On ``max_steps`` breach: returns whatever plan the model has emitted so
    far (possibly empty) with ``finish_reason="max_steps"`` — the pipeline
    decides what to do.

    On :class:`BudgetExceeded`: propagates (STOP + report semantics).
    """

    def __init__(
        self,
        client: GigaClient,
        router: ModelRouter,
        budgets: BudgetTracker,
        tracer: Tracer,
        *,
        registry: ToolRegistry | None = None,
        max_steps: int | None = None,
        settings: Settings | None = None,
        guards: Any | None = None,
    ) -> None:
        self._client = client
        self._router = router
        self._budgets = budgets
        self._tracer = tracer
        self._s = settings or get_settings()
        self._registry = registry or ToolRegistry()
        self._max_steps = max_steps or _env_max_steps()
        # The ported middleware (tool contract, shell safety, specification
        # audit, think tool).  Built lazily so a caller that does not pass one
        # still gets the guards rather than silently running without them.
        self._guards = guards
        #: ContextWindowGuard.  The LangGraph engine had one; the classic loop
        #: — which is the *default* engine — did not, so the server token count
        #: was unreachable in the configuration most deployments run.  Built
        #: lazily so the guard can pick up the client's counter.
        self._window_guard: Any = None
        #: Compactions performed in the current episode.  Bounded so a
        #: pathological trajectory cannot oscillate between compact and
        #: overflow; reset at the start of every ``run``.
        self._compactions = 0

    @property
    def registry(self) -> ToolRegistry:
        return self._registry

    # -- public collaborators -------------------------------------------------
    # The LangGraph engine drives this runner from outside, so it needs the
    # runner's collaborators.  Reaching for ``_router`` / ``_budgets`` /
    # ``_tracer`` / ``_client`` with ``# noqa: SLF001`` made every refactor of
    # this class a silent breakage of the graph; these accessors are the
    # supported seam.
    @property
    def client(self) -> GigaClient:
        return self._client

    @property
    def router(self) -> ModelRouter:
        return self._router

    @property
    def budgets(self) -> BudgetTracker:
        return self._budgets

    @property
    def tracer(self) -> Tracer:
        return self._tracer

    @property
    def settings(self) -> Settings:
        return self._s

    @property
    def max_steps(self) -> int:
        return self._max_steps

    def parse_step(self, text: str) -> tuple[str, str, str] | None:
        """Public wrapper over the step parser, for the graph engine."""
        return self._parse_step(text)

    def parse_finish_plan(self, action_input: str) -> list[Any]:
        """Public wrapper over the finish-payload parser."""
        return self._parse_finish_plan(action_input)

    def synthesize_plan(self, trajectory: list[dict[str, Any]]) -> list[Any]:
        """Public wrapper over the observation-based plan salvage."""
        return self._synthesize_plan(trajectory)

    def extract_localization(
        self, plan: list[Any], last_thought: str = ""
    ) -> dict[str, str]:
        """Public wrapper over the localization extractor."""
        return self._extract_localization(plan, last_thought)

    def compose_initial_prompt(
        self,
        task: str,
        state: Any,
        bootstrap_hint: str | None = None,
    ) -> str:
        """Public wrapper over the opening prompt builder."""
        return self._compose_initial_prompt(task, state, bootstrap_hint)

    @property
    def guards(self) -> Any:
        """The wired middleware, created on first use."""
        if self._guards is None:
            from gigaforge.agents.guards import ReActGuards

            self._guards = ReActGuards(tool_names=self._registry.names())
        return self._guards

    @property
    def window_guard(self) -> Any:
        """The context guard, wired to the client's server-side counter.

        Both engines must behave identically: the LangGraph graph already
        compacted before a request, the classic loop did not.  The counter is
        cached per trajectory prefix (see :mod:`gigaforge.core.token_cache`),
        because a ReAct trajectory grows monotonically and re-counting the same
        prefix on every step doubles the number of HTTP round trips.
        """
        if self._window_guard is None:
            from gigaforge.core.context_window_guard import ContextWindowGuard
            from gigaforge.core.token_cache import CachedTokenCounter

            counter = getattr(self._client, "count_tokens", None)
            self._window_guard = ContextWindowGuard(
                token_counter=(
                    CachedTokenCounter(counter) if callable(counter) else None
                )
            )
        return self._window_guard

    def _compact(
        self,
        trajectory: list[dict[str, str]],
        state: TaskState,
        step_idx: int,
        exc: ContextOverflowError,
    ) -> list[dict[str, str]] | None:
        """Compact *trajectory* so the next request fits; ``None`` if hopeless.

        Returns the compacted trajectory, or ``None`` when compaction cannot
        buy enough room — in which case the caller stops the run with
        ``finish_reason="context_overflow"``, which is the old behaviour and
        still the right answer when the goal itself is too large.

        The compaction is bounded: at most ``_MAX_COMPACTIONS`` per episode, so
        a pathological trajectory cannot spin between "compact" and "overflow"
        forever.  Each compaction is traced, because a run that silently
        rewrote its own history is a run whose token accounting needs a note.
        """
        from gigaforge.harness.compaction import compact_trajectory, trim_stale_file_outputs

        if self._compactions >= _MAX_COMPACTIONS:
            return None
        before = sum(len(str(m.get("content", ""))) for m in trajectory) // 4
        window = self.window_guard.window_for(self._router.route(_REACT_PHASE))
        target = int(window * self.window_guard.trigger_fraction * 0.75)
        # Two passes, cheapest first (ported from deepagents-gigachat):
        #   1. trim the bodies of large STALE file observations — keeps the
        #      message and its head, drops the bulk;
        #   2. only if that was not enough, drop whole middle messages.
        # Doing (1) first means a long episode usually keeps its full reasoning
        # history and loses only the file text it has already finished with.
        trimmed = trim_stale_file_outputs(trajectory)
        trimmed_tokens = sum(len(str(m.get("content", ""))) for m in trimmed) // 4
        if trimmed_tokens < before:
            compacted = compact_trajectory(trimmed, target_tokens=target)
        else:
            compacted = compact_trajectory(trajectory, target_tokens=target)
        after = sum(len(str(m.get("content", ""))) for m in compacted) // 4
        if after >= before:
            # Nothing was dropped: the goal alone is over the limit.
            return None
        self._compactions += 1
        self._tracer.trace(
            state.task_id,
            step=f"react_step_{step_idx}_compaction",
            kind="decision",
            detail={
                "step_idx": step_idx,
                "before_tokens": before,
                "after_tokens": after,
                "dropped_messages": len(trajectory) - len(compacted),
                "trigger": str(exc)[:200],
            },
        )
        return compacted

    async def run(
        self,
        task: str,
        state: TaskState,
        *,
        ctx: ReActContext,
        bootstrap_hint: str | None = None,
    ) -> ReActResult:
        """Run the bounded ReAct loop. Returns a :class:`ReActResult`.

        The result's ``plan`` is a list of :class:`PlanStep` (possibly empty if
        the model never finished). The result's ``localization`` is the best
        guess at ``{"target_function","target_file"}`` extracted from the
        final finish payload (or the last thought if no finish).
        """
        if not self._registry.names():
            self._registry.install_defaults(sandbox=ctx.sandbox)
        self._compactions = 0
        # The think tool is a real tool, not a prompt trick: registering it is
        # what lets the model externalise a reasoning step.
        self.guards.install(self._registry)

        result = ReActResult()
        trajectory: list[dict[str, str]] = []  # role-tagged messages for the LLM
        base_user = self._compose_initial_prompt(task, state, bootstrap_hint)
        trajectory.append({"role": "user", "content": base_user})
        # ToolContract: tell the model which tools exist, exactly once.
        trajectory.extend(self.guards.opening_messages(trajectory))

        for step_idx in range(self._max_steps):
            # ContextWindowGuard: compact before the request, not after a 422.
            # Detection alone used to *end* the run; a long episode died at 80%
            # of the window with most of its trajectory being stale observation
            # text.  Now the guard's verdict triggers a compaction and the step
            # is retried, so the run continues instead of stopping.
            try:
                self.window_guard.check(trajectory, model=self._router.route(_REACT_PHASE))
            except ContextOverflowError as exc:
                compacted = self._compact(trajectory, state, step_idx, exc)
                if compacted is None:
                    result.finished = True
                    result.finish_reason = "context_overflow"
                    result.llm_calls = step_idx
                    self._tracer.trace(
                        state.task_id,
                        step=f"react_step_{step_idx}_context_overflow",
                        kind="decision",
                        detail={"step_idx": step_idx, "detail": str(exc)[:400]},
                    )
                    return result
                trajectory = compacted

            # Pre-budget check. Phase = "plan" (L1 Lite caps).
            est_in = sum(len(m["content"]) for m in trajectory) // 4
            est_out = max(self._s.budget_plan_out // 3, 200)
            self._budgets.check(_REACT_PHASE, est_in, est_out, task_id=state.task_id)

            # Reason — one LLM call.
            started = time.monotonic()
            chat_result: ChatResult = await self._client.chat(
                self._router.route(_REACT_PHASE),
                trajectory,
                temperature=0.1,
                max_tokens=int(self._s.budget_plan_out),
                task_id=state.task_id,
                phase=_REACT_PHASE,
            )
            latency_ms = int((time.monotonic() - started) * 1000)
            text = chat_result.content or ""
            usage = chat_result.usage or {}
            tokens_in = int(usage.get("prompt_tokens", 0) or 0)
            tokens_out = int(usage.get("completion_tokens", 0) or 0)

            try:
                self._budgets.commit_task(state.task_id, _REACT_PHASE, tokens_in, tokens_out)
                self._budgets.check_overshoot(_REACT_PHASE, task_id=state.task_id)
            except BudgetExceeded:
                result.finish_reason = "budget_exceeded"
                result.llm_calls = step_idx + 1
                # The budget is a hard stop \u2014 the exception still propagates \u2014
                # but the episode has already paid for its observations.  Salvage
                # a plan from them first, so a run that ran out of tokens still
                # hands the DAG something to execute instead of nothing.  This is
                # the same recovery as the step cap, for the same reason.
                salvaged = self._synthesize_plan(trajectory)
                if salvaged:
                    result.plan = salvaged
                    result.localization = self._extract_localization(salvaged, "")
                    result.finished = True
                self._tracer.trace(
                    state.task_id,
                    step=f"react_step_{step_idx}_budget_exceeded",
                    kind="decision",
                    detail={
                        "step_idx": step_idx,
                        "tokens_in": tokens_in,
                        "tokens_out": tokens_out,
                        "salvaged_plan_size": len(salvaged),
                    },
                )
                raise
            except Exception:  # noqa: BLE001 — accounting best-effort
                pass

            result.llm_calls = step_idx + 1
            self._tracer.trace(
                state.task_id,
                step=f"react_step_{step_idx}",
                kind="llm",
                detail={
                    "step_idx": step_idx,
                    "tokens_in": tokens_in,
                    "tokens_out": tokens_out,
                    "estimated": bool(chat_result.estimated),
                    "raw_head": text[:300],
                },
                tokens=tokens_in + tokens_out,
                latency_ms=latency_ms,
            )

            parsed = self._parse_step(text)
            if parsed is None:
                # Repair nudge — keep going (still bounded by max_steps).
                trajectory.append({"role": "assistant", "content": text})
                trajectory.append(
                    {
                        "role": "user",
                        "content": (
                            "Твой прошлый ответ не является JSON вида "
                            '{thought,action,action_input}. Верни ТОЛЬКО JSON.'
                        ),
                    }
                )
                result.steps.append(
                    {"step_idx": step_idx, "raw": text, "ok": False, "error": "parse_failed"}
                )
                continue

            thought, action, action_input = parsed
            result.steps.append(
                {
                    "step_idx": step_idx,
                    "thought": thought,
                    "action": action,
                    "action_input": action_input,
                    "ok": True,
                }
            )

            # Termination.
            if action == FINISH_ACTION:
                plan = self._parse_finish_plan(action_input)
                if not plan:
                    # The model said "finish" but its payload did not parse.
                    # Returning here with an empty plan threw away every
                    # observation the episode had paid for \u2014 measured live: the
                    # model had located the files, emitted a finish whose
                    # ``action_input`` was prose, and the run reported
                    # ``finish_action`` with zero steps.  Recover from what was
                    # actually observed before giving up.
                    plan = self._synthesize_plan(trajectory)
                result.plan = plan
                result.localization = self._extract_localization(plan, thought)
                result.finished = True
                result.finish_reason = "finish_action"
                self._tracer.trace(
                    state.task_id,
                    step=f"react_finish_{step_idx}",
                    kind="decision",
                    detail={
                        "n_steps": step_idx + 1,
                        "plan_size": len(plan),
                        "localization": result.localization,
                        "recovered_from_observations": bool(plan) and not self._parse_finish_plan(action_input),
                    },
                )
                return result

            # Act — invoke the tool.
            tool = self._registry.get(action)
            if tool is None:
                trajectory.append({"role": "assistant", "content": text})
                trajectory.append(
                    {
                        "role": "user",
                        "content": (
                            f"Инструмент {action!r} неизвестен. Доступны: "
                            f"{self._registry.names()} + 'finish'."
                        ),
                    }
                )
                continue

            # ShellSafety: screen the call before it reaches the sandbox.  A
            # blocked command becomes an observation, so the model can correct
            # course instead of the harness executing it.
            blocked = self.guards.screen_tool_call(action, action_input)
            if blocked is not None:
                kind = _kind_for_tool(action)
                compressed = compress(kind, blocked, limit_tokens=_OBS_TOKEN_BUDGET)
                observation = json.dumps(compressed, ensure_ascii=False)
                trajectory.append({"role": "assistant", "content": text})
                trajectory.append(
                    {"role": "user", "content": f"Observation ({kind}):\n{observation}"}
                )
                result.steps.append(
                    {
                        "step_idx": step_idx,
                        "thought": thought,
                        "action": action,
                        "action_input": action_input,
                        "ok": True,
                        "observation": observation,
                        "blocked": True,
                    }
                )
                continue

            try:
                raw_obs = await tool.run(action_input, ctx=ctx)
            except Exception as exc:  # noqa: BLE001 — best-effort
                raw_obs = f"ERROR: tool {action!r} raised: {exc}"

            # Observe — compress the raw output via the Firewall.
            kind = _kind_for_tool(action)
            # PathNormalizer (ported from deepagents-gigachat): a tool that
            # walks the tree returns paths rooted at the workspace
            # (``/src/foo.py``); the model copies them verbatim into a plan and
            # the executor then looks for ``/src/foo.py`` on the host.  Make
            # them relative BEFORE compression, so the compressed payload the
            # model reads is already correct.
            raw_obs = self.guards.normalize_observation(action, raw_obs)
            compressed = compress(kind, raw_obs, limit_tokens=_OBS_TOKEN_BUDGET)
            observation = json.dumps(compressed, ensure_ascii=False)
            # Cap the observation text (belt-and-braces).
            if estimate_tokens(observation) > _OBS_TOKEN_BUDGET:
                observation = observation[: _OBS_TOKEN_BUDGET * 4] + "\n[truncated]"

            trajectory.append({"role": "assistant", "content": text})
            trajectory.append(
                {
                    "role": "user",
                    "content": f"Observation ({kind}):\n{observation}",
                }
            )
            # SpecificationAudit: after the model inspects a file it just
            # created, nudge it once to check the output against the contract.
            audit = self.guards.after_observation(trajectory)
            if audit is not None:
                trajectory.append(audit)

        # Reached the step cap without finishing.
        #
        # The old code simply returned an EMPTY plan here, throwing away every
        # observation the episode had paid for \u2014 on a live run the model had
        # already located the files but the harness binned the result because it
        # had used its steps on exploration rather than on ``finish``.  That is
        # the harness's failure, not the model's, so give it one last call whose
        # only job is to turn what it already knows into a plan.
        forced = await self._force_finish(trajectory, state, ctx)
        if forced is not None and forced.plan:
            result.plan = forced.plan
            result.localization = forced.localization
            result.finished = True
            result.finish_reason = "forced_finish"
            self._tracer.trace(
                state.task_id,
                step="react_forced_finish",
                kind="decision",
                detail={
                    "plan_size": len(forced.plan),
                    "llm_calls": result.llm_calls,
                    "note": "step cap reached; plan recovered from the trajectory",
                },
            )
            return result

        result.finish_reason = "max_steps"
        self._tracer.trace(
            state.task_id,
            step="react_max_steps_reached",
            kind="decision",
            detail={"max_steps": self._max_steps, "llm_calls": result.llm_calls},
        )
        # Carry the trajectory out: a caller that finds the plan unusable can
        # salvage one from what the episode actually observed.
        result.trajectory = list(trajectory)
        return result

    async def _force_finish(
        self, trajectory: list[dict[str, str]], state: TaskState, ctx: ReActContext
    ) -> ReActResult | None:
        """One last turn asking only for the plan, given everything observed.

        Returns ``None`` when the call fails or the answer holds no usable plan,
        so the caller can fall back to the plain ``max_steps`` outcome.
        """
        convo = list(trajectory)
        convo.append(
            {
                "role": "user",
                "content": (
                    "You are out of steps. Do NOT call any tool. Using only the "
                    "observations above, return the plan now as ONE JSON object:\n"
                    '{"thought": "...", "action": "finish", "action_input": '
                    '"[{\\"tool\\": \\"edit_function\\", \\"args\\": {\\"name\\": \\"<symbol>\\"}}]"}\n'
                    "If the observations are inconclusive, still return your best "
                    "single most likely edit. Never return an empty list."
                ),
            }
        )
        try:
            chat_result: ChatResult = await self._client.chat(
                self._router.route(_REACT_PHASE),
                convo,
                temperature=0.0,
                max_tokens=int(self._s.budget_plan_out),
                task_id=state.task_id,
                phase=_REACT_PHASE,
            )
        except Exception as exc:  # noqa: BLE001 - a failed rescue is not fatal
            self._tracer.trace(
                state.task_id,
                step="react_forced_finish_failed",
                kind="decision",
                detail={"error": str(exc)[:300]},
            )
            return None

        text = chat_result.content or ""
        parsed = self._parse_step(text)
        plan: list[PlanStep] = []
        thought = ""
        if parsed is not None:
            thought, action, action_input = parsed
            if action == FINISH_ACTION:
                plan = self._parse_finish_plan(action_input)
        if not plan:
            # The model may answer with a bare plan list; accept that too.
            plan = self._parse_finish_plan(text)
        if not plan:
            # Last resort: build the plan from what the episode actually saw.
            # A model that has already located the files but refuses to emit a
            # plan must not cost the run its whole exploration budget.
            plan = self._synthesize_plan(trajectory)
            thought = thought or "plan synthesised from observations"
        if not plan:
            return None

        rescued = ReActResult()
        rescued.plan = plan
        rescued.localization = self._extract_localization(plan, thought)
        return rescued

    def _synthesize_plan(self, trajectory: list[dict[str, str]]) -> list[PlanStep]:
        """Derive a plan from the files the episode actually observed.

        Deterministic, no model call.  The Firewall compresses a ``grep``
        observation into ``{"matches": [{"file": ..., "line": ..., ...}]}``, so
        the distinct ``file`` values are the files the episode found relevant.

        The salvaged steps are **read** steps, not edits.  An earlier version
        emitted one ``edit_function`` per observed file, and a live run showed
        why that is wrong: asked to *find* the TODO markers, the episode grepped,
        found them, and the salvage turned that into "edit these four files".
        The episode had no idea what to change — it was never asked to change
        anything — so the plan invented work.  A read step is always true of an
        observation; an edit step is a claim the episode never made.

        Returns an empty list when nothing was observed, so the caller still
        reports ``max_steps`` honestly rather than inventing work.
        """
        seen: list[str] = []
        for message in trajectory:
            if message.get("role") != "user":
                continue
            content = message.get("content") or ""
            if not content.startswith("Observation"):
                continue
            for path in self._paths_in_observation(content):
                if path not in seen:
                    seen.append(path)
                if len(seen) >= 5:
                    break
            if len(seen) >= 5:
                break

        steps: list[PlanStep] = []
        for path in seen:
            try:
                steps.append(PlanStep(tool="read_structure", args={"path": path}))
            except Exception:  # noqa: BLE001 - a malformed step is skipped
                continue
        return steps

    @staticmethod
    def _paths_in_observation(content: str) -> list[str]:
        """Source paths mentioned by one observation, in order, deduplicated."""
        found: list[str] = []
        # Shape 1: the Firewall's compressed grep payload.
        brace = content.find("{")
        if brace != -1:
            try:
                payload = json.loads(content[brace:])
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, dict):
                for match in payload.get("matches") or []:
                    if isinstance(match, dict):
                        candidate = str(match.get("file") or "").strip()
                        if candidate:
                            found.append(candidate)
        # Shape 2: raw ``path:line:text`` rows.
        for line in content.splitlines():
            head, sep, rest = line.partition(":")
            if not sep or not rest[:1].isdigit():
                continue
            candidate = head.strip()
            if candidate:
                found.append(candidate)

        out: list[str] = []
        for candidate in found:
            if candidate.startswith("(") or candidate in out:
                continue
            if not candidate.endswith((".py", ".md", ".toml", ".yaml", ".yml", ".json")):
                continue
            out.append(candidate)
        return out

    # ----------------------------------------------------------- internals

    def _compose_initial_prompt(
        self, task: str, state: TaskState, hint: str | None
    ) -> str:
        prefix = state.serialize_prefix()
        # The harness prompt (ported from ai-forever/deepagents-gigachat) is
        # prepended, not substituted: _REACT_PROMPT carries the JSON step
        # contract this loop parses, and the harness prompt carries the
        # behavioural rules GigaChat needs (anti-looping, two-step operations,
        # computed-not-guessed values).  Dropping either one breaks the run.
        from gigaforge.agents.harness_prompt import build_harness_prompt

        variant = os.environ.get("GIGAFORGE_HARNESS_PROFILE", "native_fs")
        try:
            harness = build_harness_prompt(
                variant, tool_names=sorted(self._registry.names())
            )
        except ValueError:
            log.warning("unknown GIGAFORGE_HARNESS_PROFILE=%r; using native_fs", variant)
            harness = build_harness_prompt("native_fs", tool_names=sorted(self._registry.names()))
        parts = [
            harness,
            _REACT_PROMPT,
            f"State: {json.dumps(prefix, ensure_ascii=False)}",
            f"Task: {task}",
            # The model was never told how many steps it had.  On a live run it
            # explored happily and hit ``max_steps`` with an empty plan every
            # time, because nothing in the prompt said "you are about to run
            # out \u2014 call finish".  A budget the model cannot see is a budget it
            # cannot plan around.
            f"Step budget: {self._max_steps} steps total. "
            "On the LAST step you must return {\"thought\": \"...\", \"action\": \"finish\", "
            "\"action_input\": \"[{\\\"tool\\\": \\\"edit_function\\\", \\\"args\\\": {...}}]\"} "
            "with whatever you have learned \u2014 an empty plan is a failed episode.",
        ]
        if hint:
            parts.append(f"Hint: {hint}")
        return "\n\n".join(parts)

    def _parse_step(self, text: str) -> tuple[str, str, str] | None:
        """Parse one ReAct step: (thought, action, action_input). None on failure."""
        repaired = json_repair(text)
        try:
            data = json.loads(repaired)
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict):
            return None
        thought = str(data.get("thought", "")).strip()
        action = str(data.get("action", "")).strip()
        if not action:
            return None
        ai = data.get("action_input", "")
        if isinstance(ai, (dict, list)):
            ai = json.dumps(ai, ensure_ascii=False)
        else:
            ai = str(ai or "").strip()
        return thought, action, ai

    def _parse_finish_plan(self, action_input: str) -> list[PlanStep]:
        """Parse the finish action's plan payload. Empty list on parse failure.

        Accepts either:
        - a JSON list of {tool,args} (preferred shape), or
        - a JSON object {"steps": [...]} (Planner-compatible shape).

        We do NOT reuse :func:`json_repair` here: that helper extracts the
        substring from the first ``{`` to the last ``}``, which would corrupt
        a top-level JSON list (the wrapping ``[ ]`` would be stripped).
        Instead we try plain ``json.loads`` first (the model should emit
        valid JSON since the prompt demands it); on failure we fall back to
        ``json_repair`` + ``json.loads`` as a softer recovery path.
        """
        if not action_input:
            return []
        # First try: the model emitted clean JSON.
        try:
            data = json.loads(action_input)
        except json.JSONDecodeError:
            # Soft fallback: peel off fences / extract brace-block.
            try:
                data = json.loads(json_repair(action_input))
            except json.JSONDecodeError:
                return []
        if isinstance(data, dict) and "steps" in data:
            data = data["steps"]
        if not isinstance(data, list):
            return []
        steps: list[PlanStep] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            tool = str(item.get("tool", "")).strip()
            if not tool:
                continue
            args = item.get("args", {})
            if not isinstance(args, dict):
                args = {"value": str(args)} if args else {}
            steps.append(PlanStep(tool=tool, args=args, done=bool(item.get("done", False))))
        return steps

    def _extract_localization(
        self, plan: list[PlanStep], last_thought: str
    ) -> dict[str, str]:
        """Best-effort: pull target_function from the first edit_function step,
        target_file from the first read_structure action_input seen in trajectory
        (or fall back to last_thought heuristic)."""
        loc: dict[str, str] = {}
        for step in plan:
            if step.tool == "edit_function" and "name" in step.args:
                loc["target_function"] = str(step.args["name"])
                break
        if not loc.get("target_function") and last_thought:
            # Fall back to extracting a `def NAME(` or bare NAME mention.
            import re

            m = re.search(r"\b([A-Za-z_][A-Za-z0-9_]*)\b", last_thought)
            if m:
                loc["target_function_guess"] = m.group(1)
        return loc


def _kind_for_tool(action: str) -> str:
    """Pick the right Firewall compressor kind for the tool's output."""
    if action == "run_tests":
        return "pytest"
    if action == "grep":
        return "grep"
    # read_structure + finish + unknown → builderr (general fallback).
    return "builderr"


def _env_max_steps() -> int:
    raw = os.environ.get("GIGAFORGE_REACT_MAX_STEPS", "")
    if not raw:
        return DEFAULT_REACT_MAX_STEPS
    try:
        n = int(raw)
        return max(1, min(n, 20))  # hard cap at 20 even if env is silly
    except ValueError:
        return DEFAULT_REACT_MAX_STEPS
