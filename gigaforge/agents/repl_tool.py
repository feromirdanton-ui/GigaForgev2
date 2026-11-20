"""A REPL tool the harness owns end to end.

**Why not an external REPL / Browser-Use / Context7 dependency.**  Those tools
are useful, but each one is a second execution surface with its own trust model,
its own output format, and its own failure modes — and none of them knows about
this project's allowlist, worktree isolation or Firewall.  A harness that
delegates its execution to a tool it does not control has not taken on the work;
it has moved it.  So the REPL here is built on the two components that already
own execution and compression:

* :class:`~gigaforge.sandbox.executor.SandboxExecutor` — the only component that
  spawns subprocesses, with the allowlist, the worktree cwd and the timeout;
* :func:`gigaforge.firewall.compress` — so a 5 000-line traceback never reaches
  the model.

**Why a scratch file and not ``python -c``.**  The allowlist rejects shell
metacharacters in any argv token (``;``, ``|``, ``&``, ``$``, backtick, ``<``,
``>``) — and real Python code contains them constantly (``a > b``, ``x | y``,
f-strings with ``$``).  Passing code as an argument would therefore reject
legitimate programs.  Writing it to a scratch file inside the worktree and
running ``python <file>`` keeps the argv clean, keeps the code inside the
sandbox, and means the allowlist is enforced on the *interpreter* rather than
on the text of the program.

**Why the scratch file is removed.**  A REPL that leaves its inputs behind
pollutes the tree the agent is reasoning about — the next ``grep`` would find
the agent's own scratch code.  The file is written under a single hidden
directory and deleted in a ``finally``, so a crash cannot leak it.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path
from typing import Any

__all__ = ["ReplTool", "REPL_TOOL_NAME", "SCRATCH_DIR"]

log = logging.getLogger(__name__)

REPL_TOOL_NAME = "repl"

#: Where scratch programs live, relative to the worktree.  Hidden and single, so
#: a cleanup is one ``rmtree`` and a ``grep`` never trips over it.
SCRATCH_DIR = ".gigaforge_repl"

#: Hard ceiling on the program text.  A REPL step is a probe, not a module.
_MAX_CODE_CHARS = 20_000


class ReplTool:
    """Run a short Python program in the sandbox and return its output.

    The tool is deliberately narrow: it executes, it compresses, it cleans up.
    It does not keep interpreter state between calls — a stateful REPL would
    make a step's result depend on the steps before it, which is exactly the
    hidden coupling the ReAct trajectory is supposed to make explicit.  A step
    that needs earlier results should read them from the files it wrote.
    """

    name = REPL_TOOL_NAME

    def __init__(self, *, timeout_s: int = 60) -> None:
        self.timeout_s = timeout_s

    async def run(self, action_input: str, *, ctx: Any) -> str:
        """Execute ``action_input`` as a Python program; return raw output.

        The runner compresses what this returns, so the raw text is returned
        here — including a non-zero exit, which is an observation, not an
        exception.  A tool that raised on a failing program would turn every
        debugging step into a harness error.
        """
        code = (action_input or "").strip()
        if not code:
            return "ERROR: repl needs a program"
        if len(code) > _MAX_CODE_CHARS:
            return (
                f"ERROR: repl program is {len(code)} chars, over the "
                f"{_MAX_CODE_CHARS} limit — write it to a file instead"
            )

        worktree = Path(getattr(ctx, "worktree", None) or getattr(ctx, "repo_root", "."))
        sandbox = getattr(ctx, "sandbox", None)
        if sandbox is None:
            return "ERROR: repl needs a sandbox"

        scratch = worktree / SCRATCH_DIR
        try:
            scratch.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return f"ERROR: repl could not create its scratch directory: {exc}"

        program = scratch / "step.py"
        try:
            program.write_text(code, encoding="utf-8")
        except OSError as exc:
            return f"ERROR: repl could not write its program: {exc}"

        try:
            # ``python`` is in the default allowlist; the argv carries only a
            # relative path, so no metacharacter check can reject the program.
            result = await sandbox.run(
                ["python", str(program.relative_to(worktree))], kind="builderr"
            )
        except Exception as exc:  # noqa: BLE001 - a rejected command is an observation
            return f"ERROR: repl could not run: {exc}"
        finally:
            # Never leave the agent's own scratch code in the tree it reasons
            # about: the next grep would find it and mistake it for the project.
            try:
                shutil.rmtree(scratch, ignore_errors=True)
            except Exception:  # noqa: BLE001 - cleanup is best-effort
                pass

        output = getattr(result, "raw_output", "") or ""
        if not getattr(result, "ok", False):
            head = f"ERROR: repl exited {getattr(result, 'returncode', '?')}"
            return f"{head}\n{output}" if output else head
        return output or "(no output)"


def install(registry: Any, *, sandbox: Any = None) -> None:
    """Register the REPL tool on *registry* (idempotent).

    Kept as a function rather than a method so the registry stays the single
    place that knows which tools exist, and a caller can add the REPL without
    importing the whole agent layer.
    """
    if registry is None:
        return
    if registry.get(REPL_TOOL_NAME) is None:
        registry.register(ReplTool())


async def _smoke() -> str:  # pragma: no cover - manual smoke helper
    """Run the tool once against a throwaway worktree."""
    import tempfile

    from gigaforge.sandbox.executor import SandboxExecutor

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        ctx = type(
            "Ctx",
            (),
            {"worktree": root, "repo_root": root, "sandbox": SandboxExecutor(worktree=root)},
        )()
        return await ReplTool().run("print(2 + 2)", ctx=ctx)


if __name__ == "__main__":  # pragma: no cover
    print(asyncio.run(_smoke()))
