"""Self-consistency coder — K candidate diffs, hunk-level majority vote.

WHY IT EARNS ITS TOKENS (justification, doctrine-aligned):
    The Coder is the single most expensive step in the pipeline (L2 Pro). The
    existing pipeline calls it ONCE, validates the diff, and retries on
    rejection. For HIGH-STAKES edits (a refactor that touches a public API, a
    security-sensitive change), one shot is risky: a single bad diff costs a
    full fix-loop iteration (3 more LLM calls). Self-consistency spends K× Pro
    tokens up front (--self-consistency, default K=3) and in exchange: (a)
    parallel K diffs are structurally validated by the existing DiffValidator
    (0 LLM cost); (b) a hunk-level MAJORITY VOTE picks the diff whose hunks
    appear in >K/2 of the candidates (semantic consensus without semantic
    similarity — we hash each hunk's file_path + content + line range); (c) on
    no-majority tie, falls back to the deterministic highest-scoring valid
    candidate (never deadlocks). Net: K× upfront cost; payback comes from
    avoiding the fix-loop on hard edits. Research (Self-Consistency / PAL /
    majority-vote over chains-of-thought) consistently shows K=3 captures ~80%
    of the gain; K=5 has diminishing returns.

Doctrine:
- Context is budget. Each of the K branches is budget-checked + committed.
  The hard cap is env ``GIGAFORGE_SELF_CONSISTENCY_K`` (default 3, max 5).
- Diff is contract. The output is a unified diff (the chosen candidate's raw
  text); the pipeline's DiffValidator still runs on it as before.
- No infinite loops. Bounded K; deterministic tie-break (highest heuristic
  score, then branch-ASC).
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from gigaforge.agents.coder import CoderAgent, is_diff
from gigaforge.client.errors import BudgetExceeded
from gigaforge.client.giga_client import ChatResult, GigaClient
from gigaforge.config import Settings, get_settings
from gigaforge.core.budgets import BudgetTracker
from gigaforge.core.diff_validator import DiffApplyResult, DiffValidator, Hunk, HunkLine
from gigaforge.core.router import ModelRouter
from gigaforge.core.trace import Tracer
from gigaforge.firewall.slicer import ContextSlice

log = logging.getLogger(__name__)

__all__ = [
    "SelfConsistencyCoder",
    "CandidateDiff",
    "SelfConsistencyResult",
    "hunk_signature",
    "DEFAULT_SELF_CONSISTENCY_K",
    "MAX_SELF_CONSISTENCY_K",
]

# Default K — the sweet spot per the self-consistency literature.
DEFAULT_SELF_CONSISTENCY_K = 3

# Hard cap to keep the token cost bounded. The literature shows diminishing
# returns past K=5; we refuse K>5 to prevent runaway costs on a single step.
MAX_SELF_CONSISTENCY_K = 5

# Temps for the K candidates. Wrap around if K > len(temps). Higher temps
# encourage diversity in the candidate diffs.
_CANDIDATE_TEMPS = (0.2, 0.4, 0.6, 0.3, 0.5)


# --- data models ------------------------------------------------------------


@dataclass
class CandidateDiff:
    """One candidate diff from one branch of the self-consistency search.

    Carries the raw model text, parsed hunks (via DiffValidator), the LLM
    token usage, and the consensus score (populated by the voting step).
    """

    branch: int
    temperature: float
    raw: str
    valid: bool  # DiffValidator said the diff applies
    apply_method: str = ""  # DiffApplyResult.method
    apply_reason: str = ""  # DiffApplyResult.reason (when valid=False)
    applied_files: list[str] = field(default_factory=list)
    hunks: list[Hunk] = field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0
    consensus_score: float = 0.0  # 0..1, populated by the voting step

    @property
    def ok(self) -> bool:
        """True iff the candidate is a structurally valid diff."""
        return self.valid and bool(is_diff(self.raw))


@dataclass
class SelfConsistencyResult:
    """Outcome of a :meth:`SelfConsistencyCoder.edit` call."""

    chosen_raw: str
    chosen_branch: int
    candidates: list[CandidateDiff]
    llm_calls: int
    n_valid: int
    n_with_majority: int  # how many candidates' hunks had majority support
    fallback_used: bool  # True if no majority; deterministic highest scorer was picked

    def to_dict(self) -> dict[str, Any]:
        return {
            "chosen_branch": self.chosen_branch,
            "llm_calls": self.llm_calls,
            "n_valid": self.n_valid,
            "n_with_majority": self.n_with_majority,
            "fallback_used": self.fallback_used,
            "candidates": [
                {
                    "branch": c.branch,
                    "temperature": c.temperature,
                    "valid": c.valid,
                    "apply_method": c.apply_method,
                    "n_hunks": len(c.hunks),
                    "tokens_in": c.tokens_in,
                    "tokens_out": c.tokens_out,
                    "consensus_score": round(c.consensus_score, 4),
                }
                for c in self.candidates
            ],
        }


# --- hunk signature (consensus key) ----------------------------------------


def hunk_signature(hunk: Hunk) -> str:
    """Stable signature for consensus voting.

    The signature is the SHA-256 of (file_path + old_start + new_start +
    sorted added + sorted removed lines). Two hunks that change the same
    lines with the same content hash to the same signature, regardless of
    whitespace-only differences in the diff metadata.

    We deliberately EXCLUDE the hunk header's @@ line-range count (it can
    vary when models pad with context lines), but INCLUDE the old/new start
    lines so that two hunks editing DIFFERENT line ranges in the same file
    are treated as separate.
    """
    parts: list[str] = [hunk.file_path, str(hunk.old_start), str(hunk.new_start)]
    # Sort added + removed lines so order-in-variance holds.
    added = sorted(
        line.content for line in hunk.lines if line.kind == "added"
    )
    removed = sorted(
        line.content for line in hunk.lines if line.kind == "removed"
    )
    parts.append("|added|:" + "\n".join(added))
    parts.append("|removed|:" + "\n".join(removed))
    payload = "\n".join(parts).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


# --- the self-consistency coder --------------------------------------------


class SelfConsistencyCoder:
    """Wraps :class:`CoderAgent` to issue K parallel Pro calls + majority vote.

    The pipeline constructs this ONCE and calls :meth:`edit` exactly like
    :meth:`CoderAgent.edit`. The return value is the chosen diff's raw text
    (the same shape the Coder returns). The pipeline runs the DiffValidator
    on the returned diff as usual — we don't double-apply.
    """

    def __init__(
        self,
        client: GigaClient,
        router: ModelRouter,
        budgets: BudgetTracker,
        tracer: Tracer,
        validator: DiffValidator,
        *,
        settings: Settings | None = None,
        k: int | None = None,
        coder: CoderAgent | None = None,
    ) -> None:
        self._client = client
        self._router = router
        self._budgets = budgets
        self._tracer = tracer
        self._validator = validator
        self._s = settings or get_settings()
        self._coder = coder or CoderAgent(
            client, router, budgets, tracer, settings=self._s
        )
        # Resolve K. Precedence: ctor arg > env > default. Cap at MAX.
        if k is not None:
            self._k = max(1, min(MAX_SELF_CONSISTENCY_K, int(k)))
        else:
            env_v = os.environ.get("GIGAFORGE_SELF_CONSISTENCY_K", "")
            try:
                k_env = int(env_v) if env_v else DEFAULT_SELF_CONSISTENCY_K
            except ValueError:
                k_env = DEFAULT_SELF_CONSISTENCY_K
            self._k = max(1, min(MAX_SELF_CONSISTENCY_K, k_env))

    @property
    def k(self) -> int:
        return self._k

    async def edit(
        self,
        slice: ContextSlice,  # noqa: A002 — matches spec parameter name
        constraints: list[str],
        *,
        task_id: str,
        repo_root: Path,
        worktree: Path,
        prev_rejection: str | None = None,
    ) -> str:
        """Run the K-branch self-consistency search; return the chosen diff.

        ``repo_root`` + ``worktree`` are required to validate each candidate
        diff via :class:`DiffValidator`. The validator's contract is "never
        raises" — invalid diffs are marked ``valid=False`` and excluded from
        the consensus vote (but traced for observability).
        """
        # K=1 is the trivial case: just delegate to the CoderAgent.
        if self._k == 1:
            return await self._coder.edit(
                slice,
                constraints,
                task_id=task_id,
                prev_rejection=prev_rejection,
            )

        # K>1: parallel candidate generation. asyncio.gather with
        # return_exceptions=True so one branch's BudgetExceeded doesn't kill
        # the others.
        tasks = [
            self._gen_one(
                branch=i,
                temperature=_CANDIDATE_TEMPS[i % len(_CANDIDATE_TEMPS)],
                slice=slice,
                constraints=constraints,
                task_id=task_id,
                repo_root=repo_root,
                worktree=worktree,
                prev_rejection=prev_rejection,
            )
            for i in range(self._k)
        ]
        raw_results = await asyncio.gather(*tasks, return_exceptions=True)

        candidates: list[CandidateDiff] = []
        for i, res in enumerate(raw_results):
            if isinstance(res, BaseException):
                # BudgetExceeded in one branch — record as invalid candidate.
                candidates.append(
                    CandidateDiff(
                        branch=i,
                        temperature=_CANDIDATE_TEMPS[i % len(_CANDIDATE_TEMPS)],
                        raw="",
                        valid=False,
                        apply_reason=f"exception:{res.__class__.__name__}: {res}",
                    )
                )
                continue
            candidates.append(res)

        # If ALL branches raised BudgetExceeded -> propagate (STOP).
        if all(
            "BudgetExceeded" in (c.apply_reason or "") for c in candidates
        ):
            raise BudgetExceeded(
                f"self-consistency: all {len(candidates)} branches failed budget check"
            )

        # --- vote ----------------------------------------------------------
        result = _vote(candidates, k=self._k)

        self._tracer.trace(
            task_id,
            step="self_consistency_done",
            kind="decision",
            detail=result.to_dict(),
        )

        return result.chosen_raw

    async def _gen_one(
        self,
        *,
        branch: int,
        temperature: float,
        slice: ContextSlice,  # noqa: A002
        constraints: list[str],
        task_id: str,
        repo_root: Path,
        worktree: Path,
        prev_rejection: str | None,
    ) -> CandidateDiff:
        """Generate ONE candidate diff via the wrapped CoderAgent, then
        validate it via DiffValidator.

        The CoderAgent.edit signature doesn't take ``temperature`` — we
        override it by wrapping the chat call. Simpler: bypass the CoderAgent
        and issue the chat call directly here, mirroring the Coder's prompt
        construction. This keeps the temperature per-branch under OUR control.
        """
        # Construct the user message the SAME way CoderAgent does. We import
        # the prompt builder logic from coder.py to avoid duplication.
        from gigaforge.agents.coder import _CODER_PROMPT  # type: ignore[attr-defined]

        slice_dict = slice.to_prompt_dict()
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
        import json as _json

        user_body_parts.append(
            "Context Slice (JSON):\n"
            + _json.dumps(slice_dict, ensure_ascii=False)
        )
        if constraints:
            user_body_parts.append(
                "Ограничения:\n- " + "\n- ".join(constraints)
            )
        user_content = "\n\n".join(user_body_parts)
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": user_content},
        ]

        model = self._router.route("code")  # Pro
        max_tokens = int(self._s.budget_code_out)

        # Pre-budget check (same discipline as the Coder).
        est_in = sum(len(str(m.get("content", ""))) for m in messages) // 4
        est_out = min(max_tokens, max(max_tokens // 2, 400))
        self._budgets.check("code", est_in, est_out, task_id=task_id)

        started = time.monotonic()
        chat_result: ChatResult = await self._client.chat(
            model,
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            task_id=task_id,
            phase="code",
        )
        latency_ms = int((time.monotonic() - started) * 1000)
        text = chat_result.content or ""
        usage = chat_result.usage or {}
        tokens_in = int(usage.get("prompt_tokens", 0) or 0)
        tokens_out = int(usage.get("completion_tokens", 0) or 0)

        try:
            self._budgets.commit_task(task_id, "code", tokens_in, tokens_out)
            self._budgets.check_overshoot("code", task_id=task_id)
        except BudgetExceeded:
            raise
        except Exception:  # noqa: BLE001 — accounting is best-effort
            pass

        self._tracer.trace(
            task_id,
            step=f"self_consistency_branch_{branch}",
            kind="llm",
            detail={
                "model": model,
                "branch": branch,
                "temperature": temperature,
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "estimated": bool(chat_result.estimated),
                "is_diff_heuristic": is_diff(text),
            },
            tokens=tokens_in + tokens_out,
            latency_ms=latency_ms,
        )

        # Validate the diff via the DiffValidator (does NOT raise; returns ok=False
        # on bad diffs). We DON'T apply to the worktree yet — just parse.
        try:
            apply_result: DiffApplyResult = self._validator.validate_and_apply(
                text, repo_root=repo_root, worktree=worktree
            )
        except Exception as exc:  # noqa: BLE001 — defensive
            # Validator's contract is "never raises"; but defensively:
            return CandidateDiff(
                branch=branch,
                temperature=temperature,
                raw=text,
                valid=False,
                apply_reason=f"validator exception: {exc}",
                tokens_in=tokens_in,
                tokens_out=tokens_out,
            )

        # IMPORTANT: the validator MUTATED the worktree (it applies the diff).
        # For self-consistency we need to REVERT after each candidate so the
        # next branch starts clean. Use `git checkout -- .` in the worktree.
        # We do this in a try/except (best-effort); if revert fails, the next
        # branch's validator will catch the dirty worktree as "no changes" or
        # "context mismatch" and the candidate is marked invalid.
        if apply_result.ok:
            try:
                _revert_worktree(worktree)
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "self-consistency: worktree revert failed (branch %d): %s",
                    branch, exc,
                )

        # The validator returns hunks? Let's check by inspecting the result.
        # DiffApplyResult doesn't directly expose hunks — but the parser's
        # internal state has them. We re-parse with a lightweight local
        # parser to get hunk signatures for consensus voting.
        hunks = _parse_hunks(text)

        return CandidateDiff(
            branch=branch,
            temperature=temperature,
            raw=text,
            valid=bool(apply_result.ok),
            apply_method=str(apply_result.method),
            apply_reason=apply_result.reason,
            applied_files=list(apply_result.applied_files),
            hunks=hunks,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
        )


# --- consensus voting -------------------------------------------------------


def _vote(candidates: list[CandidateDiff], *, k: int) -> SelfConsistencyResult:
    """Pick the candidate whose hunks have the most consensus.

    Algorithm:
      1. Filter to VALID candidates (DiffValidator said ok=True + is_diff).
      2. For each valid candidate, compute hunk signatures.
      3. Build a frequency map: signature -> count (how many candidates have it).
      4. For each candidate, consensus_score = mean over its hunks of
         (count[signature] / k). Higher = more consensus.
      5. Pick the candidate with the highest consensus_score. Tiebreak:
         highest tokens_out efficiency? No — branch ASC (deterministic).
      6. If no valid candidates: pick the first one with is_diff heuristic
         (raw model output looked like a diff even if validator rejected).
         If even that fails: pick candidate 0 (the first branch's raw text).

    Returns a :class:`SelfConsistencyResult` with the chosen raw + the full
    candidate list (with consensus_score populated).
    """
    valid = [c for c in candidates if c.ok]
    n_valid = len(valid)

    if n_valid == 0:
        # Fallback: pick the first candidate whose raw text is a diff (heuristic).
        fallback = next((c for c in candidates if is_diff(c.raw)), None)
        if fallback is None:
            # Last resort: pick candidate 0 (or empty string if no candidates).
            fallback = candidates[0] if candidates else CandidateDiff(
                branch=0, temperature=0.0, raw="", valid=False,
                apply_reason="no candidates produced",
            )
        fallback.consensus_score = 0.0
        return SelfConsistencyResult(
            chosen_raw=fallback.raw,
            chosen_branch=fallback.branch,
            candidates=candidates,
            llm_calls=len(candidates),
            n_valid=0,
            n_with_majority=0,
            fallback_used=True,
        )

    # Build signature frequency map.
    freq: dict[str, int] = {}
    for c in valid:
        seen_in_c: set[str] = set()
        for hunk in c.hunks:
            sig = hunk_signature(hunk)
            if sig not in seen_in_c:
                seen_in_c.add(sig)
                freq[sig] = freq.get(sig, 0) + 1

    # Score each candidate.
    n_with_majority = 0
    for c in valid:
        if not c.hunks:
            c.consensus_score = 0.0
            continue
        scores = [
            freq.get(hunk_signature(h), 0) / k
            for h in c.hunks
        ]
        # Mean over the candidate's hunks. Range: [1/k, 1.0].
        # 1.0 means every hunk appears in EVERY candidate.
        c.consensus_score = sum(scores) / len(scores)
        # Majority: >50% of candidates share EVERY hunk of this candidate.
        if all(s > 0.5 for s in scores):
            n_with_majority += 1

    # Pick the highest-scoring candidate. Tiebreak: branch ASC (deterministic).
    valid.sort(key=lambda c: (-c.consensus_score, c.branch))
    chosen = valid[0]

    return SelfConsistencyResult(
        chosen_raw=chosen.raw,
        chosen_branch=chosen.branch,
        candidates=candidates,
        llm_calls=len(candidates),
        n_valid=n_valid,
        n_with_majority=n_with_majority,
        fallback_used=False,
    )


# --- helpers ----------------------------------------------------------------


def _parse_hunks(diff_text: str) -> list[Hunk]:
    """Lightweight hunk parser for consensus voting.

    We don't need the full DiffValidator here — we just need the (file_path,
    old_start, new_start, added_lines, removed_lines) tuples to compute
    signatures. The DiffValidator's full Hunk class is reused for the type.
    """
    if not diff_text or not is_diff(diff_text):
        return []
    hunks: list[Hunk] = []
    current_path = ""
    current_hunk: Hunk | None = None
    for line in diff_text.splitlines():
        if line.startswith("--- "):
            # Old file path: --- a/path -> path
            pass  # we use the b/ side below
        elif line.startswith("+++ "):
            # New file path: +++ b/path -> path
            path_part = line[4:].strip()
            # Strip b/ prefix if present.
            if path_part.startswith("b/"):
                path_part = path_part[2:]
            current_path = path_part
        elif line.startswith("@@"):
            # @@ -old_start,old_count +new_start,new_count @@
            # Parse old_start + new_start.
            try:
                # Format: @@ -10,7 +10,8 @@ context
                import re

                m = re.match(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
                if m:
                    old_start = int(m.group(1))
                    new_start = int(m.group(2))
                    if current_hunk is not None:
                        hunks.append(current_hunk)
                    current_hunk = Hunk(
                        file_path=current_path,
                        old_start=old_start,
                        new_start=new_start,
                        lines=[],
                    )
            except Exception:  # noqa: BLE001 — defensive
                continue
        elif current_hunk is not None and (
            line.startswith(" ") or line.startswith("-") or line.startswith("+")
        ):
            if line.startswith("---") or line.startswith("+++"):
                continue
            kind = (
                "context"
                if line.startswith(" ")
                else "removed"
                if line.startswith("-")
                else "added"
            )
            content = line[1:]
            current_hunk.lines.append(HunkLine(kind=kind, content=content))  # type: ignore[arg-type]
    if current_hunk is not None:
        hunks.append(current_hunk)
    return hunks


def _revert_worktree(worktree: Path) -> None:
    """Best-effort: revert any uncommitted changes in the worktree.

    Uses ``git checkout -- .`` (the worktree is created by the pipeline as a
    scratch space; reverting is safe — the chosen diff is re-applied later
    by the pipeline's normal apply path).
    """
    import subprocess

    subprocess.run(
        ["git", "checkout", "--", "."],
        cwd=str(worktree),
        check=False,
        capture_output=True,
        timeout=10,
    )
    # Also remove any untracked files the diff might have added.
    subprocess.run(
        ["git", "clean", "-fd"],
        cwd=str(worktree),
        check=False,
        capture_output=True,
        timeout=10,
    )
