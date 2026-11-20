"""ReActGuards — the wiring that makes the ported middleware actually run.

The merge plan ported six components from ``ai-forever/deepagents-gigachat``.
Two of them (``ContextWindowGuard``, ``LoopBreaker``) were wired into the ReAct
loop; the other four — ``ShellSafety``, ``ToolContract``, ``SpecificationAudit``
and ``ThinkTool`` — were ported, unit-tested and then never referenced by any
production path.  That is dead code: 500+ lines that cannot change behaviour.

This module is the single place that connects them, so both ReAct engines (the
hand-written loop in :mod:`gigaforge.agents.react` and the LangGraph graph in
:mod:`gigaforge.agents.langgraph_react`) get identical guard behaviour:

* **ToolContract** — the tool contract is injected once, as a human message, so
  the model is told which tools exist instead of inventing flags.
* **ShellSafety** — every shell-shaped tool call is screened before it runs; an
  unsafe command becomes an observation, not an execution.
* **SpecificationAudit** — after the model reads a file it just created, it is
  nudged once to compare the output against the user's contract.
* **ThinkTool** — registered as a real tool, so the model can externalise a
  reasoning step without spending a tool round on the filesystem.

Everything here is pure and side-effect free apart from the registry mutation in
:meth:`ReActGuards.install`, which is deliberate and idempotent.
"""
from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from gigaforge.agents.output_guards import DeterministicOutput, PathNormalizer
from gigaforge.agents.think_tool import THINK_TOOL_NAME, ThinkTool
from gigaforge.core.specification_audit import SpecificationAudit
from gigaforge.core.tool_contract import ToolContract
from gigaforge.sandbox.shell_safety import ShellSafety

__all__ = ["ReActGuards", "ThinkToolAdapter"]


class ThinkToolAdapter:
    """Adapt :class:`ThinkTool` to the ReAct :class:`Tool` protocol.

    ``ThinkTool`` is a plain callable with an ``invoke(args)`` entry point; the
    ReAct registry wants an async ``run(action_input, *, ctx)``.  The adapter is
    the two-line bridge, and it keeps the tool's own schema and log intact.
    """

    def __init__(self, tool: ThinkTool | None = None) -> None:
        self._tool = tool or ThinkTool()

    @property
    def name(self) -> str:
        return THINK_TOOL_NAME

    @property
    def tool(self) -> ThinkTool:
        return self._tool

    async def run(self, action_input: str, *, ctx: Any = None) -> str:
        """Record the thought and echo a short acknowledgement.

        The model gets a confirmation rather than its own text back: repeating
        the thought would double the tokens for no information gain.
        """
        self._tool(thought=str(action_input or ""))
        return "Thought recorded."


class ReActGuards:
    """The four ported guards, wired for one ReAct run.

    ``workspace`` is the directory whose *initial* file set defines "new file"
    for the specification audit; pass ``None`` to disable that check (the audit
    then never fires, which is the honest behaviour when there is no workspace).
    """

    def __init__(
        self,
        *,
        tool_names: Sequence[str] = (),
        workspace: Path | str | None = None,
        shell_tool_names: tuple[str, ...] = ("execute", "shell", "bash", "run_command"),
        think: ThinkTool | None = None,
    ) -> None:
        self.shell_safety = ShellSafety(tool_names=shell_tool_names)
        self.contract = ToolContract.for_tools(list(tool_names))
        self.audit = SpecificationAudit.for_workspace(workspace)
        self.think = ThinkToolAdapter(think)
        # Ported from deepagents-gigachat: paths in observations are made
        # workspace-relative, and a plan may not assert a computed value it
        # never computed.
        self.paths = PathNormalizer()
        self.deterministic = DeterministicOutput()

    # -- installation --------------------------------------------------------
    def install(self, registry: Any) -> None:
        """Register the think tool on *registry* (idempotent)."""
        if registry is None:
            return
        if registry.get(self.think.name) is None:
            registry.register(self.think)

    # -- per-turn hooks ------------------------------------------------------
    def opening_messages(self, messages: Sequence[Any]) -> list[dict[str, Any]]:
        """Messages to append before the first model call.

        Today that is the tool contract, injected exactly once.  Returning a
        list (rather than a single message) keeps room for a future guard
        without changing every call site.
        """
        contract = self.contract.maybe_inject(messages)
        return [contract] if contract is not None else []

    def screen_tool_call(self, action: str, action_input: Any) -> str | None:
        """Return an error observation when *action* must not run, else ``None``.

        The verdict is rendered as the same ``ERROR:`` shape a failing tool
        produces, so the LoopBreaker's error-family detection sees it and the
        model can correct course.
        """
        verdict = self.shell_safety.check_tool_call(action, action_input)
        if verdict.safe:
            return None
        return f"ERROR: [SHELL-SAFETY] {verdict.message()}"

    def after_observation(self, messages: Sequence[Any]) -> dict[str, Any] | None:
        """The specification-audit nudge, when the last action read a new file."""
        return self.audit.maybe_inject(messages)

    def normalize_observation(self, tool: str, observation: str) -> str:
        """Make workspace paths in a tool result relative before the model sees it."""
        return self.paths.normalize(tool, observation)

    def check_plan(self, steps: Sequence[Any]) -> Any:
        """Deterministic-output verdict for a finished plan."""
        return self.deterministic.check(steps)

    def describe(self) -> dict[str, Any]:
        return {
            "shell_safety": self.shell_safety.describe(),
            "tool_contract": self.contract.describe(),
            "specification_audit": self.audit.describe(),
            "think_tool": self.think.tool.describe(),
            "path_normalizer": self.paths.describe(),
            "deterministic_output": self.deterministic.describe(),
        }
