"""LocalizerAgent — L1 localizer that finds the target function/file for a task.

Doctrine: closes the autonomy gap flagged by the reviewer ("GigaForge can't
find relevant files itself; it requires --function/--file from the user").
The localizer runs BEFORE the planner: if the user does NOT pass
``--function``/``--file``, the localizer figures out which symbol to edit.

L0 doctrine preserved: the deterministic path uses 0 LLM calls. Only the
ambiguous-resolution path (when the top deterministic candidate is not
clearly above the runner-up) calls the Lite LLM with the repo map + top
candidates. The LLM picks ONE function and explains.

Pipeline:
    (a) Deterministic exploration (grep_relevant_files + symbol_suggestions)
        produces top-N candidate symbols ranked by query-term overlap +
        PageRank.
    (b) If confidence high (top candidate's score is significantly higher
        than the runner-up's), return WITHOUT an LLM call (0 tokens — L0).
    (c) Only if ambiguous: call the Lite model with the repo map + top
        candidates. Parse JSON. On failure, fall back to the top deterministic
        candidate (NO silent degradation — log a WARNING).

The localizer MUST NOT read whole files. It uses:
- the L0 index (signatures, callers, repo map),
- ripgrep output compressed through the Firewall (grep compressor) for
  additional context to the LLM on the ambiguous path.

Context Firewall discipline: any ripgrep output fed to the LLM goes through
``gigaforge.firewall.compress(kind="grep", ...)``. The deterministic path
emits no ripgrep output for the LLM (it uses file-level rollups only).
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from gigaforge.client.errors import BudgetExceeded
from gigaforge.client.giga_client import ChatResult, GigaClient
from gigaforge.config import Settings, get_settings
from gigaforge.core.budgets import BudgetTracker
from gigaforge.core.exploration import (
    RankedSymbol,
    run_ripgrep,
    symbol_suggestions,
    tokenize_query,
)
from gigaforge.core.router import ModelRouter
from gigaforge.core.task_state import TaskState
from gigaforge.core.trace import Tracer
from gigaforge.firewall import compress as firewall_compress
from gigaforge.l0.index import L0Index
from gigaforge.l0.repo_map import build as build_repo_map

__all__ = ["LocalizerAgent", "LocalizationResult"]

log = logging.getLogger(__name__)

# Confidence threshold: if the top candidate's score is at least this multiple
# of the runner-up's, we consider the result unambiguous and return WITHOUT an
# LLM call. Tunable via env GIGAFORGE_LOCALIZER_CONFIDENCE_RATIO (default 2.0).
_DEFAULT_CONFIDENCE_RATIO = 2.0

# Maximum candidates to consider for the LLM disambiguation prompt.
_MAX_LLM_CANDIDATES = 8

# Maximum candidates to keep in the alternatives list of LocalizationResult.
_MAX_ALTERNATIVES = 5

# ripgrep output budget (tokens) when feeding the LLM on the ambiguous path.
_GREP_LLM_BUDGET_TOKENS = 1200

# Repo map budget (tokens) for the LLM prompt.
_REPO_MAP_LLM_BUDGET_TOKENS = 1500


@dataclass(frozen=True)
class LocalizationResult:
    """The localizer's verdict — what the pipeline should target.

    Fields:
    - ``target_function``: the qname (or short name) of the symbol to edit.
    - ``target_file``: repo-relative path of the file containing the symbol.
    - ``confidence``: 0.0 to 1.0 — how confident the localizer is. >= 0.8
      means the deterministic path returned; < 0.5 means the LLM was called
      but failed and we fell back to the top deterministic candidate.
    - ``reasoning``: short human-readable explanation (used by the pipeline
      for tracing and the REPL for display).
    - ``alternatives``: list of (qname, score) tuples for the runner-ups.
      Empty if only one candidate was found.
    """

    target_function: str
    target_file: Path
    confidence: float
    reasoning: str
    alternatives: list[tuple[str, float]] = field(default_factory=list)
    llm_calls: int = 0  # number of LLM calls made (0 on the deterministic path)


# Spec for the Lite model's prompt — ask for ONE function + a reason.
_LOCALIZER_PROMPT = (
    "Ты — локализатор. Твоя задача — выбрать ОДНУ функцию для редактирования.\n"
    "Верни ТОЛЬКО JSON-объект вида:\n"
    '{"function": "<qualified_name>", "reasoning": "<одна строка обоснования>"}\n'
    "Поле \"function\" должно содержать РОВНО одно значение из списка кандидатов.\n"
    "Не пиши ничего вне JSON. Никакого markdown."
)


class LocalizerAgent:
    """L1 localizer — finds the target function/file for a task.

    Constructed with the same collaborators as the other agents (client,
    router, budgets, tracer, settings). The deterministic path (high
    confidence) makes ZERO LLM calls. The ambiguous-resolution path makes
    exactly ONE LLM call to the Lite model (temperature 0).
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

    async def localize(
        self,
        task: str,
        repo_root: Path,
        index: L0Index,
        state: TaskState,
        *,
        confidence_ratio: float = _DEFAULT_CONFIDENCE_RATIO,
    ) -> LocalizationResult:
        """Localize the target function/file for ``task``.

        Returns a :class:`LocalizationResult`. On the deterministic path,
        ``llm_calls == 0`` (the L0 doctrine). On the ambiguous path,
        ``llm_calls == 1`` and the Lite model picked the candidate.

        If the LLM call fails (parse error, network, budget), we fall back
        to the top deterministic candidate — NO silent degradation; the
        fallback is logged at WARNING and the confidence is marked low (0.4).
        """
        repo_root = Path(repo_root)

        # (a) Deterministic exploration: top-N candidate symbols.
        candidates = symbol_suggestions(index, task, repo_root=repo_root, top_n=10)
        self._tracer.trace(
            state.task_id,
            step="localize_explore",
            kind="l0",
            detail={
                "n_candidates": len(candidates),
                "top": candidates[0].qname if candidates else None,
                "top_score": candidates[0].score if candidates else 0.0,
            },
        )

        if not candidates:
            # Nothing in the index. Return an empty result; the pipeline
            # will fail at the slicer step with a clear "no symbols indexed"
            # message rather than calling the LLM with no candidates.
            self._tracer.trace(
                state.task_id,
                step="localize_no_candidates",
                kind="decision",
                detail={"reason": "no symbols in L0 index"},
            )
            return LocalizationResult(
                target_function="",
                target_file=Path(""),
                confidence=0.0,
                reasoning="no symbols indexed — cannot localize",
                alternatives=[],
                llm_calls=0,
            )

        top = candidates[0]
        second = candidates[1] if len(candidates) > 1 else None

        # (b) High confidence: top candidate >> runner-up.
        # We treat "ambiguous" as: ratio(top, second) < confidence_ratio
        # AND there are at least 2 candidates with non-zero term overlap
        # (otherwise the runner-up is just a low-rank symbol that happens
        # to exist in the index, not a real alternative).
        if second is None:
            confidence = 0.95
            return self._build_deterministic_result(
                top, candidates, confidence,
                "single candidate in L0 index",
            )

        # If the top candidate has term overlap > 0 AND the runner-up has
        # term overlap == 0, the runner-up is just a graph-central symbol
        # that didn't match the query — not a real ambiguity.
        top_decisive = top.term_overlap > 0 and second.term_overlap == 0
        if top_decisive:
            confidence = 0.9
            return self._build_deterministic_result(
                top, candidates, confidence,
                f"top candidate has term overlap (={top.term_overlap}), "
                f"runner-up has none; decisive match",
            )

        # EXACT name match: if the task text contains the exact symbol name
        # (e.g. "Fix validate_email to ..."), it's decisive — skip the LLM
        # entirely. This is the strongest L0 signal: the user named the target.
        task_lower = task.lower()
        exact_match = next(
            (c for c in candidates if c.qname.split(".")[-1].lower() in task_lower
             and c.term_overlap > 0),
            None,
        )
        if exact_match is not None and exact_match.qname == top.qname:
            confidence = 0.95
            return self._build_deterministic_result(
                top, candidates, confidence,
                f"task text contains the exact symbol name {top.qname!r}; "
                "decisive exact match (0 LLM calls)",
            )

        ratio = top.score / max(second.score, 1e-9)
        if ratio >= confidence_ratio:
            confidence = min(0.95, 0.5 + 0.45 * (1 - 1 / ratio))
            return self._build_deterministic_result(
                top, candidates, confidence,
                f"top candidate score {top.score:.2f} >= "
                f"{confidence_ratio:.1f}× runner-up {second.score:.2f}",
            )

        # (c) Ambiguous: call the Lite LLM.
        self._tracer.trace(
            state.task_id,
            step="localize_ambiguous",
            kind="decision",
            detail={
                "top": top.qname,
                "top_score": top.score,
                "second": second.qname,
                "second_score": second.score,
                "ratio": round(ratio, 4),
            },
        )
        try:
            return await self._llm_disambiguate(
                task, candidates, repo_root, index, state,
            )
        except BudgetExceeded:
            raise  # STOP — propagate
        except Exception as exc:  # noqa: BLE001 — fall back deterministically
            log.warning(
                "localizer: LLM disambiguation failed (%s: %s); "
                "falling back to top deterministic candidate %s",
                exc.__class__.__name__, exc, top.qname,
            )
            self._tracer.trace(
                state.task_id,
                step="localize_llm_fallback",
                kind="decision",
                detail={
                    "reason": f"LLM failed: {exc.__class__.__name__}: {exc}",
                    "fallback": top.qname,
                },
            )
            return LocalizationResult(
                target_function=top.qname,
                target_file=Path(top.path),
                confidence=0.4,
                reasoning=(
                    f"LLM disambiguation failed ({exc.__class__.__name__}); "
                    f"fell back to top deterministic candidate "
                    f"{top.qname} (score={top.score:.2f})"
                ),
                alternatives=self._alternatives(candidates),
                llm_calls=0,
            )

    # ----------------------------------------------------------- helpers

    def _build_deterministic_result(
        self,
        top: RankedSymbol,
        candidates: list[RankedSymbol],
        confidence: float,
        reason: str,
    ) -> LocalizationResult:
        return LocalizationResult(
            target_function=top.qname,
            target_file=Path(top.path),
            confidence=round(confidence, 4),
            reasoning=(
                f"deterministic top candidate {top.qname} "
                f"(score={top.score:.2f}, term_overlap={top.term_overlap}, "
                f"pagerank={top.pagerank:.4f}); {reason}"
            ),
            alternatives=self._alternatives(candidates),
            llm_calls=0,
        )

    @staticmethod
    def _alternatives(
        candidates: list[RankedSymbol],
    ) -> list[tuple[str, float]]:
        return [(c.qname, round(c.score, 4)) for c in candidates[1:1 + _MAX_ALTERNATIVES]]

    async def _llm_disambiguate(
        self,
        task: str,
        candidates: list[RankedSymbol],
        repo_root: Path,
        index: L0Index,
        state: TaskState,
    ) -> LocalizationResult:
        """Call the Lite LLM with the repo map + top candidates.

        Returns the LLM's pick (parsed from JSON). On parse failure or other
        error, falls back to the top deterministic candidate (logged).
        """
        # Build the prompt: repo map + candidate list + task.
        repo_map = build_repo_map(repo_root, index)
        repo_map_text = repo_map.to_text(max_tokens=_REPO_MAP_LLM_BUDGET_TOKENS)

        # Build candidate summary (names + signatures + paths + scores).
        candidate_lines: list[str] = []
        for i, c in enumerate(candidates[:_MAX_LLM_CANDIDATES], 1):
            candidate_lines.append(
                f"{i}. {c.qname}  (score={c.score:.2f}, "
                f"term_overlap={c.term_overlap}, refs={c.refs})\n"
                f"   file: {c.path}\n"
                f"   signature: {c.signature}"
            )
        candidates_text = "\n".join(candidate_lines)

        # Optional: run ripgrep over the repo for additional context,
        # compressed through the Firewall. The LLM sees the structural JSON
        # (file:line:symbol:content per match, capped to ~1200 tokens), NEVER
        # raw file bodies. This is the L0 discipline: no whole-file reads.
        grep_compressed: dict[str, Any] | None = None
        try:
            terms = tokenize_query(task)
            if terms:
                raw_grep = run_ripgrep(task, repo_root)
                if raw_grep:
                    grep_compressed = firewall_compress(
                        "grep", raw_grep,
                        limit_tokens=_GREP_LLM_BUDGET_TOKENS,
                        target_symbol=candidates[0].qname if candidates else None,
                    )
        except Exception as exc:  # noqa: BLE001 — best-effort context
            log.debug("localizer: grep context failed (non-fatal): %s", exc)

        user_parts: list[str] = [
            _LOCALIZER_PROMPT,
            f"Задача: {task}",
            "Кандидаты (ранжированы по релевантности):\n" + candidates_text,
        ]
        if grep_compressed:
            user_parts.append(
                "Сжатый вывод grep (Firewall, не более "
                f"{_GREP_LLM_BUDGET_TOKENS} токенов):\n"
                + grep_compressed.text
            )
        user_parts.append("Карта репозитория (L0):\n" + repo_map_text)
        user_content = "\n\n".join(user_parts)

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": json.dumps(state.serialize_prefix(), ensure_ascii=False)},
            {"role": "user", "content": user_content},
        ]

        model = self._router.route("classify")  # Lite
        max_tokens = int(self._s.budget_plan_out)

        # Budget estimate: prompt + a small completion.
        est_in = sum(len(str(m.get("content", ""))) for m in messages) // 4
        est_out = min(max_tokens, max(max_tokens // 3, 200))
        claim = self._budgets.reserve("classify", est_in, est_out, task_id=state.task_id)

        started = time.monotonic()
        try:
            result: ChatResult = await self._client.chat(
                model,
                messages,
                temperature=0.0,  # L1, deterministic
                max_tokens=max_tokens,
                task_id=state.task_id,
                phase="classify",
            )
        except BaseException:
            claim.cancel()
            raise
        latency_ms = int((time.monotonic() - started) * 1000)
        text = result.content or ""
        from gigaforge.client.types import usage_tokens
        tokens_in, tokens_out = usage_tokens(result.usage)

        try:
            claim.settle(tokens_in, tokens_out)
            self._budgets.check_overshoot("classify", task_id=state.task_id)
        except BudgetExceeded:
            raise
        except Exception:  # noqa: BLE001 — accounting is best-effort
            pass

        self._tracer.trace(
            state.task_id,
            step="localize_llm",
            kind="llm",
            detail={
                "model": model,
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "estimated": bool(result.estimated),
                "candidates_n": len(candidates),
            },
            tokens=tokens_in + tokens_out,
            latency_ms=latency_ms,
        )

        # Parse the LLM's JSON response: {"function": "<qname>", "reasoning": "..."}.
        picked_qname, picked_reasoning, parse_err = _parse_localizer_json(text)
        if picked_qname is None:
            log.warning(
                "localizer: LLM returned unparseable JSON (%s); "
                "falling back to top deterministic candidate %s",
                parse_err, candidates[0].qname,
            )
            self._tracer.trace(
                state.task_id,
                step="localize_llm_parse_fail",
                kind="decision",
                detail={
                    "reason": parse_err,
                    "raw_head": text[:300],
                    "fallback": candidates[0].qname,
                },
            )
            top = candidates[0]
            return LocalizationResult(
                target_function=top.qname,
                target_file=Path(top.path),
                confidence=0.4,
                reasoning=(
                    f"LLM returned unparseable JSON ({parse_err}); "
                    f"fell back to top deterministic candidate {top.qname} "
                    f"(score={top.score:.2f})"
                ),
                alternatives=self._alternatives(candidates),
                llm_calls=1,
            )

        # Validate the picked qname is in our candidate list (defense in
        # depth — the LLM may hallucinate a name that isn't in the index).
        picked = next((c for c in candidates if c.qname == picked_qname), None)
        if picked is None:
            log.warning(
                "localizer: LLM picked %s which is NOT in the candidate list; "
                "falling back to top deterministic candidate %s",
                picked_qname, candidates[0].qname,
            )
            self._tracer.trace(
                state.task_id,
                step="localize_llm_hallucination",
                kind="decision",
                detail={
                    "picked": picked_qname,
                    "fallback": candidates[0].qname,
                    "reasoning": picked_reasoning,
                },
            )
            top = candidates[0]
            return LocalizationResult(
                target_function=top.qname,
                target_file=Path(top.path),
                confidence=0.5,
                reasoning=(
                    f"LLM picked {picked_qname!r} (not in candidate list); "
                    f"fell back to top deterministic candidate {top.qname} "
                    f"(score={top.score:.2f}). LLM reasoning: {picked_reasoning}"
                ),
                alternatives=self._alternatives(candidates),
                llm_calls=1,
            )

        # LLM successfully picked a candidate.
        self._tracer.trace(
            state.task_id,
            step="localize_llm_picked",
            kind="decision",
            detail={
                "picked": picked.qname,
                "picked_score": picked.score,
                "reasoning": picked_reasoning,
            },
        )
        return LocalizationResult(
            target_function=picked.qname,
            target_file=Path(picked.path),
            confidence=0.75,
            reasoning=(
                f"LLM (Lite) picked {picked.qname} "
                f"(deterministic score={picked.score:.2f}). "
                f"LLM reasoning: {picked_reasoning}"
            ),
            alternatives=self._alternatives(candidates),
            llm_calls=1,
        )


def _parse_localizer_json(
    text: str,
) -> tuple[str | None, str, str | None]:
    """Parse the LLM's JSON response.

    Returns ``(qname, reasoning, error)``. On success, ``error is None``.
    On failure, ``qname is None`` and ``error`` is a short reason.
    """
    if not text or not text.strip():
        return None, "", "empty response"
    s = text.strip()
    # Strip ```json / ``` fences if present.
    if s.startswith("```"):
        lines = s.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        s = "\n".join(lines).strip()
    # Extract outermost { ... } block.
    first = s.find("{")
    last = s.rfind("}")
    if first == -1 or last == -1 or last < first:
        return None, "", "no JSON object found"
    s = s[first:last + 1]
    try:
        data = json.loads(s)
    except json.JSONDecodeError as exc:
        return None, "", f"JSON decode error: {exc.msg}"
    if not isinstance(data, dict):
        return None, "", "expected a JSON object at top level"
    qname = data.get("function")
    if not isinstance(qname, str) or not qname.strip():
        return None, "", "field 'function' missing or not a string"
    reasoning = data.get("reasoning", "")
    if not isinstance(reasoning, str):
        reasoning = str(reasoning)
    return qname.strip(), reasoning.strip(), None
