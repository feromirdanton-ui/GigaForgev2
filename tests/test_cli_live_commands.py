from __future__ import annotations

from types import SimpleNamespace

import pytest
import typer

from gigaforge.cli.app import models, smoke


class _FakeClient:
    def __init__(self) -> None:
        self.list_models_called = False
        self.chat_called = False
        self.chat_model: object = None

    def list_models(self) -> list[dict[str, str]]:
        self.list_models_called = True
        return [{"id": "test-model", "owned_by": "test"}]

    def complete(
        self, messages: list[dict[str, str]], **kwargs: object
    ) -> SimpleNamespace:
        self.chat_called = True
        self.chat_model = kwargs.get("model")
        return SimpleNamespace(content="pong", usage={"total_tokens": 2})


class _FakeRuntime:
    def __init__(self) -> None:
        self.client = _FakeClient()
        self.token_provider = SimpleNamespace(
            get_token=lambda: "secret-access-token"
        )
        self.pipeline = SimpleNamespace(
            router=SimpleNamespace(
                route=lambda phase: SimpleNamespace(model="test-model")
            )
        )
        self.closed = False

    def aclose(self) -> None:
        self.closed = True


def test_models_command_uses_sync_adapter_and_closes_runtime(monkeypatch, capsys) -> None:
    from gigaforge import runtime as runtime_module

    runtime = _FakeRuntime()
    options: dict[str, object] = {}

    def build_runtime(**kwargs: object) -> _FakeRuntime:
        options.update(kwargs)
        return runtime

    monkeypatch.setattr(runtime_module, "build_runtime", build_runtime)

    models()

    assert options["require_provider"] is True
    assert runtime.client.list_models_called
    assert runtime.closed
    assert "test-model" in capsys.readouterr().out


def test_smoke_command_uses_valid_async_boundaries_without_exposing_token(
    monkeypatch, capsys
) -> None:
    from gigaforge import runtime as runtime_module

    runtime = _FakeRuntime()
    options: dict[str, object] = {}

    def build_runtime(**kwargs: object) -> _FakeRuntime:
        options.update(kwargs)
        return runtime

    monkeypatch.setattr(runtime_module, "build_runtime", build_runtime)

    with pytest.raises(typer.Exit) as exit_info:
        smoke()

    output = capsys.readouterr().out
    assert exit_info.value.exit_code == 0
    assert options["require_provider"] is True
    assert "[1/3] OAuth ok" in output
    assert "[2/3] models: 1 available" in output
    assert "[3/3] chat ok: 'pong'" in output
    assert "secret-access-token" not in output
    assert runtime.client.list_models_called
    assert runtime.client.chat_called
    assert runtime.client.chat_model == "test-model"
    assert runtime.closed
