"""ReviewerAgent — L1 reviewer over GigaChat Lite.

Checks a diff against the checklist (syntax, style, broken callers, error
handling) and returns a :class:`ReviewVerdict`. Like the planner, uses
content-parsing + :func:`json_repair` (no function calling) — the same
JSON-repair discipline. On unparseable output after 2 repairs, defaults to a
REJECT verdict with a precise issue string (fail-safe: never let a bad
reviewer response silently APPROVE a diff).

Every LLM call traced (kind="llm") and budget-checked via
:meth:`BudgetTracker.check` (phase="review" → Lite caps).
"""
from __future__ import annotations

import json
import time
from typing import Any, Literal

from pydantic import BaseModel, Field

from gigaforge.agents.planner import json_repair
from gigaforge.agents.schemas import review_response_format
from gigaforge.client.errors import BudgetExceeded
from gigaforge.client.giga_client import GigaClient
from gigaforge.config import Settings, get_settings
from gigaforge.core.budgets import BudgetTracker
from gigaforge.core.router import ModelRouter
from gigaforge.core.trace import Tracer
from gigaforge.firewall.slicer import ContextSlice

__all__ = ["ReviewerAgent", "ReviewVerdict"]

# Spec §13 — reviewer prompt (verbatim).
_REVIEWER_PROMPT = (
    "Проверь diff против чек-листа (синтаксис, стиль, сломанные callers, "
    'обработка ошибок). Верни ТОЛЬКО JSON: '
    '{"verdict":"ok|reject","issues":["…"]}'
)

_MAX_REPAIRS = 2

Verdict = Literal["ok", "reject"]


class ReviewVerdict(BaseModel):
    """Structured reviewer output.

    ``verdict`` is either ``"ok"`` (approve the diff) or ``"reject"`` (the
    pipeline retries the Coder with the issues as the rejection reason).
    """

    verdict: Verdict
    issues: list[str] = Field(default_factory=list)


class ReviewerAgent:
    """L1 reviewer over GigaChat-2-Lite.

    Uses the Lite model + Lite budget caps. Fail-safe: unparseable JSON
    after retries defaults to REJECT (never silently approve).
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

    async def review(
        self,
        diff: str,
        slice: ContextSlice,  # noqa: A002 — matches spec parameter name
        *,
        task_id: str,
    ) -> ReviewVerdict:
        """Review ``diff`` in the context of ``slice``.

        Returns a :class:`ReviewVerdict`. Fail-safe: unparseable output after
        retries -> REJECT with the issue ``"reviewer returned unparseable JSON"``.
        """
        model = self._router.route("review")  # Lite
        max_tokens = int(self._s.budget_plan_out)

        slice_dict = slice.to_prompt_dict()
        base_messages: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": (
                    f"{_REVIEWER_PROMPT}\n\n"
                    f"Context Slice (JSON):\n{json.dumps(slice_dict, ensure_ascii=False)}\n\n"
                    f"Diff:\n```\n{diff}\n```"
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

            est_in = sum(
                len(str(m.get("content", ""))) for m in round_messages
            ) // 4
            # Estimate realistically (a verdict JSON is small); reserving the
            # full max_tokens ceiling on every retry prematurely exhausts budget.
            est_out = min(max_tokens, max(max_tokens // 3, 200))
            self._budgets.check("review", est_in, est_out, task_id=task_id)

            started = time.monotonic()
            result = await self._client.chat(
                model,
                round_messages,
                temperature=0.1,
                max_tokens=max_tokens,
                task_id=task_id,
                phase="review",
                response_format=review_response_format(),
            )
            latency_ms = int((time.monotonic() - started) * 1000)

            text = result.content or ""
            usage = result.usage or {}
            tokens_in = int(usage.get("prompt_tokens", 0) or 0)
            tokens_out = int(usage.get("completion_tokens", 0) or 0)

            try:
                self._budgets.commit_task(task_id, "review", tokens_in, tokens_out)
                # HIGH-07: post-commit soft-overshoot guard.
                self._budgets.check_overshoot("review", task_id=task_id)
            except BudgetExceeded:
                raise  # STOP — propagate
            except Exception:  # noqa: BLE001 — accounting is best-effort
                pass

            self._tracer.trace(
                task_id,
                step=f"review{'_retry_' + str(attempts) if attempt > 0 else ''}",
                kind="llm",
                detail={
                    "model": model,
                    "attempt": attempts,
                    "tokens_in": tokens_in,
                    "tokens_out": tokens_out,
                    "estimated": bool(result.estimated),
                },
                tokens=tokens_in + tokens_out,
                latency_ms=latency_ms,
            )

            verdict, err = _parse_verdict_json(text)
            if verdict is not None and err is None:
                return verdict
            last_error = err or "неизвестная ошибка разбора"
            continue

        # Fail-safe default: REJECT with a clear issue string. NEVER approve on
        # parse failure — that would silently let bad diffs through.
        self._tracer.trace(
            task_id,
            step="review_default_reject",
            kind="decision",
            detail={
                "reason": "reviewer failed to produce valid JSON after retries",
                "attempts": attempts,
                "last_error": last_error,
            },
        )
        return ReviewVerdict(
            verdict="reject",
            issues=["reviewer returned unparseable JSON"],
        )


def _parse_verdict_json(text: str) -> tuple[ReviewVerdict | None, str | None]:
    """Parse reviewer output. Returns ``(verdict, None)`` or ``(None, error)``."""
    repaired = json_repair(text)
    try:
        data = json.loads(repaired)
    except json.JSONDecodeError as e:
        return None, f"невалидный JSON: {e.msg} (pos {e.pos})"
    if not isinstance(data, dict):
        return None, "ожидался JSON-объект"
    verdict_raw = data.get("verdict")
    if verdict_raw not in ("ok", "reject"):
        return None, f"verdict должен быть 'ok' или 'reject', получено {verdict_raw!r}"
    issues_raw = data.get("issues", [])
    if not isinstance(issues_raw, list):
        return None, "issues должен быть списком"
    # Coerce all issues to strings (the model may emit non-string items).
    issues = [str(i) for i in issues_raw]
    try:
        return ReviewVerdict(verdict=verdict_raw, issues=issues), None
    except Exception as e:  # pydantic ValidationError
        return None, f"невалидная структура: {e}"
