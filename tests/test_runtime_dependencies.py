from gigaforge.runtime_dependencies import RuntimeDependencyStatus, ensure_agent_runtime


def test_agent_runtime_probe_reports_missing_dependency(monkeypatch):
    import gigaforge.runtime_dependencies as dependencies

    def missing(module):
        raise ImportError(f"missing {module}")

    monkeypatch.setattr(dependencies.importlib, "import_module", missing)
    status = ensure_agent_runtime(auto_install=False)

    assert not status.available
    assert "LangGraph" in status.detail or "langgraph" in status.detail


def test_runtime_startup_fails_when_required_graph_install_fails(monkeypatch):
    import pytest

    import gigaforge.runtime_dependencies as dependencies
    from gigaforge.runtime import build_runtime

    monkeypatch.setattr(
        dependencies,
        "ensure_agent_runtime",
        lambda: RuntimeDependencyStatus(False, "package installation failed"),
    )

    with pytest.raises(RuntimeError, match="package installation failed"):
        build_runtime()


def test_base_install_includes_required_graph_runtime():
    status = ensure_agent_runtime(auto_install=False)

    assert status.available, status.detail
