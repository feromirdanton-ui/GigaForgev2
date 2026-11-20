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

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from gigaforge.agents.fallback import (
    EXPLORE,
    FORCE_FINISH,
    ParseFailureFallback,
)
from gigaforge.agents.observation_cache import ObservationCache
from gigaforge.agents.planner import json_repair
from gigaforge.client.errors import BudgetExceeded
from gigaforge.client.giga_client import ChatResult, GigaClient
from gigaforge.config import Settings, get_settings
from gigaforge.core.budgets import BudgetTracker
from gigaforge.core.context_window_guard import ContextOverflowError
from gigaforge.core.loop_breaker import LoopBreaker
from gigaforge.core.router import ModelRouter
from gigaforge.core.task_state import PlanStep, TaskState
from gigaforge.core.trace import Tracer
from gigaforge.firewall import compress
from gigaforge.firewall.tokens import estimate_tokens
from gigaforge.l0.index import L0Index
from gigaforge.providers.messages import nudge

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

#: The action that runs several independent read-only tools at once.
#:
#: The model asks for it; the harness decides whether it is allowed.  A ReAct
#: episode spends most of its steps on exploration, and exploration is usually
#: a set of *independent* reads — grep for one symbol, read the skeleton of
#: another file, run the suite.  Doing them one per step costs one model round
#: trip each, which is the dominant cost of a run.
#:
#: The harness owns the safety question, not the model: only tools whose output
#: is a pure function of the tree may run in parallel, because two writes to the
#: same tree are not independent and their order is not defined.  A request that
#: includes a write is refused with an explanation rather than serialised
#: silently — the model should learn the rule, not be quietly overruled.
PARALLEL_ACTION = "parallel"

#: Upper bound on calls in one parallel batch.  Beyond this the observation
#: itself becomes the context problem the batch was meant to avoid.
MAX_PARALLEL_CALLS = 4

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

#: How many times a ``finish`` may be refused because the produced artifact
#: does not meet the goal's explicit requirements.  Two is enough for a model
#: to add the missing colours/sections; more turns a contract miss into a loop.
_MAX_CONTRACT_RETRIES = 2

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
    '- "write_file" — записать файл. action_input = JSON-строка вида\n'
    '  \'{"path":"index.html","content":"<!DOCTYPE html>..."}\'.\n'
    '  ЭТО ЕДИНСТВЕННЫЙ правильный способ создать файл: тело экранируется ОДИН раз.\n'
    '  Не вкладывай содержимое файла в план finish — там оно экранируется трижды и ломается.\n'
    '- "finish"    — завершить, подготовить план редактирования.\n'
    '  Для finish: action_input = JSON-строка вида\n'
    '  \'[{"tool":"edit_function","args":{"name":"<symbol>"}}]\'.\n'
    '- "parallel"  — выполнить НЕСКОЛЬКО независимых read-only вызовов за один шаг.\n'
    '  action_input = JSON-строка вида\n'
    '  \'[{"tool":"grep","args":"<regex>"},{"tool":"read_structure","args":"<path>"}]\'.\n'
    '  Разрешены только read-only инструменты (grep, read_structure); вызов с\n'
    '  инструментом, меняющим файлы, будет отклонён. Максимум 4 вызова.\n'
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
    #: Files the episode wrote itself via ``write_file``.  The pipeline turns
    #: these into a diff: the coder is asked for a diff, and a file the episode
    #: already produced is invisible to it otherwise.
    written_files: list[str] = field(default_factory=list)


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


class WriteFileTool:
    """Write a file into the worktree.

    **Why this tool exists.**  Without it the only way to produce a file was to
    put its whole body inside a plan step inside the ``finish`` payload — a JSON
    string inside a JSON string inside a JSON string.  A live run asking for a
    cyberpunk page produced 1.4 KB of HTML that had to survive three levels of
    escaping; it did not, the payload failed to parse four times in a row, the
    episode hit ``max_steps`` with an empty plan, and the run fell back to a
    one-shot linear answer.  The model was not wrong — the protocol was.

    With this tool the body is escaped exactly once, and the harness does the
    writing.  ``action_input`` is ``{"path": "...", "content": "..."}``; a bare
    string is accepted as the content with a path the model must supply, which
    is reported as an error rather than guessed.
    """

    name = "write_file"

    async def run(self, action_input: str, *, ctx: ReActContext) -> str:
        try:
            data = json.loads(action_input)
        except json.JSONDecodeError:
            try:
                data = json.loads(json_repair(action_input))
            except json.JSONDecodeError:
                return (
                    "ERROR: write_file needs JSON {\"path\": \"...\", \"content\": \"...\"}. "
                    "Escape the content once, as a normal JSON string."
                )
        if not isinstance(data, dict):
            return "ERROR: write_file needs a JSON object with 'path' and 'content'"
        rel = str(data.get("path") or data.get("file") or data.get("location") or "").strip()
        if not rel:
            return "ERROR: write_file needs a 'path'"
        content = data.get("content")
        if content is None:
            return "ERROR: write_file needs 'content'"
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False, indent=2)
        target = (ctx.worktree / rel).resolve()
        root = ctx.worktree.resolve()
        # A path that escapes the worktree is a bug or an attack, never a task.
        if root not in target.parents and target != root:
            return f"ERROR: refusing to write outside the worktree: {rel}"
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            existed = target.exists()
            target.write_text(content, encoding="utf-8")
        except OSError as exc:
            return f"ERROR: write_file failed: {exc}"
        verb = "updated" if existed else "created"
        return f"{verb} {rel} ({len(content)} bytes, {content.count(chr(10)) + 1} lines)"


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
        """Install the built-in grep/read_structure/run_tests/repl tools."""
        self._sandbox = sandbox
        self.register(GrepTool())
        self.register(ReadStructureTool())
        self.register(RunTestsTool())
        # Writing is a first-class harness operation, not something the model
        # smuggles through a plan payload.  See WriteFileTool's docstring.
        self.register(WriteFileTool())
        # The REPL is the harness's own execution surface: it reuses the
        # sandbox executor (allowlist, worktree cwd, timeout) and the Firewall,
        # so a probe costs one bounded observation instead of a new dependency.
        from gigaforge.agents.repl_tool import ReplTool

        self.register(ReplTool())
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
        window_guard: Any | None = None,
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
        #:
        #: Injectable because the compaction path is otherwise unreachable in a
        #: test: the real window is 261 120 tokens, so forcing a compaction
        #: would need a trajectory of a quarter of a million tokens.  A caller
        #: that passes a guard with a small ``context_window`` exercises the
        #: exact same code with a trajectory it can afford to build.
        self._window_guard: Any = window_guard
        #: Compactions performed in the current episode.  Bounded so a
        #: pathological trajectory cannot oscillate between compact and
        #: overflow; reset at the start of every ``run``.
        self._compactions = 0
        #: Consecutive compactions with no productive step in between.  This is
        #: the number the bound applies to: a long run may compact many times as
        #: long as it keeps making progress between compactions.
        self._compaction_streak = 0

    @property
    def registry(self) -> ToolRegistry:
        return self._registry

    # -- public collaborators -------------------------------------------------
    # The LangGraph engine drives this runner from outside, so it needs the
    # runner's collaborators.  Reaching for ``_router`` / ``_budgets`` /
    # ``_tracer`` / ``_client`` with a private-access suppression made every
    # refactor of this class a silent breakage of the graph; these accessors are
    # the supported seam.
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

        The compaction is bounded: at most ``_MAX_COMPACTIONS`` *consecutive*
        compactions that fail to buy room, so a pathological trajectory cannot
        spin between "compact" and "overflow" forever.  Each compaction is
        traced, because a run that silently rewrote its own history is a run
        whose token accounting needs a note.

        The bound used to be on the *total* number of compactions per episode,
        which is a different and wrong claim.  A long run legitimately needs
        many compactions: it grows, compacts, grows again, compacts again.  A
        total cap of two therefore killed every genuinely long episode at its
        third compaction — measured: a 10-step run compacted twice (3 635 →
        1 623 tokens, then 4 086 → 1 653) and then died with
        ``context_overflow`` while it was still making progress.  What must be
        bounded is the *streak*: compaction after compaction with no productive
        step in between means the goal itself does not fit, and that is the
        case worth stopping.
        """
        from gigaforge.harness.compaction import compact_trajectory, trim_stale_file_outputs

        if self._compaction_streak >= _MAX_COMPACTIONS:
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
        self._compaction_streak += 1
        self._tracer.trace(
            state.task_id,
            step=f"react_step_{step_idx}_compaction",
            kind="decision",
            detail={
                "step_idx": step_idx,
                "before_tokens": before,
                "after_tokens": after,
                "dropped_messages": len(trajectory) - len(compacted),
                "compactions_total": self._compactions,
                "compaction_streak": self._compaction_streak,
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
        self._compaction_streak = 0
        # The think tool is a real tool, not a prompt trick: registering it is
        # what lets the model externalise a reasoning step.
        self.guards.install(self._registry)

        result = ReActResult()
        trajectory: list[dict[str, str]] = []  # role-tagged messages for the LLM
        base_user = self._compose_initial_prompt(task, state, bootstrap_hint)
        trajectory.append({"role": "user", "content": base_user})
        # ToolContract: tell the model which tools exist, exactly once.
        trajectory.extend(self.guards.opening_messages(trajectory))
        # LoopBreaker (ported from deepagents-gigachat): the classic loop had
        # ``max_steps`` and a compaction cap but no detector for the model
        # repeating itself, so an episode that grepped the same pattern three
        # times burned its whole step budget.  The breaker is pure and bounded
        # (one nudge per signature per run), so it is safe to consult on every
        # observation.
        breaker = LoopBreaker()
        # Deterministic fallback for a model that will not emit the JSON step
        # envelope.  The nudge alone was the only response to a parse failure,
        # so a model answering in prose burned the whole step budget on reformat
        # requests.  After the first failure the harness takes over: a
        # deterministic grep, then a forced finish.
        fallback = ParseFailureFallback()
        parse_failures = 0
        # How many times a finish has been refused because the artifact did not
        # meet the goal's explicit requirements.  Bounded so a model that cannot
        # satisfy the contract does not loop until the step budget runs out.
        contract_retries = 0
        # Observation cache: a ReAct episode re-reads the same file constantly,
        # and each repeat costs a tool call plus a full observation in the
        # trajectory — which is what pushes a run into compaction.  Only
        # read-only tools are cached, and any change to the tree drops the whole
        # cache, so a write is always observed.
        #
        # The root used to be read from ``self.ctx``, which does not exist on
        # this class — so it was always ``None``, ``_workspace_stamp()`` always
        # returned ``""``, and the cache NEVER invalidated.  A cached grep
        # therefore survived a write to the tree: the model edited a file,
        # searched again, and was handed the pre-edit answer.  The context is a
        # parameter of ``run``, so that is where the root comes from.
        self._obs_cache = ObservationCache(
            root=getattr(ctx, "worktree", None) or getattr(ctx, "repo_root", None)
        )

        for step_idx in range(self._max_steps):
            compacted_this_step = False
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
                    return self._carry_trajectory(result, trajectory)
                trajectory = compacted
                compacted_this_step = True

            # Pre-budget check. Phase = "react" (its own cap, not the planner's).
            est_in = sum(len(m["content"]) for m in trajectory) // 4
            est_out = max(self._react_out() // 3, 200)
            self._budgets.check(_REACT_PHASE, est_in, est_out, task_id=state.task_id)

            # Reason — one LLM call.
            started = time.monotonic()
            chat_result: ChatResult = await self._client.chat(
                self._router.route(_REACT_PHASE),
                trajectory,
                temperature=0.1,
                max_tokens=self._react_out(),
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
                # The exception still propagates — the budget is a hard stop —
                # but the trajectory goes with it, so the caller can salvage.
                self._carry_trajectory(result, trajectory)
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
                # Repair nudge — but bounded, and with a deterministic fallback.
                #
                # The nudge alone was the *only* response to a parse failure, so
                # a model that kept answering in prose burned the whole step
                # budget on reformat requests and the episode ended with
                # nothing.  Measured live: the model located the files, then
                # spent its remaining steps failing to wrap the answer in JSON.
                # After the first failure the harness takes over the part the
                # model is failing at (see agents/fallback.py).
                parse_failures += 1
                decision = fallback.next_action(task, parse_failures)
                trajectory.append({"role": "assistant", "content": text})
                result.steps.append(
                    {
                        "step_idx": step_idx,
                        "raw": text,
                        "ok": False,
                        "error": "parse_failed",
                        "fallback": decision.kind,
                    }
                )
                self._tracer.trace(
                    state.task_id,
                    step=f"react_step_{step_idx}_parse_failed",
                    kind="decision",
                    detail={
                        "step_idx": step_idx,
                        "consecutive": parse_failures,
                        "fallback": decision.kind,
                        "note": decision.note,
                    },
                )
                if decision.kind == FORCE_FINISH:
                    # Three failures is not a formatting accident.  Stop
                    # exploring and turn what was observed into a plan.
                    break
                if decision.kind == EXPLORE:
                    # Run the deterministic search ourselves and feed the result
                    # back as an observation, so the episode still produces
                    # something a salvage can use.
                    tool = self._registry.get(decision.action)
                    if tool is not None:
                        try:
                            raw_obs = await tool.run(decision.action_input, ctx=ctx)
                        except Exception as exc:  # noqa: BLE001 - best-effort
                            raw_obs = f"ERROR: fallback {decision.action!r} raised: {exc}"
                        kind = _kind_for_tool(decision.action)
                        raw_obs = self.guards.normalize_observation(decision.action, raw_obs)
                        observation = compress(
                            kind, raw_obs, limit_tokens=_OBS_TOKEN_BUDGET
                        ).text
                        trajectory.append(
                            {
                                "role": "user",
                                "content": (
                                    f"[FALLBACK] {decision.note}\n"
                                    f"Observation ({kind}):\n{observation}"
                                ),
                            }
                        )
                        continue
                trajectory.append(
                    {
                        "role": "user",
                        "content": (
                            "Твой прошлый ответ не является JSON вида "
                            '{thought,action,action_input}. Верни ТОЛЬКО JSON.'
                        ),
                    }
                )
                continue

            thought, action, action_input = parsed
            # A good step resets the failure streak: the fallback is for a model
            # that has stopped answering in this shape, not for one that
            # stumbled once.
            parse_failures = 0
            # NOTE: the step is recorded AFTER it runs, not here.  This used to
            # append ``ok: True`` before the tool was even looked up, so the run
            # record claimed success for calls that were blocked, unknown or
            # raised — and then a second append after execution produced a
            # duplicate entry for every call that worked.

            # Termination.
            if action == FINISH_ACTION:
                # Before accepting the finish, check the artifact against the
                # goal's *explicit* requirements.  A live run asked for a
                # "neon cyan/magenta palette" and produced a page whose whole
                # palette was #fff on #000; the model called finish, the plan
                # was empty, and the run reported success.  Nothing had ever
                # looked at the file.  "Does the page contain the colours the
                # user named?" is a string search, so the harness does it.
                contract = self._check_contract(ctx, state, result)
                if not contract.ok and contract_retries < _MAX_CONTRACT_RETRIES:
                    contract_retries += 1
                    trajectory.append({"role": "assistant", "content": text})
                    trajectory.append(
                        {"role": "user", "content": contract.as_observation()}
                    )
                    result.steps.append(
                        {
                            "step_idx": step_idx,
                            "thought": thought,
                            "action": action,
                            "action_input": action_input,
                            "ok": False,
                            "observation": contract.as_observation(),
                            "plan_size": 0,
                            "contract_unmet": list(contract.unmet),
                        }
                    )
                    self._tracer.trace(
                        state.task_id,
                        step=f"react_contract_{step_idx}",
                        kind="decision",
                        detail={
                            "unmet": contract.unmet,
                            "checked": contract.checked,
                            "retry": contract_retries,
                        },
                    )
                    continue
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
                # The finish is a step too: a run record that omits it cannot
                # say how the episode ended.
                result.steps.append(
                    {
                        "step_idx": step_idx,
                        "thought": thought,
                        "action": action,
                        "action_input": action_input,
                        "ok": True,
                        "observation": "",
                        "plan_size": len(plan),
                    }
                )
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
                return self._carry_trajectory(result, trajectory)

            # Act — invoke the tool.
            #
            # ``parallel`` is handled before the single-tool path: it is the
            # harness's own action, not a registered tool, and the model asks
            # for it by name.  A refused batch becomes an observation, so the
            # model learns the rule instead of the harness silently serialising
            # work it was told to run concurrently.
            if action == PARALLEL_ACTION:
                batch = self._parse_parallel_batch(action_input)
                records, refusal = await self._run_parallel(
                    batch, ctx=ctx, state=state, step_idx=step_idx
                )
                trajectory.append({"role": "assistant", "content": text})
                if refusal:
                    trajectory.append(
                        {
                            "role": "user",
                            "content": f"ERROR: [PARALLEL] {refusal}",
                        }
                    )
                    result.steps.append(
                        {
                            "step_idx": step_idx,
                            "thought": thought,
                            "action": action,
                            "action_input": action_input,
                            "ok": False,
                            "error": refusal,
                        }
                    )
                    continue
                lines = [
                    f"Observation ({r['kind']}) [{r['tool']}]:\n{r['observation']}"
                    for r in records
                ]
                trajectory.append(
                    {
                        "role": "user",
                        "content": (
                            f"Observation (parallel, {len(records)} calls):\n\n"
                            + "\n\n".join(lines)
                        ),
                    }
                )
                result.steps.append(
                    {
                        "step_idx": step_idx,
                        "thought": thought,
                        "action": action,
                        "action_input": action_input,
                        "ok": True,
                        "observation": "\n\n".join(lines),
                        "parallel_calls": [r["tool"] for r in records],
                    }
                )
                self._compaction_streak = 0
                continue

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
                observation = compressed.text
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
                cached = self._obs_cache.get(action, action_input)
                if cached is not None:
                    raw_obs = cached
                    self._tracer.trace(
                        state.task_id,
                        step=f"react_step_{step_idx}_cache_hit",
                        kind="decision",
                        detail={"tool": action},
                    )
                else:
                    raw_obs = await tool.run(action_input, ctx=ctx)
                    self._obs_cache.put(action, action_input, raw_obs)
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
            if _is_error_observation(raw_obs):
                # An error is a message, not a result: the compressors would
                # turn it into an empty result set and the model would read the
                # opposite of the truth.
                observation = raw_obs
            else:
                compressed = compress(kind, raw_obs, limit_tokens=_OBS_TOKEN_BUDGET)
                observation = compressed.text
                # Cap the observation text (belt-and-braces).
                if estimate_tokens(observation) > _OBS_TOKEN_BUDGET:
                    observation = observation[: _OBS_TOKEN_BUDGET * 4] + "\n[truncated]"

            trajectory.append({"role": "assistant", "content": text})
            # A successful write is the episode's real output.  Record the path
            # so the pipeline can compute its diff: the coder is asked for a
            # diff, and a file that already exists on disk is invisible to it.
            if action == "write_file" and not str(raw_obs).startswith("ERROR"):
                try:
                    _payload = json.loads(action_input)
                    _rel = str(
                        _payload.get("path") or _payload.get("file")
                        or _payload.get("location") or ""
                    ).strip()
                except (json.JSONDecodeError, AttributeError):
                    _rel = ""
                if _rel and _rel not in result.written_files:
                    result.written_files.append(_rel)
            if cached is not None:
                # A cache hit means this exact observation is ALREADY earlier in
                # the trajectory.  Appending it again would pay for the same
                # tokens twice on every subsequent request — the cache would
                # save a tool call and cost the budget it was meant to protect.
                # A pointer is enough: the model can read the earlier step.
                trajectory.append(
                    {
                        "role": "user",
                        "content": (
                            f"Observation ({kind}): identical to an earlier step in "
                            "this episode — see that observation; it has not changed."
                        ),
                    }
                )
            else:
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
            # Record the step.  The successful path never appended to
            # ``result.steps`` — only the blocked path did — so the run record
            # omitted every tool call that actually worked.  A live probe read
            # ``observation: None`` for a REPL step whose program had printed
            # the right answer, and any report built from ``result.steps``
            # described a run that never touched a tool.
            result.steps.append(
                {
                    "step_idx": step_idx,
                    "thought": thought,
                    "action": action,
                    "action_input": action_input,
                    "ok": True,
                    "observation": observation,
                    "cached": cached is not None,
                }
            )
            # A productive step resets the compaction streak: the bound is on
            # compaction-after-compaction with no progress, not on the total.
            # Without this reset the streak would only ever grow, and the bound
            # would be the old total cap under a new name.
            #
            # The reset is conditional on the step NOT having compacted.  A
            # step that had to compact and then immediately overflowed again is
            # not progress in the sense that matters: the trajectory is still
            # over the window, and resetting here would let a tiny window
            # compact on every single step forever (measured: 17 compactions in
            # a 20-step run).  Progress means a step that fit without help.
            if not compacted_this_step:
                self._compaction_streak = 0
            # LoopBreaker: the same three-signature detector the LangGraph
            # ``observe`` node runs.  Without it the classic loop — which is the
            # engine the product actually drives — had no defence against a
            # model repeating a broken call until the step budget ran out.
            signal = breaker.inspect(trajectory)
            if signal is not None:
                trajectory.append(nudge(signal.text, marker=signal.marker))
                self._tracer.trace(
                    state.task_id,
                    step=f"react_step_{step_idx}_loop_breaker",
                    kind="decision",
                    detail={"kind": signal.kind, **dict(signal.detail or {})},
                )

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
            return self._carry_trajectory(result, trajectory)

        result.finish_reason = "max_steps"
        self._tracer.trace(
            state.task_id,
            step="react_max_steps_reached",
            kind="decision",
            detail={
                "max_steps": self._max_steps,
                "llm_calls": result.llm_calls,
                # A run that degraded must say so: an episode that spent its
                # steps failing to emit JSON is not the same as one that
                # explored and ran out of room, and a report that cannot tell
                # them apart cannot tell a model problem from a harness one.
                "parse_failures": parse_failures,
                "fallback": fallback.describe(),
            },
        )
        return self._carry_trajectory(result, trajectory)

    def _react_out(self) -> int:
        """Output-token ceiling for one ReAct step.

        This used to be ``budget_plan_out`` (1000), while ``gigaforge stack``
        advertised ``budget_react_out`` (8000) — the phase had its own budget
        that nothing read.  A finish payload carrying a 4–6 step plan does not
        fit in 1000 tokens, which is part of why live runs came back with
        ``parse_failed``.  The react phase now uses its own number, and the
        stack command reads the same setting.
        """
        return int(getattr(self._s, "budget_react_out", 0) or self._s.budget_plan_out)

    @staticmethod
    def _carry_trajectory(result: ReActResult, trajectory: list[dict[str, str]]) -> ReActResult:
        """Attach the episode's trajectory to *result* before it is returned.

        Every return path goes through here.  The trajectory used to be carried
        out only on the ``max_steps`` branch, so the salvage in
        ``ReActPlan._guarded_steps`` — written for exactly the case where the
        model finishes with an unusable plan — never saw a trajectory on the
        ``finish_action`` path, which is the path that case takes.  A recovery
        that only works on the branch it was not written for is not a recovery.
        """
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
                    '"[{\\"tool\\": \\"read_structure\\", \\"args\\": {\\"path\\": \\"<file>\\"}}]"}\n'
                    "Prefer read steps: a read step is always true of what you "
                    "observed, while an edit step is a claim you did not verify. "
                    "If the observations are inconclusive, return an empty list "
                    "\u2014 that is an honest result, not a failure."
                ),
            }
        )
        try:
            chat_result: ChatResult = await self._client.chat(
                self._router.route(_REACT_PHASE),
                convo,
                temperature=0.0,
                max_tokens=self._react_out(),
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

    def _check_contract(self, ctx: ReActContext, state: TaskState, result: ReActResult):
        """Check the produced artifact against the goal's explicit requirements.

        Returns a :class:`~gigaforge.harness.goal_contract.ContractVerdict`.
        A goal with no checkable requirement yields an ``ok`` verdict, so this
        never invents a standard the user did not set.
        """
        from gigaforge.harness.goal_contract import check_artifacts, extract_requirements

        goal = getattr(state, "goal", "") or ""
        requirements = extract_requirements(goal)
        if not requirements:
            from gigaforge.harness.goal_contract import ContractVerdict

            return ContractVerdict()
        written = list(getattr(result, "written_files", None) or [])
        # ``files=written`` — NOT ``written or None``.  ``None`` used to mean
        # "scan the whole worktree", so a run that wrote nothing passed the
        # contract on a file that was already in the repository.  An empty list
        # now means what it says: no artifact was produced.
        return check_artifacts(requirements, ctx.worktree, files=written)

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

    def _parse_parallel_batch(self, action_input: str) -> list[tuple[str, str]]:
        """Parse a ``parallel`` payload into ``[(tool, action_input), ...]``.

        Accepts the same shapes the finish payload accepts, because the model
        has already learned them: a JSON list of ``{"tool","args"}`` objects, or
        an object with a ``calls``/``steps`` key.  Returns an empty list when
        nothing usable is present, which the caller reports as a parse failure
        rather than silently running nothing.
        """
        if not action_input:
            return []
        try:
            data = json.loads(action_input)
        except json.JSONDecodeError:
            try:
                data = json.loads(json_repair(action_input))
            except json.JSONDecodeError:
                return []
        if isinstance(data, dict):
            for key in ("calls", "steps", "actions"):
                if key in data:
                    data = data[key]
                    break
        if not isinstance(data, list):
            return []
        calls: list[tuple[str, str]] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            tool = str(item.get("tool") or item.get("action") or "").strip()
            if not tool:
                continue
            raw = item.get("args", item.get("action_input", ""))
            if isinstance(raw, (dict, list)):
                raw = json.dumps(raw, ensure_ascii=False)
            else:
                raw = str(raw or "").strip()
            calls.append((tool, raw))
        return calls

    async def _run_parallel(
        self,
        calls: list[tuple[str, str]],
        *,
        ctx: ReActContext,
        state: TaskState,
        step_idx: int,
    ) -> tuple[list[dict[str, Any]], str]:
        """Run a batch of independent read-only calls concurrently.

        Returns ``(records, refusal)``.  ``refusal`` is non-empty when the batch
        was rejected as a whole, in which case nothing ran.

        The safety rule is the harness's, and it is checked *before* anything
        executes: a batch containing a tool that can write is refused outright.
        Serialising it instead would be worse than refusing — the model would
        believe it had asked for parallel work and get sequential work, and the
        next batch would make the same mistake.
        """
        from gigaforge.agents.observation_cache import CACHEABLE_TOOLS

        if not calls:
            return [], "the parallel payload contained no usable calls"
        if len(calls) > MAX_PARALLEL_CALLS:
            return [], (
                f"too many calls in one batch ({len(calls)} > {MAX_PARALLEL_CALLS}); "
                "split the work across steps"
            )
        unknown = [name for name, _ in calls if self._registry.get(name) is None]
        if unknown:
            return [], (
                f"unknown tool(s) {unknown}; available: {self._registry.names()} + 'finish'"
            )
        unsafe = [name for name, _ in calls if name not in CACHEABLE_TOOLS]
        if unsafe:
            return [], (
                f"tool(s) {unsafe} can change the tree and cannot run in parallel; "
                "call them one per step"
            )

        async def _one(name: str, payload: str) -> dict[str, Any]:
            tool = self._registry.get(name)
            assert tool is not None  # checked above
            try:
                cached = self._obs_cache.get(name, payload)
                if cached is not None:
                    return {"tool": name, "action_input": payload, "raw": cached, "cached": True}
                raw = await tool.run(payload, ctx=ctx)
                self._obs_cache.put(name, payload, raw)
                return {"tool": name, "action_input": payload, "raw": raw, "cached": False}
            except Exception as exc:  # noqa: BLE001 - one failure must not sink the batch
                return {
                    "tool": name,
                    "action_input": payload,
                    "raw": f"ERROR: tool {name!r} raised: {exc}",
                    "cached": False,
                }

        gathered = await asyncio.gather(*(_one(name, payload) for name, payload in calls))
        records: list[dict[str, Any]] = []
        for item in gathered:
            kind = _kind_for_tool(item["tool"])
            raw = self.guards.normalize_observation(item["tool"], item["raw"])
            if _is_error_observation(raw):
                observation = raw
            else:
                observation = compress(kind, raw, limit_tokens=_OBS_TOKEN_BUDGET).text
            records.append({**item, "kind": kind, "observation": observation})
        self._tracer.trace(
            state.task_id,
            step=f"react_step_{step_idx}_parallel",
            kind="decision",
            detail={
                "step_idx": step_idx,
                "calls": [name for name, _ in calls],
                "cached": [r["cached"] for r in records],
            },
        )
        return records, ""

    def _parse_step(self, text: str) -> tuple[str, str, str] | None:
        """Parse one ReAct step: (thought, action, action_input). None on failure."""
        # The model frequently emits *two* objects in one answer: the step it
        # wants to take, then a ``finish`` it has already decided on.  Measured
        # live on every run of the cyberpunk-site goal — the answer was
        # ``{"action":"write_file",...}\n{"action":"finish",...}``.  Parsing the
        # whole text fails ("Extra data"), so the step was counted as a parse
        # failure, the fallback force-finished, and the file was never written.
        # The first object is the step; the rest is the model thinking ahead.
        first = self._first_json_object(text)
        repaired = json_repair(first if first is not None else text)
        try:
            data = json.loads(repaired)
        except json.JSONDecodeError:
            # The model can produce a step whose *outer* JSON is broken by the
            # content it is carrying.  Measured live, four times in a row: a
            # ``write_file`` step whose ``action_input`` held 1.4 KB of HTML
            # with quotes and newlines.  The outer object failed to parse, the
            # episode counted a parse failure, and after three of them the
            # fallback force-finished with nothing written.
            #
            # The shape is still recoverable: the action name and the path are
            # short and unambiguous, and the content is "everything after the
            # content key".  Recovering it is a computation, so the harness
            # does it rather than asking the model to escape better.
            recovered = self._recover_step(text)
            if recovered is None:
                return None
            return recovered
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

    @staticmethod
    def _first_json_object(text: str) -> str | None:
        """Return the first balanced ``{...}`` block, or ``None``.

        Brace counting is string-aware: a ``{`` inside a quoted value (HTML,
        CSS, a regex) must not open a block, and a ``}`` inside one must not
        close it.  Without that, a step carrying a page of CSS would be cut at
        the first ``}`` in its own stylesheet.
        """
        start = text.find("{")
        if start < 0:
            return None
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return text[start:index + 1]
        return None

    #: Keys whose value is a long free-form body, in the order they are tried.
    _BODY_KEYS = ("content", "body", "text", "code")

    def _recover_step(self, text: str) -> tuple[str, str, str] | None:
        """Recover a step whose outer JSON is broken by its own payload.

        Only the shapes the harness itself asks for are recovered — a named
        action plus a ``path``/``content`` pair.  Anything else stays a parse
        failure, so this cannot turn arbitrary prose into a tool call.
        """
        action_match = re.search(r'"action"\s*:\s*"([A-Za-z_][A-Za-z0-9_]*)"', text)
        if not action_match:
            return None
        action = action_match.group(1)
        thought_match = re.search(r'"thought"\s*:\s*"((?:[^"\\]|\\.)*)"', text)
        thought = ""
        if thought_match:
            try:
                thought = json.loads(f'"{thought_match.group(1)}"')
            except json.JSONDecodeError:
                thought = thought_match.group(1)

        if action != "write_file":
            return None

        path_match = re.search(
            r'"(?:path|file|location)"\s*:\s*"((?:[^"\\]|\\.)*)"', text
        )
        if not path_match:
            return None
        try:
            path = json.loads(f'"{path_match.group(1)}"')
        except json.JSONDecodeError:
            path = path_match.group(1)

        body_key = next(
            (k for k in self._BODY_KEYS if f'"{k}"' in text[path_match.end():]),
            None,
        )
        if body_key is None:
            return None
        body_start = text.index(f'"{body_key}"', path_match.end())
        colon = text.index(":", body_start)
        quote = text.index('"', colon)
        raw_body = text[quote + 1:]
        # The body ends at the last quote that is followed only by closing
        # punctuation — the model's own trailing ``"}`` / ``"}]}``.  Everything
        # before it is the body, *including* the unescaped quotes that broke the
        # outer object.  Trying progressively shorter prefixes instead (the
        # first version of this) stops at the first inner quote and silently
        # truncates the file to its first line.
        end = len(raw_body)
        while end > 0:
            tail = raw_body[end - 1:]
            if tail.startswith('"') and not tail[1:].strip(" \t\r\n}]"):
                break
            end -= 1
        if end == 0:
            return None
        body_raw = raw_body[: end - 1]
        # The body is raw text, not a JSON string: it carries unescaped quotes
        # *and* literal newlines, both of which are illegal inside a JSON string
        # literal.  Escape both, then decode once.  (Escaping only the quotes
        # still failed with "Invalid control character" on the first newline.)
        escaped = re.sub(r'(?<!\\)"', r'\\"', body_raw)
        escaped = escaped.replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\n")
        escaped = escaped.replace("\t", "\\t")
        try:
            body = json.loads(f'"{escaped}"')
        except json.JSONDecodeError:
            return None
        payload = json.dumps({"path": path, body_key: body}, ensure_ascii=False)
        return thought, action, payload

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
        # A double-encoded payload: the model put the plan in as a *string*
        # inside the string.  ``_parse_step`` already unwraps one level, but a
        # model that emits ``"action_input": "{\"steps\": ...}"`` twice is
        # common enough to be worth one more attempt.
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except json.JSONDecodeError:
                return []
        if isinstance(data, dict) and "steps" in data:
            data = data["steps"]
        # ``json_repair`` extracts from the first ``{`` to the last ``}``, so a
        # *list* payload that needed repair comes back as a bare object: the
        # wrapping ``[ ]`` is gone.  Measured live — the model returned a
        # one-step plan for "build a cyberpunk site", the repair collapsed it to
        # a dict, this function returned ``[]``, and the episode's whole plan
        # was discarded while the run still reported ``finish_action``.  A dict
        # that names a tool *is* a one-step plan; accept it as such.
        if isinstance(data, dict) and str(data.get("tool", "")).strip():
            data = [data]
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
            # Fall back to a *code-shaped* mention only.  The previous regex took
            # the first word of the thought, so a live run reported
            # ``target_function_guess: "The"`` — the first word of an English
            # sentence.  A guess that is obviously not an identifier is worse
            # than no guess: it is rendered into the coder's prompt as a target.
            import re

            m = re.search(r"\bdef\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", last_thought)
            if not m:
                m = re.search(r"`([A-Za-z_][A-Za-z0-9_]*)`", last_thought)
            if m and m.group(1) not in _STOPWORDS:
                loc["target_function_guess"] = m.group(1)
        return loc


#: Words that are never a symbol name, however identifier-shaped they look.
#: Used to reject a localisation guess taken from prose.
_STOPWORDS = frozenset(
    {
        "the", "this", "that", "there", "then", "these", "those", "and", "but",
        "for", "with", "from", "into", "when", "where", "which", "while", "will",
        "should", "must", "need", "needs", "have", "has", "was", "were", "are",
        "is", "it", "its", "a", "an", "to", "of", "in", "on", "at", "by", "as",
        "now", "next", "first", "last", "done", "ok", "okay", "yes", "no",
    }
)


def _is_error_observation(raw: str) -> bool:
    """True when a tool's output is a failure report rather than a result.

    The Firewall's compressors are written for *results*: the grep compressor
    turns any text into a ``{"matches": [...]}`` envelope, so a tool that raised
    ``RuntimeError("tool exploded")`` reached the model as ``{"matches":[]}`` —
    an empty result set, which reads as "the symbol is not in the tree".  The
    model then concluded the opposite of the truth, and the harness had no way
    to tell the two apart because the error text was already gone.

    An error is not a result to be summarised; it is a message to be delivered
    verbatim.  It is also short, so there is nothing to compress.
    """
    return raw.lstrip().startswith("ERROR:")


def _kind_for_tool(action: str) -> str:
    """Pick the right Firewall compressor kind for the tool's output."""
    if action == "run_tests":
        return "pytest"
    if action == "grep":
        return "grep"
    # read_structure + repl + finish + unknown → builderr (general fallback).
    # A REPL step's output is a traceback or a print, which is exactly what the
    # builderr compressor is built to bound.
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
