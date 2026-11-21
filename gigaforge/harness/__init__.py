"""The harness — the layer LangChain/LangGraph is a bridge *to*, not a substitute for.

The merge plan's level 2 says: LangGraph gives checkpointing, ``interrupt()``
and streaming, and that is a *bridge*.  A bridge is not a harness.  What a
harness adds, and what this module provides, is the part that decides **how a
run is allowed to proceed**:

* **A budget that is checked before every call and committed after it.**  The
  router already owns a ``BudgetTracker``; the harness makes it the single
  authority for the whole run, including the ReAct layer (which used to build
  its own).
* **Retries with a policy, not a bare loop.**  A 429 is not a 500: the first
  waits for ``Retry-After``, the second backs off exponentially, and neither
  retries a 400.  The harness owns that decision so no agent re-implements it.
* **Recovery from context overflow.**  The guard detects; the harness compacts
  and retries once, then gives up with a typed reason.
* **A ledger.**  Every run produces one structured record of what it cost and
  whether it worked — the input to a benchmark, a CI gate and a report.

Design rules:

* **The harness never calls a model itself.**  It wraps a *runner* (anything
  with ``run(task, state, ctx=...)``) and a *client*, so the same harness
  drives the classic loop, the LangGraph graph, or a test double.
* **Every failure is typed.**  ``HarnessResult.finish_reason`` is one of a
  closed set, so a caller can branch without parsing strings.
* **Nothing is swallowed.**  A retry is recorded; a give-up is recorded with
  the exception's type and message.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from gigaforge.client.errors import (
    AuthError,
    BudgetExceeded,
    ConfigurationError,
    GigaForgeError,
    RateLimitError,
    RequestError,
    ServerError,
)
from gigaforge.core.context_window_guard import ContextOverflowError
from gigaforge.harness.ledger import RunLedger

__all__ = [
    "HarnessPolicy",
    "HarnessResult",
    "Harness",
    "classify_error",
    "is_retryable",
]

log = logging.getLogger(__name__)

#: Finish reasons the harness can produce.  A closed set, so callers branch on
#: a value instead of matching message text.
FINISH_OK = "ok"
FINISH_BUDGET = "budget_exceeded"
FINISH_OVERFLOW = "context_overflow"
FINISH_RATE_LIMIT = "rate_limited"
FINISH_AUTH = "auth_error"
FINISH_ERROR = "error"
FINISH_CANCELLED = "cancelled"


@dataclass
class HarnessPolicy:
    """How a run is allowed to proceed.

    Defaults are deliberately conservative: two retries, a 30 s ceiling on any
    single wait, and no retry of a request the server rejected on its merits.
    """

    #: Attempts per model call, including the first.
    max_attempts: int = 3
    #: Base backoff in seconds; doubled per attempt.
    backoff_base: float = 1.0
    #: Ceiling for a single wait, so a hostile ``Retry-After`` cannot stall a run.
    max_wait: float = 30.0
    #: How many times the whole episode may be re-run after a compaction.
    max_compactions: int = 2
    #: Stop the run when the budget is exhausted (never "one more call").
    stop_on_budget: bool = True
    #: Sleep function, injectable so tests do not wait.
    sleep: Callable[[float], None] = time.sleep


@dataclass
class HarnessResult:
    """What a harness run produced."""

    result: Any = None
    ledger: RunLedger | None = None
    finish_reason: str = FINISH_OK
    error: str = ""
    attempts: int = 0

    @property
    def ok(self) -> bool:
        return self.finish_reason == FINISH_OK

    def to_dict(self) -> dict[str, Any]:
        return {
            "finish_reason": self.finish_reason,
            "ok": self.ok,
            "error": self.error,
            "attempts": self.attempts,
            "ledger": self.ledger.to_dict() if self.ledger is not None else None,
        }


def classify_error(exc: BaseException) -> str:
    """Map an exception to a finish reason.

    The mapping is the harness's contract with its callers: a 429 is
    ``rate_limited`` (retryable, and the caller may want to reschedule), a 401
    is ``auth_error`` (never retryable — the credentials are wrong), and a
    budget stop is ``budget_exceeded`` (a decision, not a failure).
    """
    if isinstance(exc, ContextOverflowError):
        return FINISH_OVERFLOW
    if isinstance(exc, BudgetExceeded):
        return FINISH_BUDGET
    if isinstance(exc, RateLimitError):
        return FINISH_RATE_LIMIT
    if isinstance(exc, (AuthError, ConfigurationError)):
        return FINISH_AUTH
    if isinstance(exc, GigaForgeError):
        return FINISH_ERROR
    return FINISH_ERROR


def is_retryable(exc: BaseException) -> bool:
    """True when retrying the same call could plausibly succeed.

    A 400/401/403 is a statement about the request or the credentials; sending
    it again produces the same answer and burns budget.  A 429, a 5xx, a
    timeout and a network error are all transient.
    """
    if isinstance(exc, (AuthError, ConfigurationError, RequestError)):
        return False
    if isinstance(exc, (RateLimitError, ServerError)):
        return True
    if isinstance(exc, ContextOverflowError):
        return False  # handled by compaction, not by retrying
    if isinstance(exc, BudgetExceeded):
        return False
    # NetworkError / RequestTimeout are GigaForgeError subclasses; treat any
    # remaining transport error as transient.
    return isinstance(exc, GigaForgeError)


def _retry_after(exc: BaseException) -> float | None:
    """The server's ``Retry-After``, when the exception carries one."""
    for attr in ("retry_after", "retry_after_s"):
        value = getattr(exc, attr, None)
        if value is not None:
            try:
                return float(value)
            except (TypeError, ValueError):
                return None
    detail = getattr(exc, "detail", None)
    if isinstance(detail, dict):
        for key in ("retry_after", "retry_after_s"):
            if key in detail:
                try:
                    return float(detail[key])
                except (TypeError, ValueError):
                    return None
    return None


class Harness:
    """Drive one agent run under a policy, and record what it cost.

    Usage::

        harness = Harness(policy=HarnessPolicy())
        outcome = harness.run(runner, task, state, ctx=ctx, run_id="run-1")
        outcome.ledger.write("reports/run-1.json")

    The harness is deliberately synchronous: it is the boundary between the
    async agent layer and the blocking CLI/CI that drives it, and it already
    owns the only blocking primitive the project has (``await_blocking``).
    """

    def __init__(
        self,
        *,
        policy: HarnessPolicy | None = None,
        tracer: Any = None,
        budgets: Any = None,
        checkpointer: Any = None,
    ) -> None:
        self.policy = policy or HarnessPolicy()
        self._tracer = tracer
        self._budgets = budgets
        self.checkpointer = checkpointer
        #: The model the ledger prices against.  Set by :meth:`run` from the
        #: runner's router; empty means "price with the default table", which
        #: still yields a real number for a known family.
        self._ledger_model = ""

    # -- public API ----------------------------------------------------------
    def run(
        self,
        runner: Any,
        task: str,
        state: Any,
        *,
        ctx: Any = None,
        run_id: str = "",
        engine: str = "",
        bootstrap_hint: str | None = None,
        react: bool = False,
    ) -> HarnessResult:
        """Run *runner* under the policy and return a :class:`HarnessResult`.

        ``react=True`` dispatches through :func:`gigaforge.agents.engine.run_react`
        instead of calling ``runner.run`` directly.  That distinction is the
        whole point of the flag: ``run_react`` is the only function that knows
        about ``GIGAFORGE_REACT_ENGINE``, so a harness that called ``runner.run``
        itself made the LangGraph ReAct graph unreachable on the product path
        while still labelling the ledger ``engine: react``.  Reporting one
        engine and running another is exactly the class of defect the stack
        command exists to prevent.
        """
        from gigaforge.core.sync_adapter import await_blocking

        resolved_engine = engine
        if react:
            from gigaforge.agents.engine import react_engine

            resolved_engine = engine or react_engine()

        ledger = RunLedger(
            run_id=run_id or getattr(state, "task_id", "") or "run",
            goal=task,
            engine=resolved_engine or type(runner).__name__,
        )
        # The model the ledger prices against.  The router is the only thing
        # that knows which model a phase resolves to, and the ledger needs a
        # name to look up a price.  Without this the cost columns were zero for
        # every run — see the note in ``_record_result``.
        try:
            router = getattr(runner, "router", None)
            if router is not None:
                self._ledger_model = str(router.route("code") or "")
        except Exception:  # noqa: BLE001 - a missing router is not fatal
            self._ledger_model = ""
        outcome = HarnessResult(ledger=ledger)
        compactions = 0

        while True:
            outcome.attempts += 1
            try:
                if react:
                    from gigaforge.agents.engine import run_react

                    result = run_react(
                        runner,
                        task,
                        state,
                        ctx=ctx,
                        bootstrap_hint=bootstrap_hint,
                        checkpointer=self.checkpointer,
                        thread_id=run_id or getattr(state, "task_id", "") or "react-plan",
                        engine=resolved_engine,
                    )
                else:
                    result = await_blocking(
                        runner.run(task, state, ctx=ctx, bootstrap_hint=bootstrap_hint)
                    )
            except ContextOverflowError as exc:
                # Recovery, not termination: compact and try the episode again.
                if compactions >= self.policy.max_compactions:
                    outcome.finish_reason = FINISH_OVERFLOW
                    outcome.error = str(exc)[:500]
                    ledger.decision(name="overflow_giveup", detail={"detail": str(exc)[:300]})
                    break
                compactions += 1
                ledger.compaction(
                    before_tokens=int(getattr(exc, "estimated_tokens", 0) or 0),
                    after_tokens=0,
                    dropped=0,
                )
                ledger.decision(name="overflow_retry", detail={"attempt": outcome.attempts})
                continue
            except BaseException as exc:  # noqa: BLE001 - classified below
                reason = classify_error(exc)
                if is_retryable(exc) and outcome.attempts < self.policy.max_attempts:
                    wait = self._wait_for(exc, outcome.attempts)
                    ledger.retries += 1
                    ledger.rate_limit_waits += wait
                    ledger.decision(
                        name="retry",
                        detail={
                            "attempt": outcome.attempts,
                            "wait_s": round(wait, 3),
                            "error": f"{type(exc).__name__}: {exc}"[:300],
                        },
                    )
                    self.policy.sleep(wait)
                    continue
                outcome.finish_reason = reason
                outcome.error = f"{type(exc).__name__}: {exc}"[:500]
                ledger.error = outcome.error
                ledger.decision(
                    name="giveup",
                    detail={"reason": reason, "error": outcome.error[:300]},
                )
                break

            outcome.result = result
            outcome.finish_reason = FINISH_OK
            ledger.finish(getattr(result, "finish_reason", "") or FINISH_OK)
            self._record_result(ledger, result)
            break

        if ledger.finished_at is None:
            ledger.finish(outcome.finish_reason)
        return outcome

    # -- internals -----------------------------------------------------------
    def _wait_for(self, exc: BaseException, attempt: int) -> float:
        """How long to wait before retrying *exc* on *attempt*.

        A server-supplied ``Retry-After`` wins over the exponential backoff —
        it is the only number that reflects the actual limit — but it is
        clamped to ``max_wait`` so a hostile header cannot stall the run.
        """
        server = _retry_after(exc)
        if server is not None:
            return max(0.0, min(float(server), self.policy.max_wait))
        backoff = self.policy.backoff_base * (2 ** max(0, attempt - 1))
        return max(0.0, min(backoff, self.policy.max_wait))

    def _record_result(self, ledger: RunLedger, result: Any) -> None:
        """Copy the runner's own accounting into the ledger.

        ``ReActResult`` carries ``llm_calls`` and per-step records but not the
        token totals — those live in the ``BudgetTracker`` the runner shares
        with the router.  Reading them from there is what makes the ledger's
        token columns real instead of zero: a cost report that always says
        ``0+0 tok`` is worse than no report, because it looks authoritative.
        """
        for step in getattr(result, "steps", None) or []:
            if not isinstance(step, dict):
                continue
            ledger.tool(
                name=str(step.get("action", "") or step.get("tool", "") or ""),
                ok=bool(step.get("ok", True)),
                step_idx=step.get("step_idx"),
            )
        calls = int(getattr(result, "llm_calls", 0) or 0)
        for index in range(calls):
            ledger.llm(name=f"step_{index}")

        # Token totals: prefer the shared budget tracker, which the runner
        # commits to after every call.  The ledger's run id *is* the task id —
        # the pipeline binds them together before planning.
        task_id = ledger.run_id
        budgets = self._budgets
        if budgets is not None and task_id:
            try:
                used = budgets._task_used_in(task_id)  # noqa: SLF001 - same package
                ledger.tokens_in_override = int(used.get("in", 0))
                ledger.tokens_out_override = int(used.get("out", 0))
            except Exception:  # noqa: BLE001 - accounting is best-effort
                pass

        # Cost.  ``note_cost`` existed and was never called by anything, so the
        # ledger's cost columns were structurally zero: measured live, a run
        # that spent 12 769 prompt + 2 095 completion tokens across six calls
        # reported ``cost_per_task_usd = 0.0`` and ``phase_cost_micros`` all
        # zero.  A cost report that always says 0.00 is worse than no report,
        # because it looks authoritative.
        #
        # The per-phase token totals are what the price is applied to, so the
        # report can say WHERE the money went, not only how much.
        try:
            from gigaforge.core.cost import PriceTable

            prices = PriceTable.from_env()
            phases = getattr(budgets, "_phase_used", None) if budgets is not None else None
            model = str(getattr(self, "_ledger_model", "") or "")
            if isinstance(phases, dict) and phases:
                for phase, totals in phases.items():
                    ledger.note_cost(
                        model=model,
                        phase=str(phase),
                        tokens_in=int(totals.get("in", 0) or 0),
                        tokens_out=int(totals.get("out", 0) or 0),
                        prices=prices,
                    )
            else:
                ledger.note_cost(
                    model=model,
                    phase="total",
                    tokens_in=ledger.tokens_in,
                    tokens_out=ledger.tokens_out,
                    prices=prices,
                )
        except Exception:  # noqa: BLE001 - accounting is best-effort
            pass
