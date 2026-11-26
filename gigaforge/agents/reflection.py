"""ReflectorAgent — L1 Lite self-critique of a produced diff + test/lint failures.

The Reflection / self-critique pattern from Aider / SWE-agent / OpenHands:
after the Coder produces a diff and the DiffValidator or tests reject it,
an L1 Lite model emits a STRUCTURED critique that's merged into the Coder's
``prev_rejection`` on the next retry. This is a richer signal than the raw
validator reason string: it diagnoses *why* the diff is wrong and *what*
approach to take instead.

Doctrine:
- Context is budget. Reflector runs on Lite (cheap). Exactly 1 reflection per
  coder retry — NO reflection storm (the Coder's retry budget still applies).
- Diff is contract. Reflection adds loops AROUND the diff pipeline; it does
  NOT replace the validator. The Coder still returns a diff; the DiffValidator
  still enforces.
- Every LLM call is budget-checked (phase="review", Lite caps) + traced +
  metrics-recorded (via GigaClient's metrics_sink).

API:
    reflector = ReflectorAgent(client, router, budgets, tracer)
    critique: Critique = await reflector.reflect(diff, failures, slice_, task_id=...)
    # critique.to_prev_rejection() -> str: merged into the Coder's next prompt.
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

__all__ = ["ReflectorAgent", "Critique"]

_REFLECTOR_PROMPT = (
    "Ты — критик: проанализируй предложенный diff и проваленные тесты/линт.\n"
    "Верни ТОЛЬКО JSON вида:\n"
    '{"strengths":["..."],"weaknesses":["..."],'
    '"suggested_fix":"короткое описание, что менять","confidence":0.7}\n'
    "Поле strengths — что в diff правильно (может быть пусто).\n"
    "weaknesses — конкретные проблемы (например: \"regex {3,} слишком жёсткий\").\n"
    "suggested_fix — ОДНО короткое предложение для следующей попытки Coder'а.\n"
    "confidence — число от 0.0 до 1.0.\n"
    "Никакого markdown. Только JSON."
)

# Bounded retries on unparseable output. After this, default to a generic
# critique (fail-safe: never let a parse failure silently APPROVE).
_MAX_REPAIRS = 1


class Critique(BaseModel):
    """Structured reflector output — feeds back into the Coder's prev_rejection.

    The ``suggested_fix`` field is the rich signal — a one-sentence directive
    the Coder sees on its next retry, instead of the raw validator reason.
    """

    strengths: list[str] = Field(default_factory=list)
    weaknesses: list[str] = Field(default_factory=list)
    suggested_fix: str = ""
    confidence: float = 0.0

    def to_prev_rejection(self, *, raw_reason: str | None = None) -> str:
        """Render the critique as a string for the Coder's ``prev_rejection``.

        Format:
            ``reflection: <suggested_fix> | weaknesses: <...> | original: <raw_reason>``

        If ``raw_reason`` is the validator's reason string, it's appended so the
        Coder sees BOTH the reflector's diagnosis AND the structural reason.
        """
        parts = [f"reflection: {self.suggested_fix}"]
        if self.weaknesses:
            parts.append("weaknesses: " + "; ".join(self.weaknesses))
        if raw_reason:
            parts.append(f"original: {raw_reason}")
        return " | ".join(parts)


class ReflectorAgent:
    """L1 Lite self-critique agent. Bounded to 1 reflection per coder retry.

    Used by the pipeline between coder-validate-reject and the next coder call.
    The pipeline controls invocation cadence; this agent just emits one
    :class:`Critique` per :meth:`reflect` call.
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

    async def reflect(
        self,
        diff: str,
        failures: dict[str, Any] | str,
        slice: ContextSlice,  # noqa: A002 — matches spec parameter name
        *,
        task_id: str,
    ) -> Critique:
        """Produce one :class:`Critique` for the given diff + failures.

        ``failures`` may be a Firewall-compressed dict (preferred) or a plain
        string (e.g., the raw validator reason). The reflector is bounded to
        ``_MAX_REPAIRS + 1`` attempts; on parse failure it returns a default
        critique that says "could not parse critique" — NEVER silently approves.
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
                    f"{_REFLECTOR_PROMPT}\n\n"
                    f"Context Slice (JSON):\n{json.dumps(slice_dict, ensure_ascii=False)}\n\n"
                    f"Diff:\n```\n{diff}\n```\n\n"
                    f"Failures:\n{failures_str}"
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

            # Pre-budget check. Phase = "review" (L1 Lite caps).
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
            from gigaforge.client.types import usage_tokens
            tokens_in, tokens_out = usage_tokens(chat_result.usage)

            try:
                claim.settle(tokens_in, tokens_out)
                self._budgets.check_overshoot("review", task_id=task_id)
            except BudgetExceeded:
                raise  # STOP — propagate
            except Exception:  # noqa: BLE001 — accounting best-effort
                pass

            self._tracer.trace(
                task_id,
                step=f"reflect{'_retry_' + str(attempts) if attempt > 0 else ''}",
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

            critique, err = _parse_critique_json(text)
            if critique is not None and err is None:
                return critique
            last_error = err or "неизвестная ошибка разбора"

        # Fail-safe default: a critique that says "couldn't parse" so the
        # pipeline still gets a Critique object back. NEVER silently approve.
        self._tracer.trace(
            task_id,
            step="reflect_default",
            kind="decision",
            detail={"reason": "reflector unparseable after retries", "attempts": attempts},
        )
        return Critique(
            strengths=[],
            weaknesses=["reflector returned unparseable JSON"],
            suggested_fix="retry the diff with a different approach",
            confidence=0.0,
        )


def _parse_critique_json(text: str) -> tuple[Critique | None, str | None]:
    """Parse reflector output. Returns ``(critique, None)`` or ``(None, error)``."""
    repaired = json_repair(text)
    try:
        data = json.loads(repaired)
    except json.JSONDecodeError as e:
        return None, f"невалидный JSON: {e.msg} (pos {e.pos})"
    if not isinstance(data, dict):
        return None, "ожидался JSON-объект"
    strengths = data.get("strengths", [])
    if not isinstance(strengths, list):
        strengths = [str(strengths)]
    weaknesses = data.get("weaknesses", [])
    if not isinstance(weaknesses, list):
        weaknesses = [str(weaknesses)]
    suggested_fix = str(data.get("suggested_fix", "")).strip()
    confidence_raw = data.get("confidence", 0.0)
    try:
        confidence = float(confidence_raw)
    except (TypeError, ValueError):
        confidence = 0.0
    # Clamp to [0, 1].
    confidence = max(0.0, min(1.0, confidence))
    try:
        return (
            Critique(
                strengths=[str(s) for s in strengths],
                weaknesses=[str(w) for w in weaknesses],
                suggested_fix=suggested_fix,
                confidence=confidence,
            ),
            None,
        )
    except Exception as e:  # pydantic ValidationError
        return None, f"невалидная структура: {e}"
