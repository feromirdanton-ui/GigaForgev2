"""SelfDebugger — adds a "diagnose" step before re-calling the Coder.

The self-debug pattern from "Teaching Large Language Models to Self-Debug"
(Chen et al., 2023): before re-prompting the Coder after a test failure, an
L1 Lite call produces a structured diagnosis of WHY tests failed:

    {root_cause, affected_symbol, suggested_approach}

This diagnosis is fed into the Coder's ``prev_rejection`` as a richer signal
than the raw compressed pytest output. The Coder still runs on Pro (L2);
the diagnosis is the cheap reasoning layer that helps the Coder converge
faster.

Doctrine:
- Context is budget. Diagnose runs on Lite (cheap); the Coder still runs on
  Pro. Diagnosis is BOUNDED to the existing fix-loop cap (3 iters).
- Diff is contract. Self-debug feeds the Coder; it doesn't replace the diff
  pipeline. The Coder still returns a diff; the DiffValidator still enforces.
- Every LLM call is budget-checked (phase="review", Lite caps) + traced.

API:
    debugger = SelfDebugger(client, router, budgets, tracer)
    diag: Diagnosis = await debugger.diagnose(
        diff, failures_compressed, slice_, task_id=...
    )
    # diag.to_prev_rejection() -> str: merged into the Coder's next prompt.
"""
from __future__ import annotations

import json
import time
from typing import Any

from pydantic import BaseModel, Field

from gigaforge.agents.planner import json_repair
from gigaforge.client.errors import BudgetExceeded
from gigaforge.client.giga_client import ChatResult, GigaClient
from gigaforge.config import Settings, get_settings
from gigaforge.core.budgets import BudgetTracker
from gigaforge.core.router import ModelRouter
from gigaforge.core.trace import Tracer
from gigaforge.firewall.slicer import ContextSlice

__all__ = ["SelfDebugger", "Diagnosis"]

_DIAGNOSER_PROMPT = (
    "Ты — отладчик. Тебе дан diff и проваленные тесты/линт (сжато).\n"
    "Проанализируй, ПОЧЕМУ тесты упали, и предложи диагноз.\n"
    "Верни ТОЛЬКО JSON вида:\n"
    '{"root_cause":"кратко, что именно сломано",'
    '"affected_symbol":"имя функции/класса",'
    '"suggested_approach":"как именно исправить (1 предложение)"}\n'
    "Никакого markdown. Только JSON."
)

_MAX_REPAIRS = 1


class Diagnosis(BaseModel):
    """Structured self-debug diagnosis — feeds back into the Coder's prompt.

    The ``suggested_approach`` is the rich signal: a one-sentence directive
    the Coder sees on its next retry, instead of the raw compressed pytest
    output. ``root_cause`` + ``affected_symbol`` add diagnostic context.
    """

    root_cause: str = ""
    affected_symbol: str = ""
    suggested_approach: str = ""
    raw: str = Field(default="", description="the raw compressed failures payload, kept for trace")

    def to_prev_rejection(self, *, raw_reason: str | None = None) -> str:
        """Render the diagnosis as a string for the Coder's ``prev_rejection``.

        Format:
            ``diagnosis: <suggested_approach> | root_cause: <...> | symbol: <...> | original: <raw>``
        """
        parts = [f"diagnosis: {self.suggested_approach}"]
        if self.root_cause:
            parts.append(f"root_cause: {self.root_cause}")
        if self.affected_symbol:
            parts.append(f"symbol: {self.affected_symbol}")
        if raw_reason:
            parts.append(f"original: {raw_reason}")
        return " | ".join(parts)


class SelfDebugger:
    """L1 Lite self-debugger. Bounded to 1 diagnosis per fix-loop iteration.

    The pipeline calls :meth:`diagnose` after a test/lint failure, BEFORE
    re-invoking the Coder. The fix-loop cap (default 3) still applies —
    self-debug does NOT extend the loop, it just enriches the rejection
    signal the Coder sees.
    """

    def __init__(
        self,
        client: GigaClient,
        router: ModelRouter,
        budgets: BudgetTracker,
        tracer: Tracer,
        *,
        settings: Settings | None = None,
    ) -> None:
        self._client = client
        self._router = router
        self._budgets = budgets
        self._tracer = tracer
        self._s = settings or get_settings()

    async def diagnose(
        self,
        diff: str,
        failures: dict[str, Any] | str,
        slice: ContextSlice,  # noqa: A002 — matches spec parameter name
        *,
        task_id: str,
    ) -> Diagnosis:
        """Produce one :class:`Diagnosis` for the given diff + failures.

        ``failures`` may be a Firewall-compressed dict (preferred) or a
        plain string (e.g., the raw validator reason). Bounded to
        ``_MAX_REPAIRS + 1`` attempts; on parse failure it returns a default
        diagnosis (NEVER crashes the pipeline).
        """
        if isinstance(failures, dict):
            failures_str = json.dumps(failures, ensure_ascii=False)
        else:
            failures_str = str(failures or "")

        slice_dict = slice.to_prompt_dict()
        base_messages: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": (
                    f"{_DIAGNOSER_PROMPT}\n\n"
                    f"Context Slice (JSON):\n{json.dumps(slice_dict, ensure_ascii=False)}\n\n"
                    f"Diff (applied):\n```\n{diff}\n```\n\n"
                    f"Failures (compressed):\n{failures_str}"
                ),
            }
        ]

        last_error: str | None = None
        attempts = 0
        for attempt in range(_MAX_REPAIRS + 1):
            attempts = attempt + 1
            round_messages = list(base_messages)
            if last_error is not None:
                round_messages.append(
                    {
                        "role": "user",
                        "content": (
                            f"Твой прошлый ответ не валиден: {last_error}. "
                            "Верни только JSON."
                        ),
                    }
                )

            est_in = sum(len(str(m.get("content", ""))) for m in round_messages) // 4
            est_out = max(self._s.budget_plan_out // 3, 200)
            claim = self._budgets.reserve("review", est_in, est_out, task_id=task_id)

            started = time.monotonic()
            try:
                chat_result: ChatResult = await self._client.chat(
                    self._router.route("review"),
                    round_messages,
                    temperature=0.1,
                    max_tokens=int(self._s.budget_plan_out),
                    task_id=task_id,
                    phase="review",
                )
            except BaseException:
                claim.cancel()
                raise
            latency_ms = int((time.monotonic() - started) * 1000)
            text = chat_result.content or ""
            usage = chat_result.usage or {}
            tokens_in = int(usage.get("prompt_tokens", 0) or 0)
            tokens_out = int(usage.get("completion_tokens", 0) or 0)

            try:
                claim.settle(tokens_in, tokens_out)
                self._budgets.check_overshoot("review", task_id=task_id)
            except BudgetExceeded:
                raise  # STOP — propagate
            except Exception:  # noqa: BLE001 — accounting best-effort
                pass

            self._tracer.trace(
                task_id,
                step=f"self_debug{'_retry_' + str(attempts) if attempt > 0 else ''}",
                kind="llm",
                detail={
                    "model": self._router.route("review"),
                    "attempt": attempts,
                    "tokens_in": tokens_in,
                    "tokens_out": tokens_out,
                    "estimated": bool(chat_result.estimated),
                },
                tokens=tokens_in + tokens_out,
                latency_ms=latency_ms,
            )

            diag, err = _parse_diagnosis_json(text)
            if diag is not None and err is None:
                diag.raw = failures_str
                return diag
            last_error = err or "неизвестная ошибка разбора"

        # Fail-safe default: a diagnosis that says "couldn't parse" so the
        # pipeline still gets a Diagnosis object back. NEVER crash.
        self._tracer.trace(
            task_id,
            step="self_debug_default",
            kind="decision",
            detail={"reason": "self-debug unparseable after retries", "attempts": attempts},
        )
        return Diagnosis(
            root_cause="self-debug returned unparseable JSON",
            affected_symbol="",
            suggested_approach="retry the diff",
            raw=failures_str,
        )


def _parse_diagnosis_json(text: str) -> tuple[Diagnosis | None, str | None]:
    """Parse self-debug output. Returns ``(diagnosis, None)`` or ``(None, error)``."""
    repaired = json_repair(text)
    try:
        data = json.loads(repaired)
    except json.JSONDecodeError as e:
        return None, f"невалидный JSON: {e.msg} (pos {e.pos})"
    if not isinstance(data, dict):
        return None, "ожидался JSON-объект"
    root_cause = str(data.get("root_cause", "")).strip()
    affected_symbol = str(data.get("affected_symbol", "")).strip()
    suggested_approach = str(data.get("suggested_approach", "")).strip()
    try:
        return (
            Diagnosis(
                root_cause=root_cause,
                affected_symbol=affected_symbol,
                suggested_approach=suggested_approach,
            ),
            None,
        )
    except Exception as e:  # pydantic ValidationError
        return None, f"невалидная структура: {e}"
