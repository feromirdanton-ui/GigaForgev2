"""CoderAgent — L2 coder over GigaChat Pro.

Doctrine: the Coder runs on Pro (the heavyweight model) and edits a single
target function via a unified diff. The Coder receives ONLY the
:class:`ContextSlice` (the target function body + signature-only skeletons of
its callers/callees + relevant imports + constraints) — NEVER the full file.
This is the structural guarantee of acceptance criterion §1.

The Coder is stateless w.r.t. retries: each :meth:`edit` invocation issues
exactly ONE LLM call and returns the raw model text. The PIPELINE (M4c)
detects bad diffs via :class:`DiffValidator`, and retries by calling
:meth:`edit` again with ``prev_rejection`` set (the validator's reason
string fed back verbatim). The Coder does NOT self-judge its output.

The Coder NEVER reads files. It imports ONLY:
    - :class:`ContextSlice` (a pydantic model — no file I/O at the type level)
    - :class:`GigaClient`, :class:`ChatResult` (HTTP, no file I/O)
    - :class:`ModelRouter`, :class:`BudgetTracker`, :class:`Tracer`
    - :class:`Settings`

If you find yourself adding ``open``, ``Path.read_text``, or an import from
``gigaforge.l0`` here, STOP — the Coder must remain structurally incapable of
reading files. ``test_agents_coder.test_coder_does_not_read_files`` enforces
this by scanning the module source.
"""
from __future__ import annotations

import json
import time
from typing import Any

from gigaforge.client.errors import BudgetExceeded
from gigaforge.client.giga_client import ChatResult, GigaClient
from gigaforge.config import Settings, get_settings
from gigaforge.core.budgets import BudgetTracker
from gigaforge.core.router import ModelRouter
from gigaforge.core.trace import Tracer
from gigaforge.firewall.slicer import ContextSlice

__all__ = ["CoderAgent", "is_diff"]

# Spec §8 — coder prompt (verbatim).
_CODER_PROMPT = (
    "Ты редактируешь функции. Контекст — Context Slice (JSON).\n"
    "Верни ТОЛЬКО unified diff в формате:\n"
    "--- a/<target_file>\n+++ b/<target_file>\n@@ -start,count +start,count @@\n"
    "<контекстные строки>\n-<удаляемые строки>\n+<добавляемые строки>\n"
    "Используй РОВНО то значение поля \"target_file\" из Context Slice в заголовках\n"
    "--- a/ и +++ b/. Не выдумывай путь и не используй плейсхолдеры вида\n"
    "a/path/to/file.py. Никаких объяснений. Никакого markdown. Только diff."
)


def is_diff(text: str) -> bool:
    """Quick heuristic: contains all three required unified-diff markers.

    Used by :meth:`CoderAgent.edit` to short-circuit obviously-bad output (see
    ``_MAX_INTERNAL_RETRIES`` below). The full structural check is the
    :class:`DiffValidator`'s job (the pipeline runs it); this is just a
    smoke-test that catches empty / prose responses.
    """
    if not text:
        return False
    return ("--- " in text) and ("+++ " in text) and ("@@" in text)


# Optional internal retry when the model returns obviously non-diff output.
# The pipeline still owns the canonical 2-retry loop via prev_rejection; this
# is a cheap fast-path that avoids a round-trip through the validator for
# empty / prose responses. Set to 0 to disable.
_MAX_INTERNAL_RETRIES = 1


class CoderAgent:
    """L2 coder over GigaChat-2-Pro.

    Receives only a :class:`ContextSlice` and a list of constraints; returns a
    raw unified-diff string. The pipeline runs the validator.
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

    async def edit(
        self,
        slice: ContextSlice,  # noqa: A002 — matches the spec's parameter name
        constraints: list[str],
        *,
        task_id: str,
        prev_rejection: str | None = None,
    ) -> str:
        """Produce a unified diff for the given slice.

        Each call issues exactly ONE LLM call to GigaChat-2-Pro and returns the
        raw model text (possibly fenced — the validator strips fences). On
        obviously-bad output (empty / no diff markers), retries up to
        :data:`_MAX_INTERNAL_RETRIES` times within this call before returning
        the (still-bad) text so the pipeline can react.
        """
        model = self._router.route("code")  # Pro
        max_tokens = int(self._s.budget_code_out)

        slice_dict = slice.to_prompt_dict()
        # Inline the target file path directly in the prompt (don't rely on the
        # model extracting it from the slice JSON — it sometimes "loses" the
        # field on larger contexts). Fall back to <target_file> if unknown.
        target_file = slice_dict.get("target_file") or "<target_file>"
        user_body_parts: list[str] = [_CODER_PROMPT]
        user_body_parts.append(
            f"Целевой файл для редактирования: {target_file}\n"
            f"Используй РОВНО этот путь в заголовках diff: "
            f"--- a/{target_file}\n+++ b/{target_file}"
        )
        if prev_rejection:
            user_body_parts.append(
                f"Предыдущая попытка была отклонена: {prev_rejection}. "
                "Исправь и верни новый diff."
            )
        user_body_parts.append(
            "Context Slice (JSON):\n"
            + json.dumps(slice_dict, ensure_ascii=False)
        )
        if constraints:
            user_body_parts.append(
                "Ограничения:\n- " + "\n- ".join(constraints)
            )
        user_content = "\n\n".join(user_body_parts)

        messages: list[dict[str, Any]] = [
            {"role": "user", "content": user_content},
        ]

        attempts = 0
        last_text = ""
        last_in = 0
        last_out = 0
        last_latency = 0
        last_estimated = False
        for attempt in range(_MAX_INTERNAL_RETRIES + 1):
            attempts = attempt + 1
            round_messages = list(messages)
            if attempt > 0:
                # Internal retry: model returned obvious garbage last time.
                round_messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Твой прошлый ответ не является unified diff "
                            "(отсутствуют --- / +++ / @@ маркеры). "
                            "Верни ТОЛЬКО diff."
                        ),
                    }
                )

            est_in = sum(
                len(str(m.get("content", ""))) for m in round_messages
            ) // 4
            # Estimate realistically (a diff is usually well under the cap);
            # reserving the full max_tokens ceiling on every retry would
            # prematurely exhaust the phase budget.
            est_out = min(max_tokens, max(max_tokens // 2, 400))
            # Atomic claim, not check-then-commit.  ``check`` validates and
            # ``commit`` records under two separate lock acquisitions, so two
            # concurrent coders (a sub-agent fan-out) both see the same
            # remaining balance and both are told there is room.  ``reserve``
            # records the claim under the same lock that validates it.
            claim = self._budgets.reserve("code", est_in, est_out, task_id=task_id)

            started = time.monotonic()
            try:
                result: ChatResult = await self._client.chat(
                    model,
                    round_messages,
                    temperature=0.2,
                    max_tokens=max_tokens,
                    task_id=task_id,
                    phase="code",
                )
            except BaseException:
                # The call never completed: give the claim back so it does not
                # starve the next attempt.
                claim.cancel()
                raise
            latency_ms = int((time.monotonic() - started) * 1000)

            text = result.content or ""
            usage = result.usage or {}
            tokens_in = int(usage.get("prompt_tokens", 0) or 0)
            tokens_out = int(usage.get("completion_tokens", 0) or 0)

            try:
                claim.settle(tokens_in, tokens_out)
                # HIGH-07: post-commit soft-overshoot guard.
                self._budgets.check_overshoot("code", task_id=task_id)
            except BudgetExceeded:
                raise  # STOP — propagate
            except Exception:  # noqa: BLE001 — accounting is best-effort
                pass

            self._tracer.trace(
                task_id,
                step=f"code{'_retry_' + str(attempts) if attempt > 0 else ''}",
                kind="llm",
                detail={
                    "model": model,
                    "attempt": attempts,
                    "tokens_in": tokens_in,
                    "tokens_out": tokens_out,
                    "estimated": bool(result.estimated),
                    "had_prev_rejection": prev_rejection is not None,
                    "is_diff_heuristic": is_diff(text),
                    "raw_head": text[:400],
                },
                tokens=tokens_in + tokens_out,
                latency_ms=latency_ms,
            )

            last_text = text
            last_in = tokens_in
            last_out = tokens_out
            last_latency = latency_ms
            last_estimated = bool(result.estimated)

            # Optional fast-path: short-circuit obviously-bad output. If the
            # model produced a real diff, return it. The pipeline's validator
            # does the structural check; we don't self-judge beyond this.
            if is_diff(text):
                return text
            # Otherwise loop once more (if retries remain); on the last
            # iteration, fall through and return the bad text.

        # Return whatever we got — the pipeline (validator) will catch it and
        # retry with prev_rejection. This preserves the contract that the
        # Coder doesn't self-judge structural validity.
        self._tracer.trace(
            task_id,
            step="code_returned_non_diff",
            kind="decision",
            detail={
                "reason": "model output failed the is_diff heuristic",
                "attempts": attempts,
                "tokens_in": last_in,
                "tokens_out": last_out,
                "latency_ms": last_latency,
                "estimated": last_estimated,
            },
        )
        return last_text
