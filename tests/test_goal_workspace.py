

class TestTelemetryOutlivesTheWorkspace:
    """A goal run's record must not be deleted with its scratch copy.

    ``isolate_storage=True`` anchored the database and trace to the isolated
    workspace, and ``cleanup_project_copy()`` deletes that directory after
    publication.  Measured: a completed goal with 6 model calls and 5995 tokens
    left ``runs`` empty, and ``gigaforge ui`` printed "no run", "accepted=no" and
    0/11 steps while showing the correct token count from the trace.
    """

    def test_build_runtime_accepts_a_telemetry_root(self) -> None:
        import inspect

        from gigaforge.runtime import build_runtime

        params = inspect.signature(build_runtime).parameters
        assert "telemetry_root" in params

    def test_the_sink_is_anchored_to_the_telemetry_root(self, tmp_path) -> None:
        from gigaforge.runtime import build_runtime

        repo = tmp_path / "scratch"
        dest = tmp_path / "dest"
        repo.mkdir()
        dest.mkdir()
        runtime = build_runtime(
            repo_root=repo,
            isolate_storage=True,
            telemetry_root=dest,
            probe_models=False,
        )
        try:
            assert str(dest) in runtime.core_config.db_path, (
                "the run database must live in the destination, not the scratch copy"
            )
            assert str(dest) in runtime.core_config.trace_path
        finally:
            runtime.aclose()

    def test_the_goal_runtime_passes_the_destination(self) -> None:
        import inspect

        from gigaforge.runtime import Runtime

        source = inspect.getsource(Runtime._run_isolated_goal)
        assert "telemetry_root=telemetry_root" in source
        assert "telemetry_root = destination" in source
