from __future__ import annotations

import time
from datetime import date, datetime
from pathlib import Path

import pytest

from gigaforge.core.models import ModelCatalog, RoleModels, fetch_models
from gigaforge.desktop.qt_app import (
    _chat_history_date_group,
    _classify_workspace_intent,
    _compose_agent_profile_prompt,
    _compose_attachment_prompt,
    _consume_goal_command,
    _read_chat_title,
    _write_chat_title,
)


class ModelClient:
    def __init__(self, models: list[dict[str, str]] | None = None, error: Exception | None = None):
        self.models = models or []
        self.error = error

    def list_models(self) -> list[dict[str, str]]:
        if self.error:
            raise self.error
        return self.models


def test_model_catalog_reads_live_api_choices(tmp_path: Path) -> None:
    catalog = fetch_models(
        ModelClient([{"id": "Model-Pro"}, {"name": "Model-Lite"}, {"id": "Model-Pro"}]),
        path=tmp_path / "models.json",
        force=True,
    )

    assert catalog.source == "live"
    assert catalog.models == ["Model-Lite", "Model-Pro"]
    assert catalog.error == ""


def test_model_catalog_reports_api_error_when_using_cache(tmp_path: Path) -> None:
    path = tmp_path / "models.json"
    ModelCatalog(
        models=["Cached-Pro"],
        roles=RoleModels(strong="Cached-Pro"),
        fetched_at=time.time(),
        source="live",
    ).save(path)

    catalog = fetch_models(
        ModelClient(error=RuntimeError("provider unavailable")),
        path=path,
        force=True,
    )

    assert catalog.source == "cache"
    assert catalog.models == ["Cached-Pro"]
    assert "provider unavailable" in catalog.error


def test_model_catalog_reapplies_configured_preferences_to_cached_models(
    tmp_path: Path,
) -> None:
    path = tmp_path / "models.json"
    models = [
        {"id": "GigaChat"},
        {"id": "GigaChat-2"},
        {"id": "GigaChat-2-Pro"},
        {"id": "GigaChat-2-Max"},
        {"id": "GigaChat-Plus"},
    ]
    fetch_models(ModelClient(models), path=path, force=True)

    catalog = fetch_models(
        ModelClient(error=RuntimeError("should use fresh cache")),
        path=path,
        configured_strong="GigaChat-2-Pro",
        configured_default="GigaChat-2",
    )

    assert catalog.source == "cache"
    assert catalog.roles.strong == "GigaChat-2-Pro"
    assert catalog.roles.default == "GigaChat-2"


def test_runtime_prefers_configured_available_model(tmp_path: Path, monkeypatch) -> None:
    from dataclasses import replace

    from gigaforge.config import get_settings
    from gigaforge.core.config import CoreConfig
    from gigaforge.runtime import build_runtime

    for key in (
        "GF_CORE__CHEAP_MODEL",
        "GF_CORE__STRONG_MODEL",
        "GF_CORE__DEFAULT_MODEL",
    ):
        monkeypatch.delenv(key, raising=False)

    class FakeClient(ModelClient):
        def aclose(self) -> None:
            pass

    client = FakeClient([
        {"id": "GigaChat"},
        {"id": "GigaChat-2"},
        {"id": "GigaChat-2-Pro"},
        {"id": "GigaChat-2-Max"},
        {"id": "GigaChat-Plus"},
    ])
    monkeypatch.setattr(
        "gigaforge.runtime.build_client",
        lambda settings, metrics=None: (client, None, None, object()),
    )
    settings = replace(
        get_settings(),
        auth_key="synthetic-test-credential",
        models={
            "plan": "GigaChat-3-Lightning",
            "code": "GigaChat-2-Pro",
            "explain": "GigaChat-2",
        },
    )
    runtime = build_runtime(
        settings=settings,
        core_config=CoreConfig.from_env({}),
        require_provider=True,
        repo_root=tmp_path,
        probe_models=True,
        isolate_storage=True,
        telemetry_root=tmp_path,
    )
    try:
        assert runtime.catalog.roles.strong == "GigaChat-2-Pro"
        assert runtime.core_config.route_for("code").model == "GigaChat-2-Pro"
        assert runtime.core_config.default_model == "GigaChat-2"
    finally:
        runtime.aclose()


def test_goal_command_is_consumed_only_at_the_start_of_composer_text() -> None:
    assert _consume_goal_command("/goal") == ""
    assert _consume_goal_command("/GOAL  Verify the saved result") == "Verify the saved result"
    assert _consume_goal_command("/goal\nVerify the saved result") == "Verify the saved result"
    assert _consume_goal_command("Please run /goal later") is None
    assert _consume_goal_command("/goalkeeper") is None


@pytest.mark.parametrize(
    ("prompt", "expected"),
    [
        ("Напиши короткое письмо клиенту", "writing"),
        ("Inspect the repository and fix the failing tests", "codebase"),
        ("Автоматизируй параллельный запуск проверок", "automation"),
        ("What is a good lunch?", "general"),
        ("", "general"),
    ],
)
def test_workspace_intent_classification(prompt: str, expected: str) -> None:
    assert _classify_workspace_intent(prompt) == expected


def test_agent_profile_instructions_are_applied_to_the_actual_task() -> None:
    composed = _compose_agent_profile_prompt(
        "Refactor the parser.",
        {
            "name": "Careful maintainer",
            "instructions": "Preserve public APIs and add focused tests.",
        },
    )

    assert "Selected agent profile: Careful maintainer" in composed
    assert "Preserve public APIs and add focused tests." in composed
    assert "User task:\nRefactor the parser." in composed


def test_chat_history_groups_sessions_by_local_calendar_day() -> None:
    today = date(2026, 6, 15)
    def timestamp(day: int) -> float:
        return datetime(2026, 6, day, 12).timestamp()

    assert _chat_history_date_group(timestamp(15), today) == "today"
    assert _chat_history_date_group(timestamp(14), today) == "yesterday"
    assert _chat_history_date_group(timestamp(10), today) == "this_week"
    assert _chat_history_date_group(timestamp(8), today) == "older"


def test_chat_title_can_be_renamed_without_changing_its_session_id(tmp_path: Path) -> None:
    chat_dir = tmp_path / ".gigaforge" / "chat"
    chat_dir.mkdir(parents=True)
    transcript = chat_dir / "session-1.jsonl"
    transcript.write_text('{"kind":"command","text":"first prompt"}\n', encoding="utf-8")

    title = _write_chat_title(chat_dir, "session-1", "  Новый   заголовок  ")

    assert title == "Новый заголовок"
    assert _read_chat_title(chat_dir, "session-1") == "Новый заголовок"
    assert transcript.exists()
    assert transcript.read_text(encoding="utf-8").endswith("first prompt\"}\n")


@pytest.mark.parametrize("title", ["", "  \n  ", "x" * 81])
def test_chat_title_rejects_empty_or_oversized_names(tmp_path: Path, title: str) -> None:
    chat_dir = tmp_path / ".gigaforge" / "chat"
    chat_dir.mkdir(parents=True)
    (chat_dir / "session-1.jsonl").write_text("", encoding="utf-8")

    with pytest.raises(ValueError):
        _write_chat_title(chat_dir, "session-1", title)


def test_attachment_prompt_inlines_text_and_exposes_project_relative_files(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    attached = project / "notes.md"
    attached.write_text("A useful reference.", encoding="utf-8")

    composed, files = _compose_attachment_prompt(
        "Summarize this reference.",
        [str(attached)],
        project,
    )

    assert "A useful reference." in composed
    assert "untrusted reference data" in composed
    assert files == ("notes.md",)


def test_attachment_prompt_rejects_sensitive_files(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    secret = project / ".env"
    secret.write_text("TOKEN=do-not-send", encoding="utf-8")

    with pytest.raises(ValueError, match="секретами"):
        _compose_attachment_prompt("Review this.", [str(secret)], project)
