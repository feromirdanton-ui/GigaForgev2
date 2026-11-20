"""Bounded fix loop (M4–M5).

The loop that turns a rejected diff into an accepted one:

    propose ─► validate ─► (rejected) ─► compress failure ─► propose ─► …

Three rules keep it honest:

**One repair mechanism, one number.**  The loop calls the firewall validator with
``max_retries=0`` — a single pass — and owns the iteration budget itself.
Leaving the validator's internal two retries *and* an outer three-iteration loop
in place would spend up to nine model calls for one task while reporting "3
iterations".  Here the bound is exactly ``max_iterations`` proposals.

**Fresh information, not the same rejection twice.**  Each repair is fed the
*compressed* failure (short, structured, from the pytest/exc snippet) plus the
repair hint.  A node that fails identically ``escalate_after_repeats`` times in a
row stops early: repeating the same prompt against the same context produces the
same diff, and burning the budget proves nothing.

**The breaker is consulted, never bypassed.**  If the model call is tripping a
circuit breaker, the loop stops instead of hammering a dependency that is down.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional, Sequence

from .config import CoreConfig, FixLoopConfig
from .contracts import Clock, ProposeResult, RunContext, SystemClock, ValidateResult
from .errors import BreakerOpenError, FixLoopExhausted
from .prompt import FIX_INSTRUCTION, build_phase_prompt, extract_diff

ProposeFn = Callable[..., Any]


@dataclass
class FixIteration:
    """One propose→validate cycle."""

    index: int
    kind: str                       # "initial" | "repair"
    diff: str = ""
    accepted: bool = False
    reason: str = ""
    repair_hint: str = ""
    failure_summary: str = ""
    validate: Optional[ValidateResult] = None
    model: str = ""
    tokens: int = 0
    latency_ms: float = 0.0
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "kind": self.kind,
            "accepted": self.accepted,
            "reason": self.reason,
            "model": self.model,
            "tokens": self.tokens,
            "latency_ms": round(self.latency_ms, 3),
            "diff_chars": len(self.diff or ""),
            "validate": self.validate.to_dict() if self.validate else None,
            "error": self.error,
            "has_repair_hint": bool(self.repair_hint),
        }


@dataclass
class FixLoopResult:
    """Outcome of the whole loop."""

    run_id: str
    task: str
    accepted: bool
    iterations: list[FixIteration] = field(default_factory=list)
    diff: str = ""
    changed_files: list[str] = field(default_factory=list)
    reason: str = ""
    breaker_open: bool = False
    duration_ms: float = 0.0
    error: str = ""
    metrics: dict[str, Any] = field(default_factory=dict)

    @property
    def proposals(self) -> int:
        """Model proposals spent — the number the iteration bound caps."""
        return len(self.iterations)

    @property
    def repairs(self) -> int:
        return max(0, len(self.iterations) - 1)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "accepted": self.accepted,
            "proposals": self.proposals,
            "repairs": self.repairs,
            "reason": self.reason,
            "changed_files": list(self.changed_files),
            "breaker_open": self.breaker_open,
            "duration_ms": round(self.duration_ms, 3),
            "iterations": [i.to_dict() for i in self.iterations],
            "metrics": dict(self.metrics),
            "error": self.error,
        }


class FixLoop:
    """Drives propose→validate until the diff is accepted or the budget is spent."""

    def __init__(
        self,
        *,
        config: Optional[CoreConfig] = None,
        validator: Any = None,
        firewall: Any = None,
        metrics: Any = None,
        trace: Any = None,
        clock: Optional[Clock] = None,
        breaker: Any = None,
        sleep: Optional[Callable[[float], None]] = None,
    ) -> None:
        self.config = config or CoreConfig()
        self.settings: FixLoopConfig = self.config.fixloop
        self.validator = validator
        self.firewall = firewall
        self.metrics = metrics
        self.trace = trace
        self.clock: Clock = clock or SystemClock()
        self.breaker = breaker
        self._sleep = sleep or time.sleep

    # ── helpers ──────────────────────────────────────────────────────────────
    def _validate_once(self, diff: str, *, task: str, dry_run: bool) -> ValidateResult:
        """Single validate pass, normalised.

        ``max_retries=0`` is the whole point: the loop below owns the budget.
        """
        if self.validator is None:
            raise FixLoopExhausted("FixLoop needs a validator")
        try:
            outcome = self.validator.validate(diff, retry_fn=None, task=task,
                                              dry_run=dry_run, max_retries=0)
        except Exception as exc:  # noqa: BLE001 - a crashing gate must reject
            # A validator that raises has *not* approved anything.  Letting the
            # exception escape used to end the run as "accepted" with no file
            # touched — the worst possible failure mode for a harness the user
            # trusts to say done / not done.
            return ValidateResult(
                accepted=False,
                applied=False,
                reason=f"validation error: {type(exc).__name__}: {exc}",
            )
        return _normalise_validation(outcome)

    def validate_once(self, diff: str, *, task: str = "", dry_run: bool = False) -> ValidateResult:
        """Public single-pass validation.

        The pipeline's ``validate`` node calls this when no repair loop ran, so the
        executor and the loop agree on what "validated" means instead of each
        having its own notion.
        """
        return self._validate_once(diff, task=task, dry_run=dry_run)

    def _compress_failure(self, raw: str, *, hint: str = "") -> str:
        """Compress raw failure output into a short structured summary.

        Delegates to the firewall's compressor so the fix prompt gets the same
        bounded, structured form the rest of the system uses — and degrades to a
        plain clip when no firewall is wired in.
        """
        text = (raw or "").strip()
        if not text:
            return ""
        if self.firewall is not None:
            try:
                result = self.firewall.compress(text, kind="auto", hint=hint)
                # ``CompressionResult`` exposes the payload as ``.data``; the
                # old code read ``.payload``, which never existed, so the
                # compression branch silently fell through to the raw
                # ``text[:2000]`` cut on every single call.  A best-effort
                # branch that always fails is not best-effort, it is dead.
                payload = getattr(result, "data", None)
                if payload is not None:
                    import json as _json
                    return _json.dumps(payload, ensure_ascii=False)[:4000]
            except Exception:  # pragma: no cover - compression is best effort
                pass
        return text[:2000]

    @staticmethod
    def _same_failure(a: str, b: str) -> bool:
        return bool(a) and a.strip() == b.strip()

    # ── main loop ────────────────────────────────────────────────────────────
    def run(
        self,
        task: str,
        *,
        propose_fn: Optional[ProposeFn] = None,
        initial_diff: str = "",
        ctx: Optional[RunContext] = None,
        slice_text: str = "",
        tests_output: str = "",
        max_iterations: Optional[int] = None,
        dry_run: bool = False,
        run_id: str = "",
        approve: Optional[Callable[[str], bool]] = None,
    ) -> FixLoopResult:
        """Run the loop.

        ``propose_fn(phase, prompt, iteration, repair_hint)`` returns a diff (or a
        :class:`ProposeResult`).  It is called for the initial attempt *and* for
        every repair, so the caller owns model selection and budgeting via the
        router it passes in.
        """
        started = self.clock.now()
        budget = int(self.settings.max_iterations if max_iterations is None else max_iterations)
        budget = max(1, budget)
        run_id = run_id or (ctx.run_id if ctx else f"fix-{int(self.clock.timestamp())}")
        result = FixLoopResult(run_id=run_id, task=task, accepted=False)

        if not initial_diff and propose_fn is None:
            result.reason = "nothing to validate and no proposer"
            # The reason is mirrored into ``error`` as well: callers branch on
            # ``error`` to decide whether the loop was even able to try, and a
            # silent empty result looks like "validated and rejected".
            result.error = result.reason
            result.duration_ms = (self.clock.now() - started) * 1000.0
            return result

        current_diff = self._strip(initial_diff)
        repair_hint = ""
        failure_summary = self._compress_failure(tests_output) if tests_output else ""
        previous_failure = ""
        repeats = 0

        for index in range(budget):
            # Consult the breaker *before* spending a proposal.  Calling it first
            # and catching ``BreakerOpenError`` afterwards still returns a clean
            # result, but it burns an iteration slot and inflates ``proposals`` —
            # and ``proposals`` is exactly the number the bound is graded on.
            if self.breaker is not None and self.breaker.is_open:
                result.breaker_open = True
                result.reason = "breaker open"
                result.error = "circuit breaker is open; no proposal was attempted"
                break

            kind = "initial" if index == 0 and not repair_hint else "repair"
            iteration = FixIteration(index=index + 1, kind=kind,
                                     repair_hint=repair_hint, failure_summary=failure_summary)

            # ── obtain a candidate diff ──────────────────────────────────────
            if index > 0 or not current_diff:
                if propose_fn is None:
                    iteration.error = "no proposer for a repair iteration"
                    iteration.reason = iteration.error
                    result.iterations.append(iteration)
                    break
                prompt = self._build_prompt(
                    index, task=task, slice_text=slice_text, repair_hint=repair_hint,
                    failure_summary=failure_summary, previous_diff=current_diff,
                    ctx=ctx,
                )
                try:
                    produced = self._call_proposer(propose_fn, index, prompt, repair_hint, ctx)
                except BreakerOpenError as exc:
                    iteration.error = str(exc)
                    iteration.reason = "breaker open"
                    result.iterations.append(iteration)
                    result.breaker_open = True
                    result.reason = "breaker open"
                    break
                except BaseException as exc:  # noqa: BLE001 - reported, not raised
                    iteration.error = str(exc)[:1000]
                    iteration.reason = "proposer failed"
                    result.iterations.append(iteration)
                    break

                propose = _as_propose(produced)
                iteration.model = propose.model
                iteration.tokens = int(_usage_tokens(propose.usage))
                iteration.latency_ms = propose.latency_ms
                candidate = self._strip(propose.diff or extract_diff(propose.text))
                if not candidate:
                    iteration.reason = "proposer returned no diff"
                    result.iterations.append(iteration)
                    # An empty repair is a legitimately different failure mode:
                    # keep a hint so the next repair can be told to emit a diff.
                    repair_hint = (repair_hint or "").strip() or "you must emit a ```diff fence"
                    failure_summary = "previous proposal contained no diff"
                    continue
                current_diff = candidate

            iteration.diff = current_diff

            # ── human approval, before anything touches the worktree ─────────
            # The gate lives here because this is the only place that sees the
            # exact diff that will be applied, exactly once per attempt.  A veto
            # ends the loop with a stated reason instead of silently continuing.
            if approve is not None:
                try:
                    allowed = bool(approve(current_diff))
                except BaseException as exc:  # noqa: BLE001 - a broken gate denies
                    allowed = False
                    iteration.error = f"approval gate failed: {exc}"
                if not allowed:
                    iteration.reason = "declined by the user"
                    result.reason = "declined by the user"
                    result.diff = ""
                    result.iterations.append(iteration)
                    self._emit("fixloop.declined", run_id=run_id, iteration=index + 1)
                    break

            # ── validate it ──────────────────────────────────────────────────
            try:
                validation = self._validate_once(current_diff, task=task, dry_run=dry_run)
            except BaseException as exc:  # noqa: BLE001 - reported, not raised
                iteration.error = str(exc)[:1000]
                iteration.reason = "validation raised"
                result.iterations.append(iteration)
                break

            iteration.validate = validation
            iteration.accepted = validation.accepted
            iteration.reason = validation.reason or ("accepted" if validation.accepted else "rejected")
            result.iterations.append(iteration)

            if validation.accepted:
                result.accepted = True
                result.diff = current_diff
                result.changed_files = list(validation.changed_files)
                result.reason = "accepted"
                self._emit("fixloop.accepted", run_id=run_id, iteration=index + 1,
                           files=len(validation.changed_files))
                break

            # ── prepare the next repair ──────────────────────────────────────
            repair_hint = validation.repair_hint or repair_hint
            failure_summary = self._compress_failure(
                tests_output or validation.reason, hint=repair_hint
            ) or validation.reason

            if self.settings.stop_on_identical and self._same_failure(
                f"{validation.reason}|{failure_summary}", previous_failure
            ):
                repeats += 1
                if repeats >= max(1, self.settings.escalate_after_repeats):
                    result.reason = (
                        f"repeated the same failure {repeats + 1}× — stopping early "
                        f"({validation.reason})"
                    )
                    self._emit("fixloop.repeat_stop", run_id=run_id, iteration=index + 1,
                               repeats=repeats)
                    break
            else:
                repeats = 0
            previous_failure = f"{validation.reason}|{failure_summary}"

            self._emit("fixloop.rejected", run_id=run_id, iteration=index + 1,
                       reason=validation.reason, retries=validation.retries_used)

        if not result.accepted and not result.reason:
            result.reason = f"not fixed in {len(result.iterations)} iteration(s)"
        if not result.accepted and not result.diff:
            result.diff = current_diff

        result.duration_ms = (self.clock.now() - started) * 1000.0
        result.metrics = {
            "iterations": len(result.iterations),
            "repairs": result.repairs,
            "budget": budget,
            "changed_files": len(result.changed_files),
        }
        if self.metrics is not None:
            self.metrics.incr("core.fixloop.runs", 1, {"accepted": str(result.accepted)})
            self.metrics.observe("core.fixloop.iterations", len(result.iterations))
        if self.trace is not None:
            self.trace.event("fixloop.finish", run_id=run_id, accepted=result.accepted,
                             iterations=len(result.iterations), reason=result.reason[:300])
        return result

    # ── proposer plumbing ────────────────────────────────────────────────────
    def _build_prompt(
        self,
        index: int,
        *,
        task: str,
        slice_text: str,
        repair_hint: str,
        failure_summary: str,
        previous_diff: str,
        ctx: Optional[RunContext],
    ) -> str:
        blocks: list[tuple[str, str]] = []
        if previous_diff:
            blocks.append(("Previous diff", previous_diff))
        if failure_summary and failure_summary != previous_diff:
            blocks.append(("Validation output", failure_summary))
        prompt = build_phase_prompt(
            "fix" if repair_hint else "code",
            goal=task,
            slice_text=slice_text,
            failure=failure_summary,
            hint=repair_hint,
            scratchpad=ctx.state.scratchpad_text() if (ctx and ctx.state) else "",
            instruction=FIX_INSTRUCTION if repair_hint else None,
            extra_blocks=blocks,
        )
        return prompt.render()

    def _call_proposer(
        self,
        propose_fn: ProposeFn,
        index: int,
        prompt: str,
        repair_hint: str,
        ctx: Optional[RunContext],
    ) -> Any:
        """Invoke the proposer, honouring the breaker and the run deadline."""
        if ctx is not None and ctx.expired():
            raise FixLoopExhausted(
                "run deadline passed before the fix iteration could start",
                detail={"run_id": ctx.run_id},
            )
        phase = "fix" if repair_hint else "code"

        def invoke() -> Any:
            return propose_fn(phase, prompt, index, repair_hint)

        if self.breaker is not None:
            return self.breaker.call(invoke)
        return invoke()

    @staticmethod
    def _strip(diff: str) -> str:
        text = (diff or "").strip()
        if not text:
            return ""
        if "```" in text or "~~~" in text:
            return extract_diff(text) or text
        return text

    def _emit(self, kind: str, **payload: Any) -> None:
        if self.metrics is not None:
            self.metrics.incr(f"core.{kind}", 1)
        if self.trace is not None:
            self.trace.event(kind, **payload)


# ── normalisation helpers ────────────────────────────────────────────────────
def _normalise_validation(outcome: Any) -> ValidateResult:
    """Map the firewall's ``ValidationOutcome`` onto the core DTO."""
    if outcome is None:
        return ValidateResult(accepted=False, reason="no validation outcome")
    if isinstance(outcome, ValidateResult):
        return outcome
    accepted = bool(getattr(outcome, "accepted", False))
    reason = str(getattr(outcome, "reason", "") or ("accepted" if accepted else "rejected"))
    # Prefer an explicit hint when the caller's validator offers one; otherwise
    # reconstruct it from the firewall's repair request.
    hint = str(getattr(outcome, "repair_hint", "") or "")
    last = getattr(outcome, "last_repair", None) if not hint else None
    if last is None and not hint:
        attempts = getattr(outcome, "attempts", None) or []
        if attempts:
            last = getattr(attempts[-1], "repair", None)
    if last is not None:
        hint_fn = getattr(last, "text", None)
        if callable(hint_fn):
            try:
                hint = str(hint_fn())
            except Exception:  # pragma: no cover - defensive
                hint = str(getattr(last, "reason", ""))
        else:
            hint = str(getattr(last, "reason", "") or "")
    return ValidateResult(
        accepted=accepted,
        applied=bool(getattr(outcome, "applied", False)),
        retries_used=int(getattr(outcome, "retries_used", 0) or 0),
        changed_files=list(getattr(outcome, "changed_files", []) or []),
        reason=reason,
        repair_hint=hint,
        raw=outcome,
    )


def _as_propose(produced: Any) -> ProposeResult:
    if isinstance(produced, ProposeResult):
        return produced
    if isinstance(produced, str):
        return ProposeResult(diff=produced)
    if isinstance(produced, Mapping):
        return ProposeResult(
            text=str(produced.get("text", "")),
            diff=str(produced.get("diff", "")),
            model=str(produced.get("model", "")),
            usage=produced.get("usage"),
        )
    if produced is None:
        return ProposeResult()
    return ProposeResult(text=str(produced))


def _usage_tokens(usage: Any) -> int:
    if usage is None:
        return 0
    if isinstance(usage, Mapping):
        return int(usage.get("total_tokens", 0) or 0)
    return int(getattr(usage, "total_tokens", 0) or 0)


def max_iterations_of(config: CoreConfig) -> int:
    return int(config.fixloop.max_iterations)


__all__ = [
    "FixLoop",
    "FixIteration",
    "FixLoopResult",
    "ProposeFn",
    "max_iterations_of",
]
