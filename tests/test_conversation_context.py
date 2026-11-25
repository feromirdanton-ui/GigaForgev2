from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from gigaforge.core.goal import (
    GoalRunner,
    GoalStore,
    VerificationCheck,
    VerificationReport,
)
from gigaforge.ui.chat import (
    ChatEngine,
    ChatEvent,
    ChatSession,
    DirectChatBackend,
    LangGraphBackend,
    PipelineBackend,
)
from gigaforge.ui.store import UiStore


class _CapturingPipeline:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def run(self, goal: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append((goal, kwargs))
        return {"accepted": True, "status": "succeeded"}


def test_agent_pipeline_receives_prior_chat_turns(tmp_path: Path) -> None:
    pipeline = _CapturingPipeline()
    session = ChatSession(directory=tmp_path)
    session.append(ChatEvent(kind="command", text="Build a page", data={"role": "user"}))
    session.say("I created the initial page.")
    engine = ChatEngine(
        UiStore(trace_path=tmp_path / "trace.jsonl"),
        backend=PipelineBackend(
            UiStore(trace_path=tmp_path / "trace.jsonl"),
            pipeline_factory=lambda: pipeline,
            poll_interval=0.001,
        ),
        session=session,
    )

    list(engine.stream("Now refine its navigation"))

    assert len(pipeline.calls) == 1
    goal, kwargs = pipeline.calls[0]
    assert goal == "Now refine its navigation"
    assert "Build a page" in kwargs["conversation_context"]
    assert "I created the initial page." in kwargs["conversation_context"]
    assert "Now refine its navigation" not in kwargs["conversation_context"]


def test_chat_backend_keeps_prior_turns() -> None:
    class _Client:
        messages: list[dict[str, Any]] = []

        def complete(self, messages: list[dict[str, Any]], **_kwargs: Any) -> Any:
            self.messages = messages
            return SimpleNamespace(content="Here is the follow-up.")

    client = _Client()
    session = ChatSession()
    engine = ChatEngine(
        UiStore(),
        backend=DirectChatBackend(client),
        session=session,
    )

    list(engine.stream("What is in this project?"))
    list(engine.stream("How should I test it?"))

    contents = [message["content"] for message in client.messages]
    assert "What is in this project?" in contents
    assert "Here is the follow-up." in contents


def test_context_refreshes_when_switching_between_cached_engines(tmp_path: Path) -> None:
    class _Client:
        def complete(self, _messages: list[dict[str, Any]], **_kwargs: Any) -> Any:
            return SimpleNamespace(content="The project is a small web app.")

    chat_session = ChatSession(session_id="shared", directory=tmp_path)
    agent_session = ChatSession(session_id="shared", directory=tmp_path)
    chat_engine = ChatEngine(
        UiStore(),
        backend=DirectChatBackend(_Client()),
        session=chat_session,
    )
    pipeline = _CapturingPipeline()
    agent_engine = ChatEngine(
        UiStore(trace_path=tmp_path / "trace.jsonl"),
        backend=PipelineBackend(
            UiStore(trace_path=tmp_path / "trace.jsonl"),
            pipeline_factory=lambda: pipeline,
            poll_interval=0.001,
        ),
        session=agent_session,
    )

    list(chat_engine.stream("Describe the existing project."))
    list(agent_engine.stream("Now update the main screen."))

    context = pipeline.calls[0][1]["conversation_context"]
    assert "Describe the existing project." in context
    assert "The project is a small web app." in context


def test_langgraph_backend_passes_follow_up_context(tmp_path: Path) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def run_goal(goal: str, **kwargs: Any) -> dict[str, Any]:
        calls.append((goal, kwargs))
        return {"goal_id": "goal-1", "status": "needs_attention"}

    session = ChatSession(directory=tmp_path)
    session.append(ChatEvent(kind="command", text="Create a settings page", data={"role": "user"}))
    session.say("The first version is ready.")
    engine = ChatEngine(
        UiStore(trace_path=tmp_path / "trace.jsonl"),
        backend=LangGraphBackend(
            UiStore(trace_path=tmp_path / "trace.jsonl"),
            goal_runner_factory=run_goal,
        ),
        session=session,
    )

    list(engine.stream("Now refine its layout"))

    assert len(calls) == 1
    assert "Create a settings page" in calls[0][1]["conversation_context"]
    assert "The first version is ready." in calls[0][1]["conversation_context"]


def test_classic_goal_backend_forwards_resume_clarification(tmp_path: Path) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def run_goal(goal: str, **kwargs: Any) -> dict[str, Any]:
        calls.append((goal, kwargs))
        return {"goal_id": "goal-1", "status": "needs_attention"}

    backend = PipelineBackend(
        UiStore(trace_path=tmp_path / "trace.jsonl"),
        goal_mode=True,
        goal_runner_factory=run_goal,
        run_kwargs={
            "goal_id": "goal-1",
            "resume": True,
            "additional_context": "Keep the existing navigation.",
            "goal_override": "",
        },
        poll_interval=0.001,
    )
    list(backend.stream("Keep the existing navigation.", ChatSession(directory=tmp_path)))

    assert len(calls) == 1
    assert calls[0][0] == ""
    assert calls[0][1]["goal_id"] == "goal-1"
    assert calls[0][1]["additional_context"] == "Keep the existing navigation."
    assert "goal_override" not in calls[0][1]


def test_langgraph_backend_forwards_resume_clarification(tmp_path: Path) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def run_goal(goal: str, **kwargs: Any) -> dict[str, Any]:
        calls.append((goal, kwargs))
        return {"goal_id": "goal-1", "status": "needs_attention"}

    backend = LangGraphBackend(
        UiStore(trace_path=tmp_path / "trace.jsonl"),
        goal_runner_factory=run_goal,
        run_kwargs={
            "goal_id": "goal-1",
            "resume": True,
            "additional_context": "Keep the existing navigation.",
            "goal_override": "",
        },
    )
    list(backend.stream("Keep the existing navigation.", ChatSession(directory=tmp_path)))

    assert len(calls) == 1
    assert calls[0][0] == ""
    assert calls[0][1]["goal_id"] == "goal-1"
    assert calls[0][1]["additional_context"] == "Keep the existing navigation."
    assert "goal_override" not in calls[0][1]


class _FailingVerifier:
    def verify(self, _root: Path, _changed: list[str], _goal: str) -> VerificationReport:
        return VerificationReport(
            [VerificationCheck("behavior", "failed", "needs another adjustment")]
        )


class _ContextPipeline:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.contexts: list[str] = []

    def run(self, _goal: str, **kwargs: Any) -> SimpleNamespace:
        self.contexts.append(str(kwargs.get("conversation_context") or ""))
        path = self.root / "app.py"
        path.write_text(f"revision = {len(self.contexts)}\n", encoding="utf-8")
        return SimpleNamespace(
            accepted=True,
            status="succeeded",
            changed_files=["app.py"],
            summary="updated",
            error="",
            tokens=1,
            calls=1,
            duration_ms=1.0,
        )


def test_goal_resume_keeps_clarification_without_changing_original_goal(
    tmp_path: Path,
) -> None:
    pipeline = _ContextPipeline(tmp_path)
    runner = GoalRunner(pipeline, repo_root=tmp_path, verifier=_FailingVerifier())

    first = runner.run(
        "Improve the application",
        max_cycles=1,
        conversation_context="User: This is an existing project.",
    )
    assert first.status == "needs_attention"

    runner.run(
        goal_id=first.goal_id,
        resume=True,
        max_cycles=2,
        additional_context="Also preserve the existing navigation.",
    )

    assert len(pipeline.contexts) >= 2
    assert "This is an existing project." in pipeline.contexts[-1]
    assert "Also preserve the existing navigation." in pipeline.contexts[-1]
    saved = GoalStore(tmp_path).load(first.goal_id)
    assert saved["goal"] == "Improve the application"
    assert "Also preserve the existing navigation." in saved["conversation_context"]


def test_langgraph_goal_resume_keeps_clarification(tmp_path: Path) -> None:
    from gigaforge.core.langgraph_goal import run_goal_graph

    pipeline = _ContextPipeline(tmp_path)
    verifier = _FailingVerifier()
    first = run_goal_graph(
        pipeline,
        repo_root=tmp_path,
        verifier=verifier,
        goal="Improve the application",
        max_cycles=1,
        conversation_context="User: This is an existing project.",
    )
    assert first.status == "needs_attention"

    run_goal_graph(
        pipeline,
        repo_root=tmp_path,
        verifier=verifier,
        goal_id=first.goal_id,
        resume=True,
        max_cycles=2,
        additional_context="Also preserve the existing navigation.",
    )

    assert len(pipeline.contexts) >= 2
    assert "This is an existing project." in pipeline.contexts[-1]
    assert "Also preserve the existing navigation." in pipeline.contexts[-1]
    saved = GoalStore(tmp_path).load(first.goal_id)
    assert saved["goal"] == "Improve the application"
    assert "Also preserve the existing navigation." in saved["conversation_context"]
