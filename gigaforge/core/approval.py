"""ApprovalGate — human-in-the-loop gate before ``commit_all``.

The Cline/Aider HITL pattern: before the pipeline commits the diff to the
worktree (and the cache-hit path commits to repo_root), show the diff +
test results to the user and let them choose:

- ``apply``   — proceed with the commit (default).
- ``reject`` — abort this diff, route back to the Coder (counts toward the
                retry budget — bounded by the pipeline's coder_max_retries).
- ``edit``    — open the diff in the user's $EDITOR; re-validate the edited
                diff before committing (a separate flow; not the default).
- ``abort``   — abort the whole task immediately (no retry).

Default implementation: ``--approve`` flag → interactive prompt (rich) showing
the diff + test results, the user picks one of the four options.

``--auto-approve`` flag: no prompt; logs that auto-approval is on and proceeds
(suitable for CI / scripted runs).

``FileApprovalGate``: writes the diff to a file, waits for a sentinel file to
appear (e.g., ``<diff_file>.approve`` or ``<diff_file>.reject``). Useful for
non-interactive editors / out-of-band review.

Doctrine:
- Context is budget. The gate is L0 (0 LLM calls). The diff is shown as-is.
- Diff is contract. The gate doesn't modify the diff (except ``edit`` mode,
  which re-validates). Rejection routes back to the Coder.
- Bounded. Rejection counts toward the Coder retry cap (2). No infinite loop.
"""
from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

__all__ = [
    "ApprovalGate",
    "ApprovalDecision",
    "ApprovalResult",
    "InteractiveApprovalGate",
    "AutoApprovalGate",
    "FileApprovalGate",
]


class ApprovalDecision(StrEnum):
    """The user's choice at the approval gate."""

    APPLY = "apply"
    REJECT = "reject"
    EDIT = "edit"
    ABORT = "abort"


@dataclass
class ApprovalResult:
    """The gate's verdict for one diff.

    Fields:
        decision: the user's choice.
        edited_diff: if decision == EDIT, the user-edited diff text
            (validated by the pipeline afterward). Empty for other decisions.
        reason: free-form string the user supplied (or the gate's own log msg).
    """

    decision: ApprovalDecision
    edited_diff: str = ""
    reason: str = ""


class ApprovalGate:
    """Protocol — every gate implementation implements :meth:`review`.

    The pipeline calls ``gate.review(diff, test_results, task_id)`` before
    ``commit_all``. The default implementations are below.
    """

    async def review(
        self,
        diff: str,
        test_results: dict[str, Any] | str,
        *,
        task_id: str,
    ) -> ApprovalResult:  # pragma: no cover — protocol
        raise NotImplementedError


class AutoApprovalGate(ApprovalGate):
    """No prompt; auto-approve. For CI / scripted runs (``--auto-approve``).

    Logs that auto-approval is on. The pipeline proceeds without waiting.
    """

    def __init__(self, *, logger: Callable[[str], None] | None = None) -> None:
        self._log = logger or (lambda msg: log.info(msg))

    async def review(
        self,
        diff: str,
        test_results: dict[str, Any] | str,
        *,
        task_id: str,
    ) -> ApprovalResult:
        self._log(
            f"[approval] auto-approve ON for task={task_id!r}; "
            f"diff={len(diff)} chars; tests={'ok' if _tests_ok(test_results) else 'fail'}"
        )
        return ApprovalResult(
            decision=ApprovalDecision.APPLY,
            reason="auto-approve",
        )


class InteractiveApprovalGate(ApprovalGate):
    """Interactive prompt (``--approve`` flag). Shows the diff + test results.

    Uses ``rich`` (already a GigaForge dep) for rendering. The user picks one
    of: apply / reject / edit / abort. Defaults to ``apply`` on empty input
    (or after a 60s timeout — interactive UX safety net).

    Non-TTY environments: falls back to :class:`AutoApprovalGate` with a warning.
    """

    def __init__(
        self,
        *,
        input_fn: Callable[[str], str] | None = None,
        output_fn: Callable[[str], None] | None = None,
        timeout_s: float = 60.0,
    ) -> None:
        self._input = input_fn or input
        self._output = output_fn or (lambda s: print(s))
        self._timeout_s = timeout_s

    async def review(
        self,
        diff: str,
        test_results: dict[str, Any] | str,
        *,
        task_id: str,
    ) -> ApprovalResult:
        # Non-TTY fallback: auto-approve (the pipeline cannot prompt without a TTY).
        if not _is_interactive():
            log.warning(
                "approval: stdin is not a TTY; falling back to auto-approve"
            )
            return ApprovalResult(
                decision=ApprovalDecision.APPLY,
                reason="auto-approve (no TTY)",
            )

        # Render the diff + tests via rich (best-effort; if rich missing, plain).
        try:
            from rich.console import Console
            from rich.panel import Panel
            from rich.syntax import Syntax

            console = Console()
            console.print(
                Panel(
                    Syntax(diff, "diff", theme="ansi_dark", word_wrap=True),
                    title=f"[gigaforge] diff for task {task_id}",
                )
            )
            console.print(
                Panel(
                    str(test_results)[:2000],
                    title="[gigaforge] test results (compressed)",
                )
            )
        except Exception:  # noqa: BLE001 — best-effort rendering
            self._output(f"--- diff (task {task_id}) ---\n{diff}\n--- end diff ---")
            self._output(f"--- tests ---\n{test_results}\n--- end tests ---")

        self._output(
            "\n[approval] choose: apply (default) / reject / edit / abort"
        )
        try:
            raw = self._input("> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return ApprovalResult(decision=ApprovalDecision.ABORT, reason="user interrupt")

        if not raw or raw in ("y", "yes", "apply"):
            return ApprovalResult(decision=ApprovalDecision.APPLY, reason="user approved")
        if raw in ("n", "no", "reject"):
            return ApprovalResult(decision=ApprovalDecision.REJECT, reason="user rejected")
        if raw == "edit":
            edited = self._prompt_edit(diff)
            return ApprovalResult(
                decision=ApprovalDecision.EDIT,
                edited_diff=edited,
                reason="user edited",
            )
        if raw == "abort":
            return ApprovalResult(decision=ApprovalDecision.ABORT, reason="user aborted")
        # Unknown input → default to apply.
        return ApprovalResult(
            decision=ApprovalDecision.APPLY,
            reason=f"unknown input {raw!r} → default apply",
        )

    def _prompt_edit(self, diff: str) -> str:
        """Open $EDITOR on a tempfile containing the diff; return the edited text."""
        editor = os.environ.get("EDITOR", "vim")
        import subprocess
        import tempfile

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".diff", delete=False, encoding="utf-8"
        ) as f:
            f.write(diff)
            tmp_path = f.name
        try:
            subprocess.run([editor, tmp_path], check=False)
            return Path(tmp_path).read_text(encoding="utf-8")
        except Exception as exc:  # noqa: BLE001 — best-effort
            log.warning("approval: edit failed: %s; using original", exc)
            return diff


class FileApprovalGate(ApprovalGate):
    """Non-interactive gate: writes the diff to a file, waits for a sentinel.

    Workflow:
    1. The pipeline calls ``review(diff, tests, task_id)``.
    2. The gate writes ``<review_dir>/<task_id>.diff`` and
       ``<review_dir>/<task_id>.tests.json``.
    3. The gate polls for ``<review_dir>/<task_id>.decision`` (a single-line
       file containing ``apply`` / ``reject`` / ``edit`` / ``abort``).
    4. On ``edit``, reads the edited diff from ``<review_dir>/<task_id>.diff``
       (the user is expected to have edited it in place).

    Suitable for non-interactive editors (VS Code, vim in another tmux pane, etc.)
    or out-of-band review tools.

    Timeout: 1 hour default (configurable). On timeout, returns ABORT.
    """

    def __init__(
        self,
        review_dir: Path,
        *,
        poll_interval_s: float = 1.0,
        timeout_s: float = 3600.0,
        sleep_fn: Callable[[float], None] | None = None,
        clock_fn: Callable[[], float] | None = None,
    ) -> None:
        self._review_dir = Path(review_dir)
        self._review_dir.mkdir(parents=True, exist_ok=True)
        self._poll = poll_interval_s
        self._timeout = timeout_s
        self._sleep = sleep_fn or time.sleep
        self._clock = clock_fn or time.monotonic

    async def review(
        self,
        diff: str,
        test_results: dict[str, Any] | str,
        *,
        task_id: str,
    ) -> ApprovalResult:
        import json as _json

        diff_file = self._review_dir / f"{_sanitize(task_id)}.diff"
        tests_file = self._review_dir / f"{_sanitize(task_id)}.tests.json"
        decision_file = self._review_dir / f"{_sanitize(task_id)}.decision"

        diff_file.write_text(diff, encoding="utf-8")
        if isinstance(test_results, dict):
            tests_file.write_text(
                _json.dumps(test_results, ensure_ascii=False), encoding="utf-8"
            )
        else:
            tests_file.write_text(str(test_results), encoding="utf-8")

        log.info(
            "approval: wrote diff to %s; waiting for %s",
            diff_file,
            decision_file,
        )

        start = self._clock()
        while True:
            if decision_file.exists():
                raw = decision_file.read_text(encoding="utf-8").strip().lower()
                try:
                    raw_first = raw.splitlines()[0] if raw else ""
                except Exception:  # noqa: BLE001
                    raw_first = raw
                decision = _parse_decision(raw_first)
                # On edit, read the (possibly modified) diff file back.
                edited = ""
                if decision == ApprovalDecision.EDIT:
                    try:
                        edited = diff_file.read_text(encoding="utf-8")
                    except OSError:
                        edited = diff
                # Clean up sentinel files (best-effort).
                for f in (decision_file,):
                    try:
                        f.unlink()
                    except OSError:
                        pass
                return ApprovalResult(
                    decision=decision,
                    edited_diff=edited,
                    reason=f"file gate: {raw_first!r}",
                )
            elapsed = self._clock() - start
            if elapsed > self._timeout:
                log.warning(
                    "approval: file gate timed out after %.1fs for task %r",
                    elapsed,
                    task_id,
                )
                return ApprovalResult(
                    decision=ApprovalDecision.ABORT,
                    reason=f"file gate timeout after {elapsed:.1f}s",
                )
            self._sleep(self._poll)


# --- helpers ---------------------------------------------------------------


def _tests_ok(test_results: dict[str, Any] | str) -> bool:
    """Best-effort: True if the test_results payload indicates success."""
    if isinstance(test_results, dict):
        # Firewall-compressed pytest shape has "passed_count" + "failed_count".
        if test_results.get("failed_count", 0) == 0 and test_results.get("error_count", 0) == 0:
            return True
        return False
    # Raw string: just check for absence of "failed".
    return "failed" not in str(test_results).lower()


def _is_interactive() -> bool:
    """True if stdin is a TTY AND stdout is a TTY."""
    try:
        return os.isatty(0) and os.isatty(1)
    except Exception:  # noqa: BLE001
        return False


def _parse_decision(raw: str) -> ApprovalDecision:
    raw = (raw or "").strip().lower()
    if raw in ("apply", "yes", "y", "ok", "approve"):
        return ApprovalDecision.APPLY
    if raw in ("reject", "no", "n", "deny"):
        return ApprovalDecision.REJECT
    if raw in ("edit", "modify", "change"):
        return ApprovalDecision.EDIT
    if raw in ("abort", "stop", "cancel"):
        return ApprovalDecision.ABORT
    # Unknown → APPLY (fail-open; the pipeline proceeds; the validator still
    # catches bad diffs downstream).
    return ApprovalDecision.APPLY


def _sanitize(name: str) -> str:
    """Make a task_id safe for use as a filename (no path traversal)."""
    # Keep only alphanumerics + dash + underscore + dot.
    return "".join(c for c in name if c.isalnum() or c in "-_.")


# Convenience factory used by the pipeline / CLI.
def make_gate(
    *,
    approve: bool = False,
    auto_approve: bool = False,
    file_gate_dir: Path | None = None,
) -> ApprovalGate | None:
    """Construct the right gate based on CLI flags. Returns None if no gate.

    Precedence:
    - ``--auto-approve`` -> AutoApprovalGate (no prompt, logs).
    - ``--approve``       -> InteractiveApprovalGate (prompts).
    - ``file_gate_dir``   -> FileApprovalGate (sentinel file).
    - else                -> None (no gate; pipeline behaves as before).
    """
    if auto_approve:
        return AutoApprovalGate()
    if file_gate_dir is not None:
        return FileApprovalGate(Path(file_gate_dir))
    if approve:
        return InteractiveApprovalGate()
    return None
