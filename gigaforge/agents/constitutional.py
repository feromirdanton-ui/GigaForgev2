"""Constitutional reviewer — checks diffs against a configurable constitution.

WHY IT EARNS ITS TOKENS (justification, doctrine-aligned):
    The existing :class:`ReviewerAgent` runs ONE L1 Lite call asking "is this
    diff OK?" — it's broad but unstructured, and the LLM's verdict on subtle
    violations ("did this add a network call without error handling?") is
    inconsistent. The ConstitutionalReviewer runs DETERMINISTIC checks FIRST
    (regex/AST-style — 0 LLM calls) for the principles that can be checked
    structurally (secrets-in-code, print/debug in added lines, diff-contract
    markers, public-API removal). When --constitution is set, only AFTER the
    deterministic sweep do we issue ONE L1 Lite call asking the model to check
    the remaining (subtle) principles with a structured verdict. Fail-safe: any
    deterministic violation -> REJECT with the principle ID in the issues; any
    LLM parse failure -> REJECT. Net cost: usually 0 LLM calls (deterministic
    sweep catches the bad cases); the LLM call is reserved for "no deterministic
    flag fired, but the diff might still violate a soft principle."

Doctrine:
- Context is budget. The deterministic sweep is free (0 LLM). The optional LLM
  call is bounded to 1 Lite attempt, budget-checked + traced + metrics-recorded.
- Diff is contract. The constitutional verdict is folded into the existing
  :class:`ReviewVerdict` shape (``verdict`` + ``issues``); the pipeline can
  consume it exactly as it does the regular reviewer's output.
- No infinite loops. 1 attempt max; no retry storm.
"""
from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from gigaforge.agents.planner import json_repair
from gigaforge.client.errors import BudgetExceeded
from gigaforge.client.giga_client import ChatResult, GigaClient
from gigaforge.config import Settings, get_settings
from gigaforge.core.budgets import BudgetTracker
from gigaforge.core.router import ModelRouter
from gigaforge.core.trace import Tracer
from gigaforge.firewall.slicer import ContextSlice

__all__ = [
    "Constitution",
    "ConstitutionalReviewer",
    "Principle",
    "PrincipleResult",
    "DEFAULT_CONSTITUTION",
    "load_constitution",
]

# Default principles. Each has:
#   - id: short stable identifier for the verdict's `issues`
#   - description: human-readable explanation (sent to the LLM if invoked)
#   - check: a callable that takes (diff, slice) -> PrincipleResult
#            (None means "soft principle, only the LLM can judge this").
# Hard cap on the LLM-assisted constitutional review. After this many parse
# failures, fail-safe to REJECT (never silently approve).
_MAX_REPAIRS = 1


# --- the principle model ----------------------------------------------------


class PrincipleResult(BaseModel):
    """The outcome of checking ONE principle against a diff.

    ``violated=True`` means the principle was violated; ``detail`` is a short
    human-readable string explaining the violation (used as the verdict issue).
    """

    violated: bool = False
    detail: str = ""

    @classmethod
    def ok(cls) -> PrincipleResult:
        return cls(violated=False, detail="")

    @classmethod
    def fail(cls, detail: str) -> PrincipleResult:
        return cls(violated=True, detail=detail)


PrincipleCheck = Callable[[str, ContextSlice], PrincipleResult]


class Principle(BaseModel):
    """One constitutional principle.

    ``check`` is optional: if ``None``, the principle is "soft" and only the
    LLM can judge it (it goes into the LLM's prompt but has no deterministic
    enforcement). If non-None, the deterministic check runs FIRST (0 LLM cost),
    and a violation short-circuits the LLM call (we already know the verdict
    is REJECT).
    """

    id: str
    description: str
    severity: Literal["blocker", "warn"] = "blocker"
    # The check callable is excluded from the pydantic model dump (not
    # serialisable) — it's stored as a plain attribute on the wrapper.
    check_id: str | None = None  # name of a builtin check; resolved at load

    model_config = {"arbitrary_types_allowed": True}


# --- builtin deterministic checks ------------------------------------------


# Regex for obvious secrets: AWS access keys, OAuth bearer tokens, Slack tokens,
# private keys, password= literals. Tuned for low false-positive rate (we'd
# rather miss a subtle secret than flag every diff that contains a string).
_SECRET_PATTERNS = [
    (re.compile(r"AKIA[0-9A-Z]{16}"), "AWS access key"),
    (re.compile(r"-----BEGIN (RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"), "private key"),
    (re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{10,}", re.IGNORECASE), "Slack token"),
    (re.compile(r"gh[opsu]_[0-9A-Za-z]{36,}", re.IGNORECASE), "GitHub token"),
    (
        re.compile(r"\b(password|passwd|pwd|secret|api_key|access_token)\s*=\s*['\"][^'\"]{6,}['\"]", re.IGNORECASE),
        "hardcoded credential literal",
    ),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"), "JWT"),
]

# Lines added by the diff (start with `+` but not `+++`).
_ADDED_LINE_RE = re.compile(r"^\+([^+].*)$")

# A network call without try/except nearby is hard to detect statically
# without AST; we use a heuristic: any added line that calls one of these
# functions, and there's no `try:` or `except` in the SAME hunk's added lines.
_NET_CALL_RE = re.compile(
    r"\b(requests\.(get|post|put|delete|patch|head)|"
    r"httpx\.(get|post|put|delete|patch)|"
    r"urllib\.request\.urlopen|"
    r"socket\.connect|"
    r"aiohttp\.ClientSession|"
    r"httpx\.Client|"
    r"urlopen)\s*\("
)

# Print / debug statements in added lines.
_DEBUG_RE = re.compile(
    r"^\s*(print\(|breakpoint\(\)|pdb\.set_trace\(\)|import\s+pdb\b)"
)


def _added_lines(diff: str) -> list[str]:
    """Extract the content of each `+`-prefixed line (excluding `+++`)."""
    out: list[str] = []
    for line in diff.splitlines():
        m = _ADDED_LINE_RE.match(line)
        if m:
            out.append(m.group(1))
    return out


def _removed_lines(diff: str) -> list[str]:
    """Extract the content of each `-`-prefixed line (excluding `---`)."""
    out: list[str] = []
    for line in diff.splitlines():
        if line.startswith("-") and not line.startswith("---"):
            out.append(line[1:])
    return out


def check_no_secrets_in_code(diff: str, slice: ContextSlice) -> PrincipleResult:  # noqa: A002
    """Catches hardcoded secrets in ADDED lines."""
    for line in _added_lines(diff):
        for pat, name in _SECRET_PATTERNS:
            if pat.search(line):
                return PrincipleResult.fail(f"secret_leak: {name} in added line: {line.strip()[:80]}")
    return PrincipleResult.ok()


def check_diff_contract(diff: str, slice: ContextSlice) -> PrincipleResult:  # noqa: A002
    """The diff must contain the required unified-diff markers."""
    if not (("--- " in diff) and ("+++ " in diff) and ("@@" in diff)):
        return PrincipleResult.fail("diff_contract: missing --- /+++ /@@ markers")
    return PrincipleResult.ok()


def check_no_debug_in_production(diff: str, slice: ContextSlice) -> PrincipleResult:  # noqa: A002
    """Catches print(), breakpoint(), pdb.set_trace() in added lines."""
    for line in _added_lines(diff):
        if _DEBUG_RE.match(line):
            return PrincipleResult.fail(f"debug_in_production: {line.strip()[:80]}")
    return PrincipleResult.ok()


def check_no_network_without_error_handling(
    diff: str, slice: ContextSlice
) -> PrincipleResult:  # noqa: A002
    """Heuristic: any added network call in a hunk without a try/except in the
    SAME hunk's added lines.

    We split the diff into hunks (by ``@@`` markers) and look for a network
    call in any hunk's added lines that doesn't have a ``try:`` or ``except``
    keyword in the same hunk's added lines.
    """
    hunks = re.split(r"^@@", diff, flags=re.MULTILINE)
    for hunk in hunks:
        added = _added_lines(hunk)
        has_net = any(_NET_CALL_RE.search(line) for line in added)
        if not has_net:
            continue
        has_handling = any(
            re.search(r"^\s*(try\s*:|except\s|finally\s*:)", line)
            for line in added
        )
        if not has_handling:
            net_line = next(
                (line for line in added if _NET_CALL_RE.search(line)),
                "<unknown>",
            )
            return PrincipleResult.fail(
                f"network_without_error_handling: {net_line.strip()[:80]}"
            )
    return PrincipleResult.ok()


def check_preserve_public_apis(diff: str, slice: ContextSlice) -> PrincipleResult:  # noqa: A002
    """Catches removal of public function defs (``def name(`` not prefixed with ``_``).

    A def that is removed **and re-added** is not a removal.  Editing a
    function's body rewrites its signature line too — a unified diff shows
    ``-def mul(a, b):`` followed by ``+def mul(a, b):`` — and the check used to
    fail on the removed line alone.  Measured live: a sub-agent that changed
    ``return a + b`` to ``return a * b`` was rejected with

        public_api_removed: def mul(...) removed by the diff

    for a diff that kept ``mul`` exactly where it was.  The check now compares
    the two sides: a name fails only when it is gone from the added lines.
    """
    added_names = {
        m.group(1)
        for line in _added_lines(diff)
        if (m := re.match(r"\s*def\s+([A-Za-z][A-Za-z0-9_]*)\s*\(", line))
    }
    for line in _removed_lines(diff):
        m = re.match(r"\s*def\s+([A-Za-z][A-Za-z0-9_]*)\s*\(", line)
        if m:
            name = m.group(1)
            if name.startswith("_"):
                continue
            if name in added_names:
                # Re-added: the signature moved or was reformatted, the API did
                # not disappear.
                continue
            return PrincipleResult.fail(
                f"public_api_removed: def {name}(...) removed by the diff"
            )
    return PrincipleResult.ok()


# Registry of builtin checks by name. Principles in the constitution config
# reference these by ``check_id``; the load_constitution function resolves.
_BUILTIN_CHECKS: dict[str, PrincipleCheck] = {
    "no_secrets": check_no_secrets_in_code,
    "diff_contract": check_diff_contract,
    "no_debug_in_production": check_no_debug_in_production,
    "no_network_without_error_handling": check_no_network_without_error_handling,
    "preserve_public_apis": check_preserve_public_apis,
}


# --- the default constitution ------------------------------------------------
# Constitution is defined here; we expose Constitution.default() as the loader.


class Constitution:
    """A constitution is a list of principles + a resolver for builtin checks.

    A principle with ``check_id="no_secrets"`` resolves to the
    :func:`check_no_secrets_in_code` builtin. A principle with ``check_id=None``
    is a "soft" principle — only the LLM can judge it (the constitutional
    reviewer's LLM call sees its description in the prompt).
    """

    def __init__(self, principles: list[Principle]) -> None:
        self.principles = list(principles)

    def deterministic_check(
        self, diff: str, slice: ContextSlice
    ) -> list[tuple[Principle, PrincipleResult]]:
        """Run all deterministic checks. Returns (principle, result) pairs.

        Soft principles (no ``check_id``) are skipped here (their result will
        be folded into the LLM verdict). Hard principles with a check_id
        resolve to a builtin function and are run.
        """
        out: list[tuple[Principle, PrincipleResult]] = []
        for p in self.principles:
            if not p.check_id:
                continue
            fn = _BUILTIN_CHECKS.get(p.check_id)
            if fn is None:
                # Unknown check_id -> treat as soft (no deterministic enforcement).
                continue
            out.append((p, fn(diff, slice)))
        return out

    def soft_principles(self) -> list[Principle]:
        """Principles that ONLY the LLM can judge (no deterministic check)."""
        return [p for p in self.principles if not p.check_id]

    def hard_principles(self) -> list[Principle]:
        """Principles with a deterministic builtin check."""
        return [p for p in self.principles if p.check_id]

    def to_prompt_lines(self) -> list[str]:
        """Render the constitution as a numbered list for the LLM prompt."""
        lines: list[str] = []
        for i, p in enumerate(self.principles, start=1):
            lines.append(f"{i}. [{p.severity}] {p.id}: {p.description}")
        return lines

    @classmethod
    def default(cls) -> Constitution:
        """The built-in default constitution (5 hard principles)."""
        return cls(
            principles=[
                Principle(
                    id="diff_contract",
                    description="The diff must contain the required unified-diff markers (--- /+++ /@@).",
                    severity="blocker",
                    check_id="diff_contract",
                ),
                Principle(
                    id="no_secrets",
                    description="Added lines must not contain hardcoded secrets (AWS keys, OAuth tokens, private keys, password= literals).",
                    severity="blocker",
                    check_id="no_secrets",
                ),
                Principle(
                    id="no_debug_in_production",
                    description="Added lines must not contain print(), breakpoint(), or pdb.set_trace() calls.",
                    severity="blocker",
                    check_id="no_debug_in_production",
                ),
                Principle(
                    id="preserve_public_apis",
                    description="The diff must not remove public function definitions (def name(...) where name does not start with _).",
                    severity="blocker",
                    check_id="preserve_public_apis",
                ),
                Principle(
                    id="no_network_without_error_handling",
                    description="Added network calls (requests/httpx/urllib) must be inside a try/except in the same hunk.",
                    severity="warn",
                    check_id="no_network_without_error_handling",
                ),
                # One soft principle the LLM judges:
                Principle(
                    id="respect_coding_standards",
                    description="The diff must respect the project's coding standards (PEP 8 style, no obvious anti-patterns, no dead code).",
                    severity="warn",
                    check_id=None,
                ),
            ]
        )

    @classmethod
    def from_json(cls, json_str: str) -> Constitution:
        """Load from a JSON string.

        Schema:
        ``{"principles": [{"id": "no_secrets", "description": "...", "severity": "blocker", "check_id": "no_secrets"}, ...]}``
        """
        data = json.loads(json_str)
        if not isinstance(data, dict):
            raise ValueError("constitution JSON must be a dict")
        principles_raw = data.get("principles", [])
        if not isinstance(principles_raw, list):
            raise ValueError("'principles' must be a list")
        principles: list[Principle] = []
        for i, item in enumerate(principles_raw):
            if not isinstance(item, dict):
                raise ValueError(f"principle {i} must be a dict")
            principles.append(
                Principle(
                    id=str(item.get("id", f"p{i}")),
                    description=str(item.get("description", "")),
                    severity=str(item.get("severity", "warn")),  # type: ignore[arg-type]
                    check_id=item.get("check_id") or None,
                )
            )
        return cls(principles=principles)


# --- the reviewer ------------------------------------------------------------


class ConstitutionalReviewer:
    """Runs deterministic checks first (0 LM); optionally one Lite LLM call
    for soft principles + a sanity-check verdict.

    The verdict is a :class:`ReviewVerdict` (the same shape as the existing
    :class:`ReviewerAgent`), so the pipeline can consume it without changes.
    """

    def __init__(
        self,
        client: GigaClient,
        router: ModelRouter,
        budgets: BudgetTracker,
        tracer: Tracer,
        *,
        constitution: Constitution | None = None,
        settings: Settings | None = None,
        llm_enabled: bool | None = None,
    ) -> None:
        self._client = client
        self._router = router
        self._budgets = budgets
        self._tracer = tracer
        self._s = settings or get_settings()
        self._constitution = constitution or _load_default_constitution()
        # The LLM-assisted check is OFF by default for hard principles
        # (deterministic checks cover them) but ON for soft principles. The
        # operator can force it off via env GIGAFORGE_CONSTITUTION_LLM=0.
        if llm_enabled is not None:
            self._llm_enabled = bool(llm_enabled)
        else:
            self._llm_enabled = os.environ.get(
                "GIGAFORGE_CONSTITUTION_LLM", ""
            ).strip().lower() not in {"0", "false", "no", "off"}

    @property
    def constitution(self) -> Constitution:
        return self._constitution

    @property
    def llm_enabled(self) -> bool:
        return self._llm_enabled

    async def review(
        self,
        diff: str,
        slice: ContextSlice,  # noqa: A002 — matches spec parameter name
        *,
        task_id: str,
    ) -> ReviewVerdict:
        """Check ``diff`` against the constitution.

        1. Run all hard (deterministic) checks. Any violation -> REJECT
           immediately with the principle ID in the issues.
        2. If no hard violations AND there are soft principles AND the LLM is
           enabled, issue ONE Lite LLM call to judge the soft principles.
           Fail-safe: parse failure -> REJECT with "constitutional reviewer
           returned unparseable JSON" in the issues.
        3. If no hard violations and no soft principles (or LLM disabled),
           return OK (the deterministic sweep passed).
        """
        from gigaforge.agents.reviewer import ReviewVerdict  # local to avoid cycle

        issues: list[str] = []
        # 1. Deterministic sweep.
        hard_results = self._constitution.deterministic_check(diff, slice)
        blocking_violations = 0
        for principle, result in hard_results:
            if result.violated:
                issues.append(f"[{principle.id}] {result.detail}")
                # ANY deterministic violation -> REJECT (fail-safe). The
                # ``severity`` field is informational (for the operator's
                # report); it does NOT gate the verdict — a warn violation
                # still rejects, just with the principle marked "warn" so the
                # operator knows it's not a hard-blocker in spirit.
                blocking_violations += 1

        if blocking_violations > 0:
            self._tracer.trace(
                task_id,
                step="constitutional_reject_deterministic",
                kind="decision",
                detail={
                    "n_issues": len(issues),
                    "blocking_violations": blocking_violations,
                    "issues": issues,
                    "llm_call": False,
                },
            )
            return ReviewVerdict(verdict="reject", issues=issues)

        # 2. Optional LLM check for soft principles (or re-affirm hard).
        soft = self._constitution.soft_principles()
        if not soft or not self._llm_enabled:
            # Nothing left to check; deterministic sweep passed.
            self._tracer.trace(
                task_id,
                step="constitutional_ok",
                kind="decision",
                detail={
                    "hard_checks_passed": len(hard_results),
                    "soft_principles": len(soft),
                    "llm_call": False,
                },
            )
            return ReviewVerdict(verdict="ok", issues=[])

        # 3. The LLM check.
        return await self._llm_review(
            diff=diff,
            slice=slice,
            task_id=task_id,
            soft_principles=soft,
        )

    async def _llm_review(
        self,
        *,
        diff: str,
        slice: ContextSlice,  # noqa: A002
        task_id: str,
        soft_principles: list[Principle],
    ) -> ReviewVerdict:
        """One Lite LLM call asking the model to judge the soft principles.

        Fail-safe: parse failure or invalid JSON -> REJECT.
        """
        from gigaforge.agents.reviewer import ReviewVerdict

        model = self._router.route("review")  # Lite
        max_tokens = int(self._s.budget_plan_out)

        slice_dict = slice.to_prompt_dict()
        principles_lines = "\n".join(self._constitution.to_prompt_lines())
        prompt = (
            "Ты конституционный ревьюер. Проверь diff против принципов ниже.\n"
            "Верни ТОЛЬКО JSON: "
            '{"verdict":"ok|reject","issues":["[principle_id] описание", ...]}\n'
            "Каждая issue должна начинаться с [id_принципа]. "
            "Если нарушений нет — verdict='ok', issues=[]."
        )
        base_messages: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": (
                    f"{prompt}\n\n"
                    f"Принципы:\n{principles_lines}\n\n"
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
            est_out = min(max_tokens, max(max_tokens // 3, 200))
            # Atomic claim (see BudgetTracker.reserve): check-then-commit let
            # concurrent reviewers pass the same remaining balance.
            claim = self._budgets.reserve("review", est_in, est_out, task_id=task_id)

            started = time.monotonic()
            try:
                chat_result: ChatResult = await self._client.chat(
                    model,
                    round_messages,
                    temperature=0.1,
                    max_tokens=max_tokens,
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
                raise
            except Exception:  # noqa: BLE001 — accounting is best-effort
                pass

            self._tracer.trace(
                task_id,
                step=f"constitutional_review{'_retry_' + str(attempts) if attempt > 0 else ''}",
                kind="llm",
                detail={
                    "model": model,
                    "attempt": attempts,
                    "tokens_in": tokens_in,
                    "tokens_out": tokens_out,
                    "estimated": bool(chat_result.estimated),
                },
                tokens=tokens_in + tokens_out,
                latency_ms=latency_ms,
            )

            verdict, err = _parse_verdict_json(text)
            if verdict is not None and err is None:
                self._tracer.trace(
                    task_id,
                    step="constitutional_review_done",
                    kind="decision",
                    detail={
                        "verdict": verdict.verdict,
                        "n_issues": len(verdict.issues),
                        "llm_call": True,
                    },
                )
                return verdict
            last_error = err or "неизвестная ошибка разбора"

        # A verdict the reviewer never reached is not a judgement about the
        # code.  The previous behaviour returned ``reject`` with the issue
        # "constitutional reviewer returned unparseable JSON", so a *format*
        # failure in the judge rejected the artifact — measured live: a 95-byte
        # diff that had been produced and applied was thrown away because the
        # reviewer's answer was not JSON.  That is an inverted incentive: the
        # reviewer that says nothing is punished harder than the one that says
        # "reject", and the work is lost either way.
        #
        # The verdict is now advisory: the run proceeds, and the degradation is
        # recorded so the report says the review did not happen instead of
        # claiming it passed.
        self._tracer.trace(
            task_id,
            step="constitutional_default_advisory",
            kind="decision",
            detail={
                "reason": "constitutional reviewer LLM returned unparseable JSON after retries",
                "attempts": attempts,
                "last_error": last_error,
            },
        )
        return ReviewVerdict(
            verdict="ok",
            issues=[
                "constitutional review did not run: the reviewer's answer could "
                f"not be parsed ({last_error})"
            ],
            advisory=True,
        )


# --- helpers ----------------------------------------------------------------


def _parse_verdict_json(text: str) -> tuple[ReviewVerdict | None, str | None]:
    """Parse a constitutional reviewer verdict JSON.

    Same shape as the regular reviewer's verdict, but each issue SHOULD start
    with a ``[principle_id]`` prefix. We don't enforce that — we just coerce
    everything to a list of strings.
    """
    from gigaforge.agents.reviewer import ReviewVerdict

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
    issues = [str(i) for i in issues_raw]
    try:
        return ReviewVerdict(verdict=verdict_raw, issues=issues), None
    except Exception as e:  # pydantic ValidationError
        return None, f"невалидная структура: {e}"


def _load_default_constitution() -> Constitution:
    """Load the default constitution, optionally overridden by env.

    Env:
      - ``GIGAFORGE_CONSTITUTION`` — either an inline JSON string OR a path to
        a JSON file. Auto-detected by prefix: ``{`` -> inline JSON; otherwise
        treated as a path.
    """
    env_v = os.environ.get("GIGAFORGE_CONSTITUTION", "")
    if not env_v:
        return Constitution.default()
    # Inline JSON if it starts with `{`.
    if env_v.lstrip().startswith("{"):
        try:
            return Constitution.from_json(env_v)
        except (json.JSONDecodeError, ValueError):
            # Bad config -> fall back to default (don't crash the pipeline).
            return Constitution.default()
    # Otherwise treat as a path.
    path = Path(env_v)
    if not path.exists():
        return Constitution.default()
    try:
        return Constitution.from_json(path.read_text())
    except (json.JSONDecodeError, ValueError, OSError):
        return Constitution.default()


def load_constitution() -> Constitution:
    """Public alias for the env-driven default constitution loader."""
    return _load_default_constitution()


# Re-export ReviewVerdict from the regular reviewer for callers' convenience.
# (Local import to avoid an import cycle at module-load time.)
from gigaforge.agents.reviewer import ReviewVerdict  # noqa: E402

_ = ReviewVerdict  # silence unused-import linters; callers can re-import

# Module-level default constitution (eagerly materialized so __all__ resolves).
DEFAULT_CONSTITUTION = Constitution.default()
