"""Agentic chat interface for GigaForge V2.

The chat is the *front door* to the harness: you describe a goal in natural
language, the agent plans it, runs the pipeline, and streams what is happening
back into the conversation.  Slash commands reach the same read-only store the
dashboard reads, so ``/cost`` in the chat and the cost card in the panel can
never disagree.

Four pieces, deliberately separated:

``ChatSession``
    Transcript with durable history (JSONL, one file per session).  A chat that
    forgets is a toy; persisting the transcript is what makes an agentic
    interface usable across a terminal restart.
``AgentBackend``
    A protocol: ``stream(goal) -> Iterator[ChatEvent]``.  The real backend drives
    ``TaskPipeline`` and projects the run's trace into events; tests inject a
    scripted backend, so the chat can be verified without a model.
``ChatEvent``
    Everything the UI shows is one of these (``say`` / ``step`` / ``tool`` /
    ``snapshot`` / ``error``).  Rendering is then a pure function of events and
    the transport can be a terminal, an HTTP response or a socket.
``build_chat_handler``
    The bridge to the web panel's ``POST /api/chat``.

Streaming contract: the pipeline runs on a worker thread and the *trace file* is
polled for new ``dag.node_*`` events, so progress appears while the run is still
in flight instead of arriving in one batch at the end.  Polling the persisted
trace (rather than an in-memory callback) is what makes the stream work
identically for a run started by *another* process.
"""

from __future__ import annotations

import json
import shlex
import threading
import time
import uuid
import inspect
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Optional, Sequence

from . import analytics as A
from . import renderers as R
from .snapshot import build_snapshot
from .store import UiStore, read_jsonl

#: Slash commands, with a one-line description each (also the ``/help`` text).
COMMANDS: dict[str, str] = {
    "/help": "list the available commands",
    "/run": "run the pipeline for a goal, streaming step events as they land",
    "/status": "current run status, progress and health signals",
    "/cost": "token usage and cost, total and per phase",
    "/cache": "cache contents, hit/miss counters and the hit rate",
    "/trace": "the most recent trace events (optional substring filter)",
    "/steps": "node timeline: status, duration and attempts",
    "/diff": "the run's final diff with per-file added/removed counts",
    "/models": "which model handled how many calls, and fallbacks",
    "/errors": "error signals from the journal and the trace",
    "/context": "the whole UI snapshot as JSON (what the panel renders)",
    "/export": "write the current report to a file (Markdown or JSON)",
    "/clear": "clear the transcript",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class ChatEvent:
    """One thing the chat shows.  ``kind`` decides how a renderer draws it."""

    kind: str  # say | step | tool | snapshot | error | command
    text: str = ""
    data: Mapping[str, Any] = field(default_factory=dict)
    ts: str = field(default_factory=_utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "text": self.text, "data": dict(self.data), "ts": self.ts}

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ChatEvent":
        return cls(
            kind=str(payload.get("kind") or "say"),
            text=str(payload.get("text") or ""),
            data=dict(payload.get("data") or {}),
            ts=str(payload.get("ts") or _utc_now()),
        )


# ── transcript ─────────────────────────────────────────────────────────────
class ChatSession:
    """A transcript that survives the process (one JSONL file per session)."""

    def __init__(self, *, session_id: Optional[str] = None, directory: str | Path = ".gigaforge/chat") -> None:
        self.session_id = session_id or uuid.uuid4().hex[:12]
        self.directory = Path(directory)
        self.path = self.directory / f"{self.session_id}.jsonl"
        self.events: list[ChatEvent] = self._load()
        self._persisted = self.path.is_file()

    def _load(self) -> list[ChatEvent]:
        return [ChatEvent.from_dict(row) for row in read_jsonl(self.path)]

    def reload(self) -> None:
        """Refresh from disk so mode-specific engines share one transcript."""
        if self.path.is_file():
            self.events = self._load()
            self._persisted = True
        elif self._persisted:
            self.events = []
            self._persisted = False

    def append(self, event: ChatEvent) -> ChatEvent:
        self.events.append(event)
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")
            self._persisted = True
        except OSError:  # pragma: no cover - read-only filesystem
            pass
        return event

    def say(self, text: str, **data: Any) -> ChatEvent:
        return self.append(ChatEvent(kind="say", text=text, data=data))

    def tool(self, name: str, payload: Mapping[str, Any] | None = None, text: str = "") -> ChatEvent:
        return self.append(ChatEvent(kind="tool", text=text or name, data={"name": name, **(payload or {})}))

    def step(self, node: str, status: str, **data: Any) -> ChatEvent:
        return self.append(ChatEvent(kind="step", text=f"{node}: {status}", data={"node": node, "status": status, **data}))

    def error(self, text: str, **data: Any) -> ChatEvent:
        return self.append(ChatEvent(kind="error", text=text, data=data))

    def clear(self) -> None:
        self.events = []
        self._persisted = False
        try:
            self.path.unlink(missing_ok=True)
        except OSError:  # pragma: no cover
            pass

    def as_text(self, *, limit: int = 200) -> str:
        lines: list[str] = []
        for event in self.events[-limit:]:
            prefix = {"say": "", "step": "  ", "tool": "» ", "error": "✗ ", "command": "$ "}.get(event.kind, "")
            lines.append(f"{prefix}{event.text}")
        return "\n".join(lines)

    def context_for_task(self, current_text: str, *, max_chars: int = 8000) -> str:
        """Return a bounded transcript of user/assistant turns before this task."""
        turns: list[tuple[str, str]] = []
        for event in self.events:
            text = event.text.strip()
            if not text:
                continue
            if (event.kind == "command" and event.data.get("role") == "user"
                    and not text.startswith("/")):
                turns.append(("User", text))
            elif event.kind in {"say", "assistant", "text"}:
                turns.append(("Assistant", text))

        if turns and turns[-1] == ("User", (current_text or "").strip()):
            turns.pop()
        if not turns:
            return ""

        selected: list[str] = []
        header = "Relevant earlier conversation (the current task takes priority):\n"
        remaining = max(0, int(max_chars) - len(header))
        for role, text in reversed(turns[-12:]):
            line = f"{role}: {text}"
            if len(line) > remaining:
                if not selected and remaining > len(role) + 2:
                    line = f"{role}: {text[-(remaining - len(role) - 2):]}"
                else:
                    break
            selected.append(line)
            remaining -= len(line) + 1
            if remaining <= 0:
                break
        if not selected:
            return ""
        return header + "\n".join(reversed(selected))

    def __len__(self) -> int:
        return len(self.events)

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"ChatSession({self.session_id!r}, events={len(self.events)})"


# ── agent backends ─────────────────────────────────────────────────────────
class AgentBackend:
    """Protocol: turn a goal into a stream of :class:`ChatEvent`."""

    name = "null"

    def stream(self, goal: str, session: ChatSession) -> Iterator[ChatEvent]:  # pragma: no cover - protocol
        raise NotImplementedError

    # ── optional direct-run helper ─────────────────────────────────────────
    def run_blocking(self, goal: str) -> Mapping[str, Any]:
        raise NotImplementedError


@dataclass
class ScriptedBackend(AgentBackend):
    """A deterministic backend for tests and demos — no model, no network."""

    name = "scripted"
    events: Sequence[Mapping[str, Any]] = ()
    result: Mapping[str, Any] = field(default_factory=dict)

    def stream(self, goal: str, session: ChatSession) -> Iterator[ChatEvent]:
        yield session.say(f"planning: {goal}")
        for payload in self.events:
            yield session.step(
                str(payload.get("node") or "?"),
                str(payload.get("status") or "unknown"),
                duration_ms=payload.get("duration_ms"),
            )
        yield session.say("done.")

    def run_blocking(self, goal: str) -> Mapping[str, Any]:
        return dict(self.result)


class DirectChatBackend(AgentBackend):
    """Plain conversation with the model — no DAG, no repository changes.

    A chat window needs two verbs.  "What can you do?" is a *question*: it must be
    answered by the model and must not touch the working tree.  "Add a clamp() to
    math_utils.py" is a *task*: it belongs to the DAG.

    Previously every non-slash line went to the pipeline, so asking a question
    started a run that planned against the repository.  This backend is the other
    verb: a bounded conversation window, the selected model, no tools.
    """

    name = "direct"

    def __init__(
        self,
        client: Any,
        *,
        model: str = "",
        system: str = "",
        temperature: float = 0.6,
        max_tokens: int = 900,
        window: int = 16,
        search: Any = None,
    ) -> None:
        self.client = client
        self.model = model
        self.system = system or DEFAULT_CHAT_SYSTEM
        self.temperature = float(temperature)
        self.max_tokens = int(max_tokens)
        self.window = max(2, int(window))
        self.history: list[dict[str, str]] = []
        self.last_result: Mapping[str, Any] = {}
        #: The web-search tool, or ``None`` to answer from the model alone.
        #:
        #: A chat window that cannot search is a chat window that says "I cannot
        #: search the internet" — which is what the live probe got, and it is
        #: the one answer a user will not accept from an agent harness.  The
        #: decision to search is made *deterministically* here (see
        #: :func:`wants_search`), not by asking the model whether it would like
        #: a tool: that would be a model call to decide whether to make a model
        #: call, and the phrase "search the web" is not ambiguous.
        self.search = search

    def set_model(self, model: str) -> None:
        self.model = (model or "").strip()

    def reset(self) -> None:
        self.history.clear()

    def restore_history(self, events: Sequence[ChatEvent]) -> None:
        history: list[dict[str, str]] = []
        for event in events:
            text = event.text.strip()
            if not text:
                continue
            if (event.kind == "command" and event.data.get("role") == "user"
                    and not text.startswith("/")):
                history.append({"role": "user", "content": text})
            elif event.kind in {"say", "assistant", "text"}:
                history.append({"role": "assistant", "content": text})
        self.history = history[-self.window:]

    def stream(self, goal: str, session: ChatSession) -> Iterator[ChatEvent]:
        text = (goal or "").strip()
        if not text:
            return
        if self.client is None:
            yield session.error(
                "no model client: set GIGAFORGE_AUTH_KEY (or GF_CLIENT__AUTH_KEY) in .env"
            )
            return

        self.history.append({"role": "user", "content": text})
        messages = [{"role": "system", "content": self.system}] + self.history[-self.window:]
        yield session.tool("model", {"model": self.model}, text=f"→ {self.model or 'default'}")

        # ── deterministic pre-search ────────────────────────────────────────
        # The harness decides whether this message needs the web, from the
        # message alone.  No model call is spent on the decision, and the
        # search happens *before* the answer call so the model writes from
        # observations instead of from memory.  A search that fails is
        # reported to the model as a failed search, not hidden: an answer that
        # silently pretends to have searched is the worst outcome here.
        search_note = ""
        if self.search is not None and wants_search(text):
            query = search_query(text)
            yield session.tool("web_search", {"query": query}, text=f"⌕ {query}")
            try:
                found = self.search.run({"query": query})
                if inspect.isawaitable(found):
                    found = _run_awaitable(found)
                found = str(found or "").strip()
            except Exception as exc:  # noqa: BLE001 - surfaced, not swallowed
                found = f"ERROR: {type(exc).__name__}: {exc}"
            if found:
                search_note = (
                    "## Web search results\n"
                    f"Query: {query}\n\n{found}\n\n"
                    "Answer from these results and cite the URLs they contain. "
                    "Do not invent a URL that is not above."
                )
                messages = [
                    {"role": "system", "content": self.system},
                    {"role": "system", "content": search_note},
                ] + self.history[-self.window:]

        try:
            result = self.client.complete(
                messages,
                model=self.model or None,
                temperature=self.temperature,
                max_tokens=self.max_tokens,
            )
        except Exception as exc:  # noqa: BLE001 - surfaced as a chat message
            self.history.pop()  # keep the transcript consistent with the model's view
            yield session.error(f"{type(exc).__name__}: {exc}")
            return

        reply = str(getattr(result, "content", "") or "").strip()
        usage = getattr(result, "usage", None)
        self.history.append({"role": "assistant", "content": reply})
        self.last_result = {
            "accepted": True,
            "status": "answered",
            "mode": "chat",
            "model": self.model,
            "reply": reply,
            "searched": bool(search_note),
            "usage": usage if isinstance(usage, dict) else None,
            "tokens": int((usage or {}).get("total_tokens", 0) or 0)
            if isinstance(usage, dict) else 0,
        }
        yield session.say(reply or "(empty reply)")


#: Phrases that mean "this message needs the web".  Matched case-insensitively
#: against the message alone — no model call decides whether to search, because
#: that would be a call to decide whether to make a call, and "search the web"
#: is not ambiguous.
_SEARCH_PHRASES = (
    "search the web", "search the internet", "search online", "web search",
    "google ", "look up online", "look it up online", "find online",
    "latest news", "current news", "what's new in", "whats new in",
    "as of today", "up to date", "up-to-date", "recent developments",
    "найди в интернете", "поищи в интернете", "поиск в интернете",
    "погугли", "посмотри в интернете", "свежие новости", "последние новости",
    "актуальн", "в интернете",
)

#: Words that make a message a *question about the world* rather than about the
#: repository.  Used only together with a search phrase, never alone.
_WORLD_HINTS = (
    "trend", "trends", "news", "release", "released", "version", "benchmark",
    "compare", "comparison", "market", "price", "pricing", "who is", "what is the latest",
)


def wants_search(message: str) -> bool:
    """Whether this message asks for the web, decided from the text alone.

    Deliberately conservative: a false positive costs one search and a few
    hundred tokens, a false negative costs the user an answer that says "I
    cannot search the internet" — which is the failure the live probe found.
    """
    low = (message or "").lower()
    if not low.strip():
        return False
    return any(phrase in low for phrase in _SEARCH_PHRASES)


def search_query(message: str) -> str:
    """The query to send, derived from the message without a model call.

    The instruction words are stripped so the search engine sees the subject
    rather than the request: "Search the web for AI agent framework trends"
    becomes "AI agent framework trends".  A query that keeps the instruction
    returns pages about searching, which is the classic pre-search mistake.
    """
    text = (message or "").strip()
    low = text.lower()
    for phrase in _SEARCH_PHRASES:
        index = low.find(phrase)
        if index >= 0:
            text = text[:index] + " " + text[index + len(phrase):]
            low = text.lower()
    for filler in ("for me", "please", "and cite your sources", "cite your sources",
                   "with sources", "and tell me", "tell me", "can you", "could you"):
        text = text.replace(filler, " ").replace(filler.title(), " ")
    text = " ".join(text.split()).strip(" .,:;-—")
    # A leading preposition is left behind when the instruction is removed
    # ("Search the web for X" -> "for X", "найди в интернете про X" -> "про X").
    # A search engine treats it as a term, so it is stripped here.
    for lead in ("for ", "about ", "on ", "про ", "о ", "об ", "по "):
        if text.lower().startswith(lead):
            text = text[len(lead):]
            break
    text = " ".join(text.split()).strip(" .,:;-—")
    return text or (message or "").strip()


def _run_awaitable(awaitable: Any) -> Any:
    """Run a coroutine to completion from synchronous code.

    The search tool's ``run`` is ``async`` because the ReAct loop awaits it.
    The chat backend is a generator consumed synchronously, so the coroutine is
    driven here.  A fresh event loop is used when none is running; when one is
    (a notebook, an async server), the coroutine is run on a worker thread so
    the caller's loop is not re-entered.
    """
    import asyncio

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(awaitable)

    result: dict[str, Any] = {}

    def _worker() -> None:
        try:
            result["value"] = asyncio.run(awaitable)
        except BaseException as exc:  # noqa: BLE001 - re-raised on the caller
            result["error"] = exc

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    thread.join()
    if "error" in result:
        raise result["error"]
    return result.get("value")


DEFAULT_CHAT_SYSTEM = (
    "Ты — GigaForge, инженерный ассистент внутри рабочего репозитория. "
    "Отвечай кратко и по делу, на языке собеседника. "
    "Если вопрос про код — предлагай конкретные шаги или фрагменты кода. "
    "Если пользователь просит изменить файлы — скажи, что это делается в режиме Agent; "
    "Goal выполняет задачу, запускает проверки и исправляет реальные сбои."
)


class PipelineBackend(AgentBackend):
    """Drives a one-shot pipeline or a long-lived goal and streams its trace.

    The pipeline is executed on a worker thread while this generator polls the
    persisted JSONL trace for newly appended ``dag.node_*`` events.  Two
    consequences worth stating: progress streams *during* the run, and the same
    mechanism works for a run started by a different process (the stream is a
    view over the file, not a callback from an object).
    """

    name = "pipeline"

    def __init__(
        self,
        store: UiStore,
        *,
        pipeline_factory: Optional[Callable[[], Any]] = None,
        config: Any = None,
        poll_interval: float = 0.25,
        max_wait_s: Optional[float] = None,
        run_kwargs: Optional[Mapping[str, Any]] = None,
        goal_mode: bool = False,
        goal_runner_factory: Optional[Callable[..., Any]] = None,
    ) -> None:
        self.store = store
        self.pipeline_factory = pipeline_factory
        self.config = config
        self.poll_interval = poll_interval
        self.goal_mode = bool(goal_mode)
        # Keep the UI attached until the pipeline's own configured timeout ends
        # the run. An extra UI deadline left work running in the background while
        # making the same Goal appear resumable, which could start a duplicate.
        self.max_wait_s = None if max_wait_s is None else max(0.0, float(max_wait_s))
        self.run_kwargs = dict(run_kwargs or {})
        self.goal_runner_factory = goal_runner_factory
        self._owned_runtime: Any = None
        #: ``Callable[[str], bool]`` consulted with the candidate diff before it is
        #: applied.  ``None`` means "apply without asking" (CLI, automation).
        self.approve: Optional[Any] = None
        self.last_result: Mapping[str, Any] = {}

    def _build_pipeline(self) -> Any:
        if self.pipeline_factory is not None:
            return self.pipeline_factory()
        from gigaforge.core.pipeline import TaskPipeline  # imported late: keeps the UI import-light

        # Fall back to the composition root, not to a bare TaskPipeline.  A bare
        # pipeline has no model client, so the first goal typed into a chat died
        # with "no model client is wired into the pipeline" — which is why the
        # chat looked like a read-only console with slash commands.
        try:
            from gigaforge.runtime import build_runtime

            runtime = build_runtime(
                core_config=self.config if not isinstance(self.config, type(None)) else None,
            )
            self._owned_runtime = runtime
            pipeline = runtime.pipeline
            if self.goal_mode and self.goal_runner_factory is None:
                self.goal_runner_factory = lambda goal, **kwargs: runtime.run_goal(goal, **kwargs)
        except Exception:  # noqa: BLE001 - fall through to the config-only object
            pipeline = TaskPipeline(config=self.config)
        self.pipeline_factory = lambda: pipeline
        return pipeline

    def stream(self, goal: str, session: ChatSession) -> Iterator[ChatEvent]:
        try:
            pipeline = self._build_pipeline()
        except Exception as exc:
            yield session.error(f"cannot construct the pipeline: {exc}")
            return

        mode_label = "long-lived goal" if self.goal_mode else "run"
        yield session.tool("pipeline", {"goal": goal, "mode": mode_label},
                           text=f"starting {mode_label}: {goal}")
        started = time.monotonic()
        box: dict[str, Any] = {}

        def worker() -> None:
            try:
                kwargs = dict(self.run_kwargs)
                conversation_context = session.context_for_task(goal)
                # Human-in-the-loop: the gate is handed to the pipeline so it is
                # consulted *before* the diff touches the worktree, with the exact
                # diff the user is approving.
                if self.approve is not None and "approve" not in kwargs:
                    kwargs["approve"] = self.approve
                if self.goal_mode:
                    goal_kwargs = {
                        key: value for key, value in kwargs.items()
                        if key in {"goal_id", "resume", "max_cycles", "max_fix_iterations",
                                   "use_subagents", "files", "symbols", "budget_tokens", "approve",
                                   "conversation_context", "additional_context", "goal_override"}
                    }
                    goal_for_runner = (
                        str(goal_kwargs.pop("goal_override"))
                        if "goal_override" in goal_kwargs else goal
                    )
                    if conversation_context and not goal_kwargs.get("resume"):
                        goal_kwargs["conversation_context"] = conversation_context
                    goal_kwargs.setdefault("max_cycles", 8)
                    goal_kwargs.setdefault("max_fix_iterations", 2)
                    goal_kwargs.setdefault("approve", self.approve)
                    if self.goal_runner_factory is not None:
                        box["result"] = self.goal_runner_factory(goal_for_runner, **goal_kwargs)
                    else:
                        from pathlib import Path

                        from gigaforge.core.goal import GoalRunner

                        config = self.config or getattr(pipeline, "config", None)
                        repo_root = Path(str(getattr(config, "repo_root", ".") or "."))
                        box["result"] = GoalRunner(
                            pipeline, repo_root=repo_root
                        ).run(goal_for_runner, **goal_kwargs)
                elif callable(getattr(pipeline, "run", None)):
                    if conversation_context:
                        kwargs["conversation_context"] = conversation_context
                    box["result"] = pipeline.run(goal, **kwargs)
                elif callable(pipeline):
                    box["result"] = pipeline(goal, **kwargs)
                else:
                    box["error"] = "pipeline object exposes no callable run()"
            except Exception as exc:  # noqa: BLE001 - surfaced to the user
                box["error"] = f"{type(exc).__name__}: {exc}"

        thread = threading.Thread(target=worker, name="gigaforge-pipeline", daemon=True)
        seen = self._trace_cursor()
        thread.start()
        emitted_final = False
        while thread.is_alive() or not emitted_final:
            step_events, seen = self._new_step_events(seen)
            for event in step_events:
                details = {
                    key: event[key]
                    for key in ("event", "name", "spawn_index", "count", "parallel",
                                "ok", "failed", "phase", "goal_id", "cycle_index",
                                "cycle_limit", "cycle_count", "goal_status", "passed",
                                "accepted", "resumed", "summary")
                    if key in event
                }
                yield session.step(
                    str(event.get("node") or event.get("name") or event.get("event") or "?"),
                    str(event.get("status") or _status_for(
                        str(event.get("event") or ""), event
                    )),
                    duration_ms=event.get("duration_ms"),
                    **details,
                )
            elapsed = time.monotonic() - started
            if not thread.is_alive():
                step_events, seen = self._new_step_events(seen)
                for event in step_events:
                    details = {
                        key: event[key]
                        for key in ("event", "name", "spawn_index", "count", "parallel",
                                    "ok", "failed", "phase", "goal_id", "cycle_index",
                                    "cycle_limit", "cycle_count", "goal_status", "passed",
                                    "accepted", "resumed", "summary")
                        if key in event
                    }
                    yield session.step(
                        str(event.get("node") or event.get("name") or event.get("event") or "?"),
                        str(event.get("status") or _status_for(
                            str(event.get("event") or ""), event
                        )),
                        duration_ms=event.get("duration_ms"),
                        **details,
                    )
                emitted_final = True
                break
            if self.max_wait_s is not None and elapsed > self.max_wait_s:
                yield session.error(f"run exceeded the {self.max_wait_s:.0f}s wait budget; still running in background")
                emitted_final = True
                break
            time.sleep(self.poll_interval)

        thread.join(timeout=1.0)
        if box.get("error"):
            yield session.error(str(box["error"]))
        result = box.get("result")
        if result is not None:
            self.last_result = _as_mapping(result)
            if self.goal_mode:
                goal_status = str(self.last_result.get("status") or "unknown")
                goal_id = str(self.last_result.get("goal_id") or "")
                yield session.tool(
                    "goal",
                    {"goal_id": goal_id, "status": goal_status,
                     "cycles": len(self.last_result.get("cycles") or []),
                     "workspace_root": self.last_result.get("workspace_root", "")},
                    text=f"goal {goal_id or '—'} finished with status {goal_status}",
                )
                if goal_status == "completed":
                    published = list(self.last_result.get("published_files") or [])
                    yield session.tool(
                        "publish",
                        {"files": published},
                        text=("published verified files: " + ", ".join(published))
                        if published else "no destination files changed",
                    )
        yield session.tool("snapshot", {}, text="run finished; refreshing snapshot")

    def run_blocking(self, goal: str) -> Mapping[str, Any]:
        pipeline = self._build_pipeline()
        if self.goal_mode:
            goal_kwargs = {
                key: value for key, value in self.run_kwargs.items()
                if key in {"goal_id", "resume", "max_cycles", "max_fix_iterations",
                           "use_subagents", "files", "symbols", "budget_tokens", "approve"}
            }
            goal_kwargs.setdefault("max_cycles", 8)
            goal_kwargs.setdefault("max_fix_iterations", 2)
            goal_kwargs.setdefault("approve", self.approve)
            if self.goal_runner_factory is not None:
                result = self.goal_runner_factory(goal, **goal_kwargs)
            else:
                from pathlib import Path

                from gigaforge.core.goal import GoalRunner

                config = self.config or getattr(pipeline, "config", None)
                result = GoalRunner(
                    pipeline,
                    repo_root=Path(str(getattr(config, "repo_root", ".") or ".")),
                ).run(goal, **goal_kwargs)
        else:
            runner = getattr(pipeline, "run", None)
            result = runner(goal, **self.run_kwargs) if callable(runner) else pipeline(goal, **self.run_kwargs)
        self.last_result = _as_mapping(result)
        return self.last_result

    # ── trace polling ────────────────────────────────────────────────────────
    def _trace_cursor(self) -> int:
        """Number of trace lines already present — the stream starts after these."""
        return len(read_jsonl(self.store.trace_path))

    def _new_step_events(self, cursor: int) -> tuple[list[dict[str, Any]], int]:
        """Return displayable trace rows and the next *raw trace line* cursor.

        The cursor must advance over every trace record, including records that
        the chat does not render. Advancing by the filtered result count caused
        repeats and omissions whenever metrics or cache events were interleaved.
        """
        events = read_jsonl(self.store.trace_path)
        start = max(0, min(int(cursor), len(events)))
        fresh = events[start:]
        rows: list[dict[str, Any]] = []
        for source in fresh:
            event = str(source.get("event") or "")
            if event not in _STEP_EVENT_KINDS:
                continue
            row = dict(source)
            row["event"] = event
            if not row.get("status"):
                row["status"] = _status_for(event, row)
            rows.append(row)
        return rows, len(events)


_STEP_EVENT_KINDS = {
    "dag.start",
    "dag.node_start",
    "dag.node_error",
    "dag.node_failed",
    "dag.finish",
    "dag.resume",
    "router.call",
    "fixloop.finish",
    "subagents.spawn",
    "subagents.finish",
    "subagents.no_worktree",
    "subagent.start",
    "subagent.finish",
    "goal.start",
    "goal.cycle_start",
    "goal.verify_start",
    "goal.verify_finish",
    "goal.cycle_finish",
    "goal.finish",
}
_STATUS_BY_EVENT = {
    "dag.start": "running",
    "dag.node_start": "running",
    "dag.finish": "succeeded",
    "dag.node_error": "failed",
    "dag.node_failed": "failed",
    "router.call": "succeeded",
    "subagents.spawn": "running",
    "subagents.no_worktree": "failed",
    "subagent.start": "running",
    "goal.start": "running",
    "goal.cycle_start": "running",
    "goal.verify_start": "running",
    "goal.verify_finish": "succeeded",
    "goal.cycle_finish": "succeeded",
    "goal.publish": "succeeded",
    "goal.finish": "succeeded",
}


def _status_for(event: str, payload: Mapping[str, Any] | None = None) -> str:
    payload = payload or {}
    if event == "subagent.finish":
        return "succeeded" if bool(payload.get("ok")) else "failed"
    if event == "subagents.finish":
        failed = int(payload.get("failed") or max(
            0, int(payload.get("count") or 0) - int(payload.get("ok") or 0)
        ))
        if failed:
            return "partial"
        return "succeeded"
    return _STATUS_BY_EVENT.get(event, "running")


def _as_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    for attr in ("to_dict", "model_dump", "dict"):
        method = getattr(value, attr, None)
        if callable(method):
            try:
                produced = method()
                if isinstance(produced, Mapping):
                    return dict(produced)
            except Exception:  # pragma: no cover - defensive
                continue
    return {"repr": repr(value)[:2000]}


# ── the chat engine ────────────────────────────────────────────────────────
class LangGraphBackend(AgentBackend):
    """Goal mode driven by the LangGraph runtime, streamed as it happens.

    ``PipelineBackend`` polls the JSONL trace, which works for any run but adds
    a poll interval of latency and cannot see an ``interrupt()``.  This backend
    subscribes to the graph's own ``custom`` stream instead: every node writes
    its progress with ``get_stream_writer()``, so the chat shows a step the
    moment it happens, and an approval interrupt surfaces as a question rather
    than as a stalled run.
    """

    name = "langgraph"

    def __init__(
        self,
        store: UiStore,
        *,
        goal_runner_factory: Optional[Callable[..., Any]] = None,
        run_kwargs: Optional[Mapping[str, Any]] = None,
    ) -> None:
        self.store = store
        self.goal_runner_factory = goal_runner_factory
        self.run_kwargs = dict(run_kwargs or {})
        self.approve: Optional[Any] = None
        self.last_result: Mapping[str, Any] = {}

    def stream(self, goal: str, session: ChatSession) -> Iterator[ChatEvent]:
        if self.goal_runner_factory is None:
            yield session.error("no goal runner is wired into this chat")
            return
        yield session.tool("goal", {"goal": goal, "engine": "langgraph"},
                           text=f"starting long-lived goal: {goal}")
        queue: list[dict[str, Any]] = []
        box: dict[str, Any] = {}

        def emit(event: str, payload: dict[str, Any]) -> None:
            queue.append({**payload, "event": event})

        def worker() -> None:
            try:
                kwargs = dict(self.run_kwargs)
                goal_for_runner = (
                    str(kwargs.pop("goal_override"))
                    if "goal_override" in kwargs else goal
                )
                kwargs.setdefault("max_cycles", 8)
                kwargs.setdefault("max_fix_iterations", 2)
                if not kwargs.get("resume"):
                    conversation_context = session.context_for_task(goal)
                    if conversation_context:
                        kwargs["conversation_context"] = conversation_context
                if self.approve is not None:
                    kwargs.setdefault("approve", self.approve)
                kwargs["emit"] = emit
                box["result"] = self.goal_runner_factory(goal_for_runner, **kwargs)
            except Exception as exc:  # noqa: BLE001 - surfaced to the user
                box["error"] = f"{type(exc).__name__}: {exc}"

        thread = threading.Thread(target=worker, name="gigaforge-goal-graph", daemon=True)
        thread.start()
        while thread.is_alive() or queue:
            while queue:
                event = queue.pop(0)
                yield session.step(
                    str(event.get("node") or event.get("event") or "?"),
                    str(event.get("status") or _status_for(str(event.get("event") or ""), event)),
                    **{key: event[key] for key in (
                        "event", "goal_id", "cycle_index", "cycle_limit", "passed",
                        "accepted", "summary", "files",
                    ) if key in event},
                )
            if thread.is_alive():
                time.sleep(0.05)
        thread.join(timeout=1.0)
        if box.get("error"):
            yield session.error(str(box["error"]))
        result = box.get("result")
        if result is not None:
            self.last_result = _as_mapping(result)
            status = str(self.last_result.get("status") or "unknown")
            yield session.tool(
                "goal",
                {"goal_id": self.last_result.get("goal_id", ""), "status": status,
                 "cycles": len(self.last_result.get("cycles") or [])},
                text=f"goal finished with status {status}",
            )
        yield session.tool("snapshot", {}, text="run finished; refreshing snapshot")

    def run_blocking(self, goal: str) -> Mapping[str, Any]:
        if self.goal_runner_factory is None:
            return {"error": "no goal runner is wired into this chat"}
        kwargs = dict(self.run_kwargs)
        if self.approve is not None:
            kwargs.setdefault("approve", self.approve)
        return _as_mapping(self.goal_runner_factory(goal, **kwargs))


class ChatEngine:
    """Turns a user line into a stream of events.

    A line beginning with ``/`` is a **tool call**: it is answered from the
    read-only store and never starts a pipeline run.  Anything else is a goal and
    goes to the agent backend.
    """

    def __init__(
        self,
        store: UiStore,
        *,
        backend: Optional[AgentBackend] = None,
        session: Optional[ChatSession] = None,
        prices: Any = None,
        default_model: str = "GigaChat",
    ) -> None:
        self.store = store
        self.backend = backend or PipelineBackend(store)
        self.session = session if session is not None else ChatSession()
        self.prices = prices
        self.default_model = default_model

    # ── public API ──────────────────────────────────────────────────────────
    def handle(self, line: str) -> list[ChatEvent]:
        """Process one line, returning every event it produced (non-streaming)."""
        return list(self.stream(line))

    def stream(self, line: str) -> Iterator[ChatEvent]:
        text = (line or "").strip()
        if not text:
            return
        self.session.reload()
        restore_history = getattr(self.backend, "restore_history", None)
        if callable(restore_history):
            restore_history(self.session.events)
        if text.startswith("/"):
            yield from self._command(text)
            return
        self.session.append(ChatEvent(kind="command", text=text, data={"role": "user"}))
        try:
            yield from self.backend.stream(text, self.session)
        except Exception as exc:  # noqa: BLE001 - a chat must not crash the shell
            yield self.session.error(f"{type(exc).__name__}: {exc}")

    def snapshot(self) -> Mapping[str, Any]:
        return build_snapshot(self.store, prices=self.prices, default_model=self.default_model)

    # ── commands ────────────────────────────────────────────────────────────
    def _command(self, text: str) -> Iterator[ChatEvent]:
        try:
            parts = shlex.split(text)
        except ValueError:
            parts = text.split()
        name, rest = parts[0], parts[1:]
        self.session.append(ChatEvent(kind="command", text=text, data={"role": "user"}))
        handler = {
            "/help": self._cmd_help,
            "/run": self._cmd_run,
            "/status": self._cmd_status,
            "/cost": self._cmd_cost,
            "/cache": self._cmd_cache,
            "/trace": self._cmd_trace,
            "/steps": self._cmd_steps,
            "/diff": self._cmd_diff,
            "/models": self._cmd_models,
            "/errors": self._cmd_errors,
            "/context": self._cmd_context,
            "/export": self._cmd_export,
            "/clear": self._cmd_clear,
        }.get(name)
        if handler is None:
            yield self.session.error(f"unknown command {name!r} — try /help")
            return
        self.session.tool(name, {"args": rest})
        yield from handler(rest)

    def _cmd_help(self, args: Sequence[str]) -> Iterator[ChatEvent]:
        lines = ["GigaForge V2 chat — commands:"]
        for name, description in COMMANDS.items():
            lines.append(f"  {name:<9} {description}")
        lines.append("")
        lines.append("Anything else is treated as a goal and sent to the agent.")
        yield self.session.say("\n".join(lines))

    def _cmd_run(self, args: Sequence[str]) -> Iterator[ChatEvent]:
        """``/run <goal>`` — start a real pipeline run from inside the chat."""
        if not args:
            yield self.session.error("/run needs a goal, e.g. /run harden the diff validator")
            return
        yield from self.backend.stream(" ".join(args), self.session)

    def _cmd_status(self, args: Sequence[str]) -> Iterator[ChatEvent]:
        snap = self.snapshot()
        run = snap.get("run") or {}
        progress = snap.get("progress") or {}
        health = (snap.get("kpis") or {}).get("health") or {}
        yield self.session.say(
            f"run {run.get('run_id') or '-'} · status {run.get('status') or 'unknown'} · "
            f"accepted {bool(run.get('accepted'))} · {progress.get('done', 0)}/{progress.get('total', 0)} nodes "
            f"({progress.get('percent', 0)}%)\n"
            f"router errors {health.get('router_errors', 0)} · breaker rejections "
            f"{health.get('breaker_rejected', 0)} · rate-limit 429s {health.get('ratelimit_429', 0)}"
        )

    def _cmd_cost(self, args: Sequence[str]) -> Iterator[ChatEvent]:
        snap = self.snapshot()
        kpis = snap.get("kpis") or {}
        tokens = kpis.get("tokens") or {}
        cost = kpis.get("cost") or {}
        lines = [
            f"tokens {A.as_int(tokens.get('total')):,} "
            f"(prompt {A.as_int(tokens.get('prompt')):,} / completion {A.as_int(tokens.get('completion')):,}) "
            f"over {A.as_int(tokens.get('calls')):,} model calls",
            f"total cost {cost.get('display') or A.human_usd(cost.get('micros'))}",
        ]
        phases = snap.get("phases") or []
        if phases:
            lines.append("per phase:")
            for phase in phases:
                lines.append(
                    f"  {phase.get('phase'):<10} {str(phase.get('model') or '-'):<14} "
                    f"calls={A.as_int(phase.get('calls')):<4} tokens={A.as_int(phase.get('tokens')):>8,} "
                    f"cost={A.human_usd(phase.get('cost_micros'))} p95={A.human_duration(phase.get('latency_p95_ms'))}"
                )
        yield self.session.say("\n".join(lines))

    def _cmd_cache(self, args: Sequence[str]) -> Iterator[ChatEvent]:
        cache = (self.snapshot().get("kpis") or {}).get("cache") or {}
        namespaces = cache.get("namespaces") or {}
        lines = [
            f"hit rate {A.as_float(cache.get('hit_rate')) * 100:.1f}%  "
            f"hits {A.as_int(cache.get('hits'))} · misses {A.as_int(cache.get('misses'))} · "
            f"writes {A.as_int(cache.get('writes'))}",
            f"entries {A.as_int(cache.get('entries'))} · size {A.human_bytes(cache.get('bytes'))}",
        ]
        if namespaces:
            lines.append("namespaces: " + ", ".join(f"{k}={v}" for k, v in namespaces.items()))
        yield self.session.say("\n".join(lines))

    def _cmd_trace(self, args: Sequence[str]) -> Iterator[ChatEvent]:
        needle = (args[0] if args else "").lower()
        events = self.store.trace_events(limit=200)
        if needle:
            events = [e for e in events if needle in json.dumps(e, default=str).lower()]
        if not events:
            yield self.session.say("no trace events" + (f" matching {needle!r}" if needle else ""))
            return
        shown = events[-40:]
        lines = [f"{len(shown)} of {len(events)} events:"]
        for event in shown:
            detail = " · ".join(
                str(v) for v in (
                    event.get("node"), event.get("phase"), event.get("model"),
                    event.get("status"), event.get("code"), event.get("error"),
                ) if v
            )
            lines.append(f"  {event.get('ts') or '-'}  {event.get('event') or '?':<20} {detail}")
        yield self.session.say("\n".join(lines))

    def _cmd_steps(self, args: Sequence[str]) -> Iterator[ChatEvent]:
        steps = self.snapshot().get("steps") or []
        if not steps:
            yield self.session.say("no node journal yet — run the pipeline once (/run <goal>)")
            return
        lines = ["node timeline:"]
        for step in steps:
            lines.append(
                f"  {step.get('index', 0):>2}  {str(step.get('node')):<11} {str(step.get('status')):<10} "
                f"{A.human_duration(step.get('duration_ms')):>10}"
                + (f"  {step.get('error_msg')}" if step.get("error_msg") else "")
            )
        yield self.session.say("\n".join(lines))

    def _cmd_diff(self, args: Sequence[str]) -> Iterator[ChatEvent]:
        snap = self.snapshot()
        diff = snap.get("diff") or {}
        if not diff.get("files"):
            yield self.session.say("no diff recorded for this run")
            return
        lines = [
            f"{diff.get('hunks', 0)} hunks · +{diff.get('added', 0)} / −{diff.get('removed', 0)} · "
            f"{len(diff.get('files') or [])} file(s)"
        ]
        for name in diff.get("files", []):
            lines.append(f"  {name}")
        text = str(diff.get("text") or "")
        if text:
            limit = int(args[0]) if args and str(args[0]).isdigit() else 40
            lines.append("")
            lines.extend(text.split("\n")[:limit])
        yield self.session.say("\n".join(lines))

    def _cmd_models(self, args: Sequence[str]) -> Iterator[ChatEvent]:
        models = self.snapshot().get("models") or []
        if not models:
            yield self.session.say("no model calls recorded")
            return
        yield self.session.say(
            "model routing:\n" + "\n".join(f"  {m.get('model'):<20} {A.as_int(m.get('calls')):>6} calls" for m in models)
        )

    def _cmd_errors(self, args: Sequence[str]) -> Iterator[ChatEvent]:
        snap = self.snapshot()
        errors = list(snap.get("errors") or [])
        health = (snap.get("kpis") or {}).get("health") or {}
        if not errors and not any(health.values()):
            yield self.session.say("no error signals recorded")
            return
        lines = [f"error signals: {json.dumps(health, ensure_ascii=False)}"]
        lines.extend(f"  ✗ {e}" for e in errors[:30])
        yield self.session.say("\n".join(lines))

    def _cmd_context(self, args: Sequence[str]) -> Iterator[ChatEvent]:
        yield self.session.say(R.render_json(self.snapshot()))

    def _cmd_export(self, args: Sequence[str]) -> Iterator[ChatEvent]:
        target = Path(args[0]) if args else Path(f"gigaforge-report-{self.session.session_id}.md")
        fmt = "json" if target.suffix.lower() == ".json" else "markdown"
        snap = self.snapshot()
        body = R.render_json(snap) if fmt == "json" else R.render_markdown(snap)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body, encoding="utf-8")
        except OSError as exc:
            yield self.session.error(f"cannot write {target}: {exc}")
            return
        yield self.session.say(f"wrote {target} ({fmt})")

    def _cmd_clear(self, args: Sequence[str]) -> Iterator[ChatEvent]:
        self.session.clear()
        yield self.session.say("transcript cleared")


# ── rendering ──────────────────────────────────────────────────────────────
def render_event(event: ChatEvent, *, plain: bool = True) -> str:
    """One event as a terminal line (no ``rich`` markup is ever assembled)."""
    if event.kind == "error":
        return f"✗ {event.text}"
    if event.kind == "tool":
        name = event.data.get("name") or "tool"
        return f"» {name}: {event.text}" if event.text and event.text != name else f"» {name}"
    if event.kind == "step":
        node = event.data.get("node") or "?"
        status = event.data.get("status") or "?"
        duration = event.data.get("duration_ms")
        suffix = f" ({A.human_duration(duration)})" if duration else ""
        return f"  {status:<10} {node}{suffix}"
    if event.kind == "command":
        return f"$ {event.text}"
    return event.text


def run_repl(
    engine: ChatEngine,
    *,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
    prompt: str = "gigaforge> ",
    banner: bool = True,
) -> None:
    """A line-oriented REPL.  Streaming events are printed as they arrive."""
    if banner:
        output_fn(f"{R.BANNER} agentic chat — type /help for commands, /exit to leave")
    while True:
        try:
            line = input_fn(prompt)
        except (EOFError, KeyboardInterrupt):
            output_fn("")
            break
        if line.strip() in {"/exit", "/quit", "exit", "quit"}:
            break
        for event in engine.stream(line):
            if event.kind == "step" and event.data.get("status") == "running":
                # a running step is transient noise in a line-oriented shell
                continue
            output_fn(render_event(event))


def build_chat_handler(engine: ChatEngine) -> Callable[[str], str]:
    """A ``message -> reply`` callable for the web panel's ``POST /api/chat``."""

    def handler(message: str) -> str:
        lines = [render_event(event) for event in engine.handle(message)]
        return "\n".join(line for line in lines if line)

    return handler



def default_engine(
    store: UiStore,
    *,
    session_id: Optional[str] = None,
    chat_dir: str | Path = ".gigaforge/chat",
    prices: Any = None,
    default_model: str = "GigaChat",
    offline: bool = False,
    config: Any = None,
    pipeline_factory: Optional[Callable[[], Any]] = None,
    run_kwargs: Optional[Mapping[str, Any]] = None,
    goal_mode: bool = False,
    goal_runner_factory: Optional[Callable[..., Any]] = None,
) -> ChatEngine:
    """A ready-to-use engine bound to the real pipeline (or a scripted one).

    ``pipeline_factory`` is how a caller hands over an already-composed pipeline
    (the runtime owns one); without it the backend builds a runtime itself, so a
    goal typed into any chat surface reaches the real DAG.  ``offline=True`` swaps
    in a scripted backend for demos with no credential at all.
    """
    session = ChatSession(session_id=session_id, directory=chat_dir)
    backend: AgentBackend
    if offline:
        backend = ScriptedBackend(
            events=[
                {"node": "intake", "status": "succeeded", "duration_ms": 4.0},
                {"node": "index", "status": "succeeded", "duration_ms": 118.5},
                {"node": "plan", "status": "succeeded", "duration_ms": 640.2},
            ]
        )
    else:
        if goal_mode:
            from gigaforge.core.langgraph_runtime import require_langgraph

            require_langgraph()
            backend = LangGraphBackend(
                store,
                goal_runner_factory=goal_runner_factory,
                run_kwargs=run_kwargs,
            )
        else:
            backend = PipelineBackend(
                store,
                config=config,
                pipeline_factory=pipeline_factory,
                run_kwargs=run_kwargs,
                goal_mode=goal_mode,
                goal_runner_factory=goal_runner_factory,
            )
    return ChatEngine(
        store, backend=backend, session=session, prices=prices, default_model=default_model
    )


__all__ = [
    "COMMANDS",
    "AgentBackend",
    "ChatEngine",
    "ChatEvent",
    "ChatSession",
    "DirectChatBackend",
    "DEFAULT_CHAT_SYSTEM",
    "LangGraphBackend",
    "PipelineBackend",
    "ScriptedBackend",
    "build_chat_handler",
    "default_engine",
    "direct_engine",
    "render_event",
    "run_repl",
]


def direct_engine(
    store: UiStore,
    *,
    client: Any,
    model: str = "",
    session_id: Optional[str] = None,
    chat_dir: str | Path = ".gigaforge/chat",
    prices: Any = None,
    default_model: str = "GigaChat",
    system: str = "",
    search: Any = None,
) -> ChatEngine:
    """A chat engine that talks to the model without running the pipeline.

    ``search`` is the web-search tool.  When it is ``None`` the engine builds
    one, because a chat window that cannot search answers "I cannot search the
    internet" — the one answer a user will not accept from an agent harness.
    Passing ``search=False`` disables it explicitly (tests, offline use).
    """
    if search is None:
        try:
            from gigaforge.agents.web_search_tool import WebSearchTool

            search = WebSearchTool()
        except Exception:  # noqa: BLE001 - chat must still work without search
            search = None
    elif search is False:
        search = None
    session = ChatSession(session_id=session_id, directory=chat_dir)
    backend = DirectChatBackend(client, model=model, system=system, search=search)
    return ChatEngine(store, backend=backend, session=session, prices=prices,
                      default_model=default_model)
