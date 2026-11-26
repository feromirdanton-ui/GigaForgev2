"""Task state and the stable system-prompt prefix (M4–M5).

Two things make an agent loop cheap and debuggable:

* **A stable prefix.**  The system message is assembled once, from parts that
  do not change during a run, and hashed.  Every later phase re-uses the exact
  same bytes, so a provider-side prefix cache can hit.  ``TaskState.history``
  therefore lives *after* the prefix, never inside it.
* **An explicit scratchpad.**  Intermediate reasoning is kept in a bounded,
  separate buffer that the model may read but which is never part of the
  stable prefix.  Bounding it stops an agent from slowly poisoning its own
  context with stale notes.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional, Sequence

DEFAULT_PERSONA = (
    "You are IGAFORGE, a precise repository-modification agent. "
    "You reason from the indexed symbols you are given, you never invent file "
    "contents, and when you change code you emit a single unified diff."
)

DEFAULT_RULES: tuple[str, ...] = (
    "Prefer the smallest correct change; do not refactor beyond the task.",
    "Cite paths and symbol names exactly as they appear in the context slice.",
    "When asked for a diff, return only the diff inside one ```diff fence.",
    "If the request is ambiguous, state the assumption you are making and proceed.",
    "Never print secrets, tokens or credentials, even if present in the context.",
)


def _digest(*parts: str) -> str:
    hasher = hashlib.sha256()
    for part in parts:
        hasher.update(part.encode("utf-8"))
        hasher.update(b"\x00")
    return hasher.hexdigest()


def normalize_whitespace(text: str, *, keep_lines: bool = False) -> str:
    """Collapse whitespace so the same request hashes the same way.

    By default *all* whitespace runs collapse to a single space, newlines
    included.  That is what ``normalized_task`` in the cache key means: a task
    pasted as three bulleted lines and the same task typed as one sentence are
    the same request, and hashing them differently would split the cache for no
    reason.  Pass ``keep_lines=True`` when line structure carries meaning.
    """
    if not text:
        return ""
    if keep_lines:
        lines = [" ".join(line.split()) for line in text.splitlines()]
        return "\n".join(line for line in lines if line).strip()
    return " ".join(text.split())


@dataclass
class SystemPrefix:
    """An immutable, hashable system prompt."""

    text: str
    hash: str
    parts: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"hash": self.hash, "chars": len(self.text), "parts": sorted(self.parts)}


def build_system_prefix(
    *,
    persona: str = DEFAULT_PERSONA,
    rules: Sequence[str] = DEFAULT_RULES,
    repo_map: str = "",
    tool_contract: str = "",
    model_hint: str = "",
    skills: str = "",
    extra: Optional[Mapping[str, str]] = None,
) -> SystemPrefix:
    """Assemble the stable prefix.

    Order is fixed (persona → rules → tools → skills → repo map → model hint) so
    that two runs with the same inputs produce byte-identical text.  ``extra``
    is sorted by key for the same reason: a dict's insertion order must not
    leak into the prompt, or the hash would change for no substantive reason.

    ``skills`` is the rendered skill block for *this* run's task class.  It sits
    after the tool contract and before the repo map because it is procedural
    knowledge about the work, while the repo map is data about the tree — and
    the data is what changes most often, so it goes last, closest to the
    conversation, where a provider's prefix cache is least likely to be
    invalidated by it.
    """
    parts: dict[str, str] = {"persona": persona.strip()}
    if rules:
        parts["rules"] = "\n".join(f"{i + 1}. {r.strip()}" for i, r in enumerate(rules))
    if tool_contract:
        parts["tools"] = tool_contract.strip()
    if skills:
        parts["skills"] = skills.strip()
    if repo_map:
        parts["repo_map"] = repo_map.strip()
    if model_hint:
        parts["model_hint"] = model_hint.strip()
    for key in sorted(extra or {}):
        value = (extra or {})[key]
        if value and value.strip():
            parts[f"extra:{key}"] = value.strip()

    blocks = [f"## {name}\n{body}" for name, body in parts.items()]
    text = "\n\n".join(blocks)
    return SystemPrefix(text=text, hash=_digest(text), parts=parts)


@dataclass
class PlanStep:
    """One step of the agent's plan — both faces of it.

    Two shapes meet here and both are real:

    * the **DAG's** plan is a list of human-readable lines, built by
      :meth:`TaskState.set_plan` from strings — ``index`` + ``description``;
    * the **agent layer's** plan is structured — ``tool`` + ``args``, which is
      what ``ReActRunner`` emits and what ``plan_guard`` validates.

    They used to be two different classes in two different modules
    (``core.state.PlanStep`` and ``core.task_state.PlanStep``), so a plan
    produced by one could not be read by the other.  That is the mechanical
    reason the ReAct plan never reached the coder: there was no object both
    sides agreed on, so "pass the plan along" had nothing to pass.
    """

    index: int = 0
    description: str = ""
    status: str = "pending"   # pending | doing | done | failed | skipped
    tool: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    done: bool = False
    result: Optional[dict[str, Any]] = None

    def __post_init__(self) -> None:
        # A DAG line (``"edit_function: foo"``) carries its tool in the text.
        # Only an identifier-shaped prefix is treated as a tool name, so prose
        # like ``"Note: check the tests"`` is not mistaken for one.
        if not self.tool and self.description:
            head, sep, rest = self.description.partition(":")
            if sep and rest.strip() and _TOOL_NAME.match(head.strip()):
                self.tool = head.strip()
                self.args = self.args or {"target": rest.strip()}
        if not self.description and self.tool:
            self.description = render_plan_step(self.tool, self.args)

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "description": self.description,
            "status": self.status,
            "tool": self.tool,
            "args": dict(self.args),
            "done": self.done,
        }

    def to_status_dict(self) -> dict[str, Any]:
        """Minimal stable-ordered dict for the KV-cache prefix.

        Insertion order is fixed (tool, args, done) and ``result`` is excluded:
        it changes after every step and would bust the prefix cache.
        """
        return {"tool": self.tool, "args": self.args, "done": self.done}


#: A tool name is an identifier — never a sentence.
_TOOL_NAME = re.compile(r"^[a-z_][a-z0-9_]*$")


def render_plan_step(tool: str, args: Mapping[str, Any] | None = None) -> str:
    """Render a structured step as the DAG's human-readable plan line."""
    tool = str(tool or "").strip()
    if not tool:
        return ""
    args = args if isinstance(args, Mapping) else {}
    for key in ("name", "path", "file", "query", "target", "value"):
        if key in args and str(args[key]).strip():
            return f"{tool}: {args[key]}"
    if args:
        first = next(iter(args))
        return f"{tool}: {args[first]}"
    return tool


@dataclass
class TaskState:
    """Mutable run state: goal, plan, scratchpad, metrics.

    The state is *serialisable* and *resumable*: :meth:`to_dict` round-trips
    through :meth:`from_dict`, which is what the DAG checkpoints write to SQLite
    and what a resumed run reads back.
    """

    run_id: str = ""
    goal: str = ""
    task_hash: str = ""
    prefix: Optional[SystemPrefix] = None
    plan: list[PlanStep] = field(default_factory=list)
    scratchpad: list[str] = field(default_factory=list)
    notes: dict[str, str] = field(default_factory=dict)
    slice_hash: str = ""
    git_sha: str = ""
    phase_usages: dict[str, dict[str, Any]] = field(default_factory=dict)
    counters: dict[str, float] = field(default_factory=dict)
    #: Cost of the run in integer micro-USD, accumulated per model call.
    cost_micros: int = 0
    #: Per-phase cost, so a report can say where the money went.
    phase_cost_micros: dict[str, int] = field(default_factory=dict)
    #: Cache accounting: lookups and hits.  A bare ``cache_hit`` bool says
    #: whether the LAST lookup hit; the rate says whether the cache works.
    cache_hits: int = 0
    cache_lookups: int = 0
    #: Things the run could not do and did not fail on (see ``note_degraded``).
    degraded: list[dict[str, Any]] = field(default_factory=list)
    #: One row per model call: phase, decided level, attempt, repeat, the model
    #: that actually answered, tokens and latency.  The route table is a policy;
    #: this is what the run experienced.  A policy never compared against its own
    #: outcomes cannot be improved, only argued about.
    routing: list[dict[str, Any]] = field(default_factory=list)
    artifacts: dict[str, Any] = field(default_factory=dict)
    completed_nodes: list[str] = field(default_factory=list)
    history: list[dict[str, str]] = field(default_factory=list)
    status: str = "running"       # running | succeeded | failed | cancelled
    error: str = ""
    max_scratchpad_chars: int = 8000
    max_plan_steps: int = 24
    #: The agent layer keys its trace and budget accounting on ``task_id``; the
    #: DAG keys its checkpoints on ``run_id``.  They are the same run, so the
    #: two names are kept in sync rather than being two classes.
    task_id: str = ""
    #: Free-form per-run metrics (the agent layer's name for ``counters``).
    metrics: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.task_id and not self.run_id:
            self.run_id = self.task_id
        elif self.run_id and not self.task_id:
            self.task_id = self.run_id

    # ── agent-layer API ──────────────────────────────────────────────────────
    def serialize_prefix(self) -> dict[str, Any]:
        """Minimal stable-ordered prefix prepended before each LLM call.

        Fixed key order: ``["goal", "plan", "slice_hash"]``.  Excludes the
        scratchpad, metrics and per-step results — those mutate per step and
        would bust any prefix-based KV cache.
        """
        return {
            "goal": self.goal,
            "plan": [s.to_status_dict() for s in self.plan],
            "slice_hash": self.slice_hash,
        }

    def serialize_full(self) -> dict[str, Any]:
        """Full state for persistence/tracing.  NOT used as the LLM prefix."""
        return {
            "goal": self.goal,
            "task_id": self.task_id,
            "git_sha": self.git_sha,
            "slice_hash": self.slice_hash,
            "plan": [s.to_dict() for s in self.plan],
            "scratchpad": list(self.scratchpad),
            "metrics": dict(self.metrics),
        }

    def add_fact(self, fact: str) -> None:
        """Append a compact fact to the scratchpad.  No dedup — caller's job."""
        if not isinstance(fact, str):
            raise TypeError(f"add_fact expects str, got {type(fact).__name__}")
        self.scratchpad.append(fact)

    def mark_step_done(self, idx: int, result: dict[str, Any]) -> None:
        """Flip step *idx* to done and record its result.

        Raises ``IndexError`` when out of range — a silent no-op here would
        make a plan look executed when nothing ran.
        """
        if idx < 0 or idx >= len(self.plan):
            raise IndexError(f"plan step idx out of range: {idx} (len={len(self.plan)})")
        step = self.plan[idx]
        step.done = True
        step.result = result
        step.status = "done"

    # ── prompt assembly ──────────────────────────────────────────────────────
    def system_message(self) -> dict[str, str]:
        if self.prefix is None:
            return {"role": "system", "content": DEFAULT_PERSONA}
        return {"role": "system", "content": self.prefix.text}

    def messages(self, *, include_history: bool = True, extra: Sequence[Mapping[str, Any]] = ()) -> list[dict[str, Any]]:
        """System message first, then the volatile tail.

        The system message is always index 0 with content identical across every
        phase of the run — that invariant is what the tests pin down.
        """
        out: list[dict[str, Any]] = [self.system_message()]
        if include_history:
            out.extend(dict(m) for m in self.history)
        out.extend(dict(m) for m in extra)
        return out

    # ── scratchpad ───────────────────────────────────────────────────────────
    def add_note(self, text: str) -> None:
        """Append to the scratchpad, dropping the oldest notes past the cap.

        Trimming from the front keeps recent reasoning; an agent that exceeds the
        cap has usually drifted, and recent context is the part worth keeping.
        """
        if not text:
            return
        self.scratchpad.append(text.strip())
        total = sum(len(n) + 1 for n in self.scratchpad)
        while len(self.scratchpad) > 1 and total > self.max_scratchpad_chars:
            total -= len(self.scratchpad.pop(0)) + 1

    def scratchpad_text(self) -> str:
        return "\n".join(self.scratchpad)

    # ── plan ─────────────────────────────────────────────────────────────────
    def set_plan(self, steps: Iterable[Any]) -> None:
        """Install the plan, accepting either shape of step.

        A ``str`` is a DAG line (``"edit_function: foo"``); a ``PlanStep`` is
        already structured and is kept as it is.  Accepting both is what lets
        the ReAct episode's plan — a list of ``PlanStep`` — be installed here
        without a lossy round-trip through strings.
        """
        out: list[PlanStep] = []
        for i, step in enumerate(list(steps)[: self.max_plan_steps]):
            if isinstance(step, PlanStep):
                step.index = i
                out.append(step)
            else:
                out.append(PlanStep(index=i, description=str(step).strip()))
        self.plan = out

    def mark_step(self, index: int, status: str) -> None:
        for step in self.plan:
            if step.index == index:
                step.status = status
                return

    def add_history(self, role: str, content: str) -> None:
        self.history.append({"role": role, "content": content})

    def bump(self, name: str, value: float = 1.0) -> float:
        self.counters[name] = self.counters.get(name, 0.0) + float(value)
        return self.counters[name]

    def record_phase_usage(self, phase: str, usage: Mapping[str, Any]) -> None:
        bucket = self.phase_usages.setdefault(
            phase, {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "calls": 0}
        )
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            bucket[key] = bucket.get(key, 0) + int(usage.get(key, 0) or 0)
        bucket["calls"] = bucket.get("calls", 0) + max(
            0, int(usage.get("calls", 1) or 0)
        )
        # Cost is accumulated here because this is the ONE place every model
        # call passes through.  Tokens alone do not answer what a run cost: the
        # same token count on a different model is a different bill, and a
        # report that only shows tokens cannot tell a cheap run from an
        # expensive one.
        model = str(usage.get("model") or "")
        if "cost_micros" in usage:
            micros = max(0, int(usage.get("cost_micros", 0) or 0))
        elif model:
            try:
                from gigaforge.core.cost import PriceTable

                prices = getattr(self, "_prices", None)
                if prices is None:
                    # ``PriceTable()`` uses the built-in placeholder prices and
                    # ignores ``GF_CORE__PRICES__*`` entirely, so a deployment
                    # that configured its own prices got ``cost_usd = 0.0`` on
                    # every run — measured live: 12 769 prompt + 2 095 completion
                    # tokens on GigaChat, priced at 0.00.  ``from_env`` is the
                    # constructor that reads the configuration.
                    prices = PriceTable.from_env()
                    self._prices = prices
                micros = int(
                    prices.cost_micros(
                        model,
                        int(usage.get("prompt_tokens", 0) or 0),
                        int(usage.get("completion_tokens", 0) or 0),
                    )
                )
            except Exception:  # noqa: BLE001 - pricing must not fail a run
                micros = 0
        else:
            micros = 0
        if micros:
            bucket["cost_micros"] = bucket.get("cost_micros", 0) + micros
            self.cost_micros += micros
            self.phase_cost_micros[phase] = self.phase_cost_micros.get(phase, 0) + micros

    def note_cache(self, *, hit: bool) -> None:
        """Record one cache lookup and whether it was served from cache."""
        self.cache_lookups += 1
        if hit:
            self.cache_hits += 1

    @property
    def cache_hit_rate(self) -> float:
        if self.cache_lookups <= 0:
            return 0.0
        return self.cache_hits / self.cache_lookups

    def note_degraded(self, *, what: str, why: str, where: str = "") -> None:
        """Record something the run could not do and did not fail on.

        A silent ``except`` that swallows a broken check makes a run look clean
        when it was merely unverified.  Every such degradation is recorded here
        and surfaces in ``PipelineResult.degraded``, so "we did not check" is
        never reported as "we checked and it passed".
        """
        self.degraded.append({"what": what, "why": why[:400], "where": where})

    def total_tokens(self) -> int:
        return int(sum(v.get("total_tokens", 0) for v in self.phase_usages.values()))

    def total_calls(self) -> int:
        return int(sum(v.get("calls", 0) for v in self.phase_usages.values()))

    # ── serialisation ────────────────────────────────────────────────────────
    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "goal": self.goal,
            "task_hash": self.task_hash,
            "prefix_hash": self.prefix.hash if self.prefix else "",
            "prefix_text": self.prefix.text if self.prefix else "",
            "prefix_parts": dict(self.prefix.parts) if self.prefix else {},
            "plan": [s.to_dict() for s in self.plan],
            "scratchpad": list(self.scratchpad),
            "notes": dict(self.notes),
            "slice_hash": self.slice_hash,
            "git_sha": self.git_sha,
            "task_id": self.task_id,
            "metrics": _jsonable(self.metrics),
            "phase_usages": {k: dict(v) for k, v in self.phase_usages.items()},
            "counters": dict(self.counters),
            "artifacts": _jsonable(self.artifacts),
            "completed_nodes": list(self.completed_nodes),
            "history": list(self.history),
            "status": self.status,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TaskState":
        prefix_text = str(data.get("prefix_text", ""))
        prefix: Optional[SystemPrefix] = None
        if prefix_text:
            prefix = SystemPrefix(
                text=prefix_text,
                hash=str(data.get("prefix_hash", "")) or _digest(prefix_text),
                parts=dict(data.get("prefix_parts") or {}),
            )
        state = cls(
            run_id=str(data["run_id"]),
            goal=str(data.get("goal", "")),
            task_hash=str(data.get("task_hash", "")),
            prefix=prefix,
            slice_hash=str(data.get("slice_hash", "")),
            git_sha=str(data.get("git_sha", "")),
            notes=dict(data.get("notes") or {}),
            phase_usages={k: dict(v) for k, v in (data.get("phase_usages") or {}).items()},
            counters=dict(data.get("counters") or {}),
            artifacts=dict(data.get("artifacts") or {}),
            completed_nodes=list(data.get("completed_nodes") or []),
            history=[dict(m) for m in (data.get("history") or [])],
            status=str(data.get("status", "running")),
            error=str(data.get("error", "")),
            task_id=str(data.get("task_id", "")),
            metrics=dict(data.get("metrics") or {}),
        )
        state.plan = [
            PlanStep(
                index=int(s.get("index", i)),
                description=str(s.get("description", "")),
                status=str(s.get("status", "pending")),
                tool=str(s.get("tool", "")),
                args=dict(s.get("args") or {}),
                done=bool(s.get("done", False)),
            )
            for i, s in enumerate(data.get("plan") or [])
        ]
        state.scratchpad = [str(s) for s in (data.get("scratchpad") or [])]
        return state


def _jsonable(value: Any, *, limit: int = 32) -> Any:
    """Best-effort JSON-safe projection of arbitrary artifacts."""
    if value is None or isinstance(value, (bool, int, float, str)):
        if isinstance(value, str) and len(value) > 4000:
            return value[:4000] + "...[truncated]"
        return value
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v, limit=limit) for k, v in list(value.items())[:limit]}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v, limit=limit) for v in list(value)[:limit]]
    if hasattr(value, "to_dict"):
        try:
            return _jsonable(value.to_dict(), limit=limit)
        except Exception:  # pragma: no cover - defensive
            return f"<{type(value).__name__}>"
    return f"<{type(value).__name__}>"


__all__ = [
    "SystemPrefix",
    "TaskState",
    "PlanStep",
    "build_system_prefix",
    "normalize_whitespace",
    "render_plan_step",
    "DEFAULT_PERSONA",
    "DEFAULT_RULES",
]
