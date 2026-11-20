"""SummarizerAgent — L1 Lite context-compression (OpenHands/Cline pattern).

When the TaskState's ``learnings`` list (a list of free-form strings accumulated
by the AUTO agent during a long-running task) exceeds a token budget, the
SummarizerAgent compresses it to a 1-paragraph summary.

This is the OpenHands/Cline context-compression pattern: long-running agentic
loops accumulate context (each fix-loop iteration adds a learning); eventually
the accumulated context exceeds the L1/L2 model's input budget. The summarizer
collapses N learnings into 1 paragraph, preserving the salient signal.

Doctrine:
- Context is budget. Summarizer runs on Lite (cheap). Called between fix-loop
  iterations (NOT on every iteration — bounded to "when over budget").
- Diff is contract. Summarizer compresses scratchpad/learnings; it does NOT
  touch the diff.
- Every LLM call is budget-checked (phase="summarize", Lite caps) + traced.

TaskState integration:
    The AUTO agent (if it lands) will populate ``state.learnings``. Until then,
    this module treats ``state.scratchpad`` as the learnings list (the existing
    TaskState field). A future AUTO agent can add a dedicated ``learnings``
    field; the Summarizer API takes a ``list[str]`` directly so it doesn't
    depend on TaskState's shape.

API:
    summarizer = SummarizerAgent(client, router, budgets, tracer)
    summary: str = await summarizer.summarize(learnings, task_id=...)
    # summary is a 1-paragraph compressed string.
"""
from __future__ import annotations

import time
from typing import Any

from gigaforge.client.errors import BudgetExceeded
from gigaforge.client.giga_client import ChatResult, GigaClient
from gigaforge.config import Settings, get_settings
from gigaforge.core.budgets import BudgetTracker
from gigaforge.core.router import ModelRouter
from gigaforge.core.trace import Tracer
from gigaforge.firewall.tokens import estimate_tokens

__all__ = ["SummarizerAgent", "DEFAULT_LEARNINGS_TOKEN_BUDGET"]

# When the learnings list exceeds this many tokens, summarize.
# Default 800 — comfortably under the Lite model's input budget; leaves room
# for the actual task prompt.
DEFAULT_LEARNINGS_TOKEN_BUDGET = 800

_SUMMARIZER_PROMPT = (
    "Ты — компрессор контекста. Дан список наблюдений (learnings) из прошлых "
    "шагов агента. Сожми их в ОДИН короткий абзац (3-5 предложений), "
    "сохранив главные факты: что уже испробовано, что сработало/не сработало, "
    "на чём сфокусироваться дальше. Не добавляй ничего от себя.\n"
    "Верни ТОЛЬКО текст абзаца (без JSON, без markdown)."
)

# Bounded retries on empty/garbage output.
_MAX_REPAIRS = 1


class SummarizerAgent:
    """L1 Lite context-compression agent.

    Called when the TaskState's learnings list exceeds a token budget.
    Bounded to 1 summary per call (the pipeline decides when to invoke;
    the summarizer itself doesn't loop).

    Fail-safe: on parse failure (empty output, unparseable) returns the
    FIRST learning verbatim + a note. Never crashes the pipeline.
    """

    def __init__(
        self,
        client: GigaClient,
        router: ModelRouter,
        budgets: BudgetTracker,
        tracer: Tracer,
        *,
        settings: Settings | None = None,
        learnings_token_budget: int = DEFAULT_LEARNINGS_TOKEN_BUDGET,
    ) -> None:
        self._client = client
        self._router = router
        self._budgets = budgets
        self._tracer = tracer
        self._s = settings or get_settings()
        self._budget = learnings_token_budget

    @property
    def learnings_token_budget(self) -> int:
        return self._budget

    def is_over_budget(self, learnings: list[str]) -> bool:
        """True if ``learnings`` exceeds the token budget and should be summarized."""
        joined = "\n".join(learnings)
        return estimate_tokens(joined) > self._budget

    async def summarize(
        self,
        learnings: list[str],
        *,
        task_id: str,
    ) -> str:
        """Compress ``learnings`` into a 1-paragraph summary.

        If ``learnings`` is empty, returns "". If a single short learning,
        returns it verbatim (no LLM call). Otherwise issues ONE Lite LLM call.

        Bounded to ``_MAX_REPAIRS + 1`` attempts; on parse failure (empty
        output), returns the first learning + a fallback note.
        """
        if not learnings:
            return ""
        # If a single short learning fits, don't spend tokens.
        joined = "\n".join(learnings)
        if estimate_tokens(joined) <= self._budget:
            # No summarization needed — but we DO need to honor the caller's
            # explicit request to summarize. Caller should check is_over_budget
            # first. If they called us anyway, we return the joined learnings
            # unchanged (deterministic, 0 LLM calls).
            return joined

        # Build the prompt. Cap the input learnings themselves to avoid
        # blowing the input budget (the whole point is to shrink them).
        learnings_payload = self._cap_learnings_for_prompt(learnings)
        base_messages: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": (
                    f"{_SUMMARIZER_PROMPT}\n\n"
                    f"Learnings ({len(learnings)} entries):\n{learnings_payload}"
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
                            f"Твой прошлый ответ пуст или некорректен: {last_error}. "
                            "Верни один абзац текста."
                        ),
                    }
                )

            est_in = sum(len(str(m.get("content", ""))) for m in round_messages) // 4
            est_out = max(self._s.budget_plan_out // 2, 300)
            self._budgets.check("summarize", est_in, est_out, task_id=task_id)

            started = time.monotonic()
            chat_result: ChatResult = await self._client.chat(
                self._router.route("summarize"),
                round_messages,
                temperature=0.1,
                max_tokens=int(self._s.budget_plan_out),
                task_id=task_id,
                phase="summarize",
            )
            latency_ms = int((time.monotonic() - started) * 1000)
            text = chat_result.content or ""
            usage = chat_result.usage or {}
            tokens_in = int(usage.get("prompt_tokens", 0) or 0)
            tokens_out = int(usage.get("completion_tokens", 0) or 0)

            try:
                self._budgets.commit_task(task_id, "summarize", tokens_in, tokens_out)
                self._budgets.check_overshoot("summarize", task_id=task_id)
            except BudgetExceeded:
                raise  # STOP — propagate
            except Exception:  # noqa: BLE001 — accounting best-effort
                pass

            self._tracer.trace(
                task_id,
                step=f"summarize{'_retry_' + str(attempts) if attempt > 0 else ''}",
                kind="llm",
                detail={
                    "model": self._router.route("summarize"),
                    "attempt": attempts,
                    "tokens_in": tokens_in,
                    "tokens_out": tokens_out,
                    "estimated": bool(chat_result.estimated),
                    "n_learnings": len(learnings),
                    "input_tokens_estimate": est_in,
                },
                tokens=tokens_in + tokens_out,
                latency_ms=latency_ms,
            )

            summary = text.strip()
            if summary:
                # Sanity: ensure the summary is meaningfully shorter than the
                # joined learnings (otherwise we didn't compress — fall back).
                if estimate_tokens(summary) < estimate_tokens(joined):
                    return summary
                # If the model returned something LONGER than the input,
                # retry once with a sharper prompt.
                last_error = "summary not shorter than input"
                continue
            last_error = "empty response"

        # Fail-safe: return the first learning + a note (no LLM call further).
        self._tracer.trace(
            task_id,
            step="summarize_default",
            kind="decision",
            detail={
                "reason": "summarizer returned empty/uncompressing output after retries",
                "attempts": attempts,
            },
        )
        first = learnings[0] if learnings else ""
        return f"{first}\n[additional learnings omitted — summarizer failed]"

    # ----------------------------------------------------------- internals

    def _cap_learnings_for_prompt(self, learnings: list[str]) -> str:
        """Cap the learnings payload so the prompt itself fits in the Lite budget.

        Strategy: take the FIRST N learnings whose total token count is
        under ``self._budget`` (the most recent learnings are at the end of
        the list and are usually the most relevant — but the FIRST ones
        establish context, so we keep those). Truncates with a marker.
        """
        cap = self._budget
        used = 0
        kept: list[str] = []
        for learning in learnings:
            t = estimate_tokens(learning)
            if used + t > cap:
                # Keep at least the first one even if it's too long.
                if not kept:
                    kept.append(learning[: cap * 4])
                kept.append("[learnings truncated]")
                break
            kept.append(learning)
            used += t
        return "\n".join(kept)
