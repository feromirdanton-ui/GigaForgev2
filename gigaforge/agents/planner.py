"""PlannerAgent — L1 planner over GigaChat Lite.

Doctrine: planner runs on the cheap Lite model; it decomposes the goal into a
list of :class:`PlanStep` objects. The LLM returns JSON in ``content`` — we use
content-parsing + :func:`json_repair` (NOT function calling) to sidestep
GigaChat's single-function-call-per-turn limit. If the model emits
unparseable / structurally invalid JSON after 2 repairs, we raise
:class:`PlanEscalation` (a :class:`GigaForgeError` subclass) so the pipeline
can escalate to the human — NEVER infinite-loop.

The STABLE LLM prefix comes from ``state.serialize_prefix()`` (goal +
current plan + slice_hash); it is prepended verbatim as the system message so
the future KV-cache stays valid (the prefix is byte-identical across retries
within one plan() call).

Every LLM call is traced via :class:`Tracer` (kind="llm") and budget-checked
via :class:`BudgetTracker.check` BEFORE the call (STOP + report semantics).
"""
from __future__ import annotations

import json
import re
import time
from typing import Any

from gigaforge.client.errors import BudgetExceeded, GigaForgeError
from gigaforge.client.giga_client import GigaClient
from gigaforge.config import Settings, get_settings
from gigaforge.core.budgets import BudgetTracker
from gigaforge.core.router import ModelRouter
from gigaforge.core.task_state import PlanStep, TaskState
from gigaforge.core.trace import Tracer

__all__ = [
    "PlannerAgent",
    "PlanEscalation",
    "json_repair",
    "parse_plan_json",
    "ALLOWED_TOOLS",
]

# The allowed tool set per spec §13. Anything else in a returned plan is a
# parse error (rejected with a precise reason).
ALLOWED_TOOLS: frozenset[str] = frozenset(
    {"read_structure", "edit_function", "run_tests", "grep"}
)

# Spec §13 — planner prompt (verbatim).
_PLANNER_PROMPT = (
    "Ты — планировщик. Разбей задачу на шаги. Верни ТОЛЬКО JSON-объект вида:\n"
    '{"steps":[{"tool":"edit_function","args":{"name":"validate_email"},"done":false}]}\n'
    "Поле \"tool\" принимает РОВНО ОДНО значение из списка: "
    '"read_structure", "edit_function", "run_tests", "grep". '
    "Не перечисляй варианты через символ |. Не пиши текст вне JSON. "
    "Каждый шаг должен содержать ровно один инструмент."
)

# Max repairs after the first attempt before escalating. Total attempts = 1 + 2.
_MAX_REPAIRS = 2


class PlanEscalation(GigaForgeError):
    """Raised when the planner cannot produce a valid plan after retries.

    The pipeline catches this and escalates to a human (per spec §15 / §6).
    NEVER silently swallow — the planner either returns steps or raises this.
    """


# --- module-level helpers ---------------------------------------------------


def json_repair(text: str) -> str:
    """Best-effort JSON extraction from an LLM response.

    Strips ``\\`\\`\\`json`` / ``\\`\\`\\`...\\`\\`\\` `` fences, then takes the
    substring from the first ``{`` to the last ``}``. If no braces are found,
    returns the (de-fenced) text unchanged so :func:`json.loads` produces a
    clear error message.

    This is RECOVERY, not parsing — we never raise; we hand back a best-effort
    string and let the caller's ``json.loads`` decide validity.
    """
    if not text:
        return ""
    s = text.strip()
    # Strip ``` fences. Models commonly wrap JSON in ```json ... ``` or ``` ... ```.
    if s.startswith("```"):
        lines = s.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        s = "\n".join(lines).strip()
    # Find outermost { ... } block. Take the first { and the LAST }.
    first = s.find("{")
    last = s.rfind("}")
    if first == -1 or last == -1 or last < first:
        # No braces — return de-fenced text; json.loads will fail clearly.
        return s
    return s[first : last + 1]


def parse_plan_json(text: str) -> tuple[list[PlanStep] | None, str | None]:
    """Parse model output into a list of validated :class:`PlanStep`.

    Returns ``(steps, None)`` on success or ``(None, error_msg)`` on failure.
    The error_msg is suitable to feed back into the model as a repair prompt
    (it is short, in Russian, and points at the specific defect).

    Validation rules:
        - The repaired JSON must parse as a JSON object.
        - Must contain a ``steps`` list of objects.
        - Each step's ``tool`` MUST be in :data:`ALLOWED_TOOLS`.
        - ``args`` MUST be a JSON object (dict); ``done`` defaults to False.
        - The plan MUST be non-empty.
    """
    repaired = json_repair(text)
    try:
        data = json.loads(repaired)
    except json.JSONDecodeError as e:
        return None, f"невалидный JSON: {e.msg} (pos {e.pos})"
    if not isinstance(data, dict):
        return None, "ожидался JSON-объект на верхнем уровне"
    steps_raw = data.get("steps")
    if not isinstance(steps_raw, list):
        return None, "поле 'steps' отсутствует или не является списком"
    steps: list[PlanStep] = []
    for i, item in enumerate(steps_raw):
        if not isinstance(item, dict):
            return None, f"шаг {i}: не является объектом"
        tool = item.get("tool")
        if not isinstance(tool, str) or not tool:
            return None, f"шаг {i}: отсутствует поле 'tool'"
        if tool not in ALLOWED_TOOLS:
            # Pragmatic repair: models sometimes paste the schema's pipe-union
            # (e.g. "read_structure|edit_function|...") literally. Pick the
            # first token (split on |, comma, whitespace) that is a valid tool.
            candidates = re.split(r"[|,\s]+", tool)
            picked = next((c for c in candidates if c in ALLOWED_TOOLS), None)
            if picked:
                tool = picked
            else:
                return None, (
                    f"шаг {i}: неизвестный инструмент {tool!r}; "
                    f"допустимы: {sorted(ALLOWED_TOOLS)}"
                )
        args = item.get("args", {})
        if not isinstance(args, dict):
            return None, f"шаг {i}: 'args' должен быть объектом"
        done = bool(item.get("done", False))
        try:
            steps.append(PlanStep(tool=tool, args=args, done=done))
        except Exception as e:  # pydantic ValidationError or similar
            return None, f"шаг {i}: невалидная структура: {e}"
    if not steps:
        return None, "план пустой"
    return steps, None


# --- PlannerAgent ------------------------------------------------------------


class PlannerAgent:
    """L1 planner over GigaChat-2-Lite.

    Produces a list of :class:`PlanStep` from a free-form task string. The
    prefix (goal + current plan + slice_hash) is taken from
    :meth:`TaskState.serialize_prefix` — byte-stable for KV-cache reuse.

    On repeated JSON-parse / validation failure, raises :class:`PlanEscalation`.
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

    async def plan(self, task: str, state: TaskState) -> list[PlanStep]:
        """Produce a validated plan for ``task`` given the current ``state``.

        Raises :class:`PlanEscalation` after 2 failed repairs (3 attempts total).
        """
        prefix = state.serialize_prefix()
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": json.dumps(prefix, ensure_ascii=False)},
            {"role": "user", "content": f"{_PLANNER_PROMPT}\n\nЗадача: {task}"},
        ]

        model = self._router.route("plan")  # Lite
        max_tokens = int(self._s.budget_plan_out)

        last_error: str | None = None
        attempts = 0
        for attempt in range(_MAX_REPAIRS + 1):
            attempts = attempt + 1
            # Build messages fresh per attempt; on retries, append the
            # validation error so the model can fix its output.
            round_messages = list(messages)
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

            # Pre-check the budget. STOP + report — never "one more call".
            # Estimate realistically: a JSON plan is small; estimating the full
            # max_tokens ceiling on every retry would prematurely exhaust the
            # phase budget. Use a third of the cap as the per-attempt reservation.
            est_in = sum(
                len(str(m.get("content", ""))) for m in round_messages
            ) // 4
            est_out = min(max_tokens, max(max_tokens // 3, 200))
            try:
                self._budgets.check("plan", est_in, est_out, task_id=state.task_id)
            except BudgetExceeded as exc:
                # Budget exhausted during planning -> graceful escalation,
                # not a hard pipeline crash (spec: "затем эскалация").
                raise PlanEscalation(f"plan budget exhausted: {exc}") from exc

            started = time.monotonic()
            result = await self._client.chat(
                model,
                round_messages,
                temperature=0.1,
                max_tokens=max_tokens,
                task_id=state.task_id,
                phase="plan",
            )
            latency_ms = int((time.monotonic() - started) * 1000)

            text = result.content or ""
            usage = result.usage or {}
            tokens_in = int(usage.get("prompt_tokens", 0) or 0)
            tokens_out = int(usage.get("completion_tokens", 0) or 0)

            # Per-task accounting (best-effort; budgets.check already committed
            # to the global "" bucket per the BudgetTracker design).
            try:
                self._budgets.commit_task(state.task_id, "plan", tokens_in, tokens_out)
                # HIGH-07: post-commit soft-overshoot guard. If actuals
                # exceeded the phase cap (estimates too low), escalate now
                # rather than letting the next check silently pass.
                self._budgets.check_overshoot("plan", task_id=state.task_id)
            except BudgetExceeded:
                raise  # propagate as a real stop (PlanEscalation handled below)
            except Exception:  # noqa: BLE001 — accounting is best-effort
                pass

            self._tracer.trace(
                state.task_id,
                step=f"plan{'_retry_' + str(attempt) if attempt > 0 else ''}",
                kind="llm",
                detail={
                    "model": model,
                    "attempt": attempts,
                    "tokens_in": tokens_in,
                    "tokens_out": tokens_out,
                    "estimated": bool(result.estimated),
                    "ok": True,  # LLM returned something; parse may still fail
                },
                tokens=tokens_in + tokens_out,
                latency_ms=latency_ms,
            )

            steps, err = parse_plan_json(text)
            if steps is not None and err is None:
                return steps
            last_error = err or "неизвестная ошибка разбора"
            continue

        # Exhausted retries — escalate.
        self._tracer.trace(
            state.task_id,
            step="plan_escalation",
            kind="decision",
            detail={
                "reason": "planner failed to produce valid JSON after retries",
                "attempts": attempts,
                "last_error": last_error,
            },
        )
        raise PlanEscalation(
            f"planner failed to produce valid JSON after {attempts} attempts; "
            f"last error: {last_error}"
        )
