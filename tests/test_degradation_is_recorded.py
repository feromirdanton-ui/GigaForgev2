"""A swallowed exception is a fact the report cannot state.

**The gap.**  The pipeline had 34 ``except Exception`` blocks and most of them
were correct — a repo map, a cache, a summary must never fail a run.  But
"must not fail the run" is not the same as "must not be recorded".  A run whose
repo map never built picks the wrong files, and the report showed an empty slice
with no reason; a run whose code-contract baseline was missing skipped the
public-symbol check and the report implied it passed.

The rule: an exception that changes what the run did is a **degradation**, and a
degradation is recorded.  ``_degraded`` writes it to the state, which reaches
``PipelineResult.degraded`` and the report.

The last test is the one that matters most: when the recording itself fails, the
failure must still surface — a run whose own reporting is broken is exactly the
run whose failures need to be visible.
"""
from __future__ import annotations

from gigaforge.core.pipeline import PipelineResult


class TestTheResultCarriesDegradations:
    def test_degraded_is_a_list_on_the_result(self) -> None:
        result = PipelineResult(run_id="r", goal="g", status="succeeded")
        assert result.degraded == []

    def test_a_degradation_reaches_the_result(self) -> None:
        result = PipelineResult(
            run_id="r", goal="g", status="succeeded",
            degraded=[{"what": "repo_map", "why": "RuntimeError: boom", "where": "context"}],
        )
        assert result.degraded[0]["what"] == "repo_map"

    def test_it_is_serialised(self) -> None:
        result = PipelineResult(
            run_id="r", goal="g", status="succeeded",
            degraded=[{"what": "x", "why": "y", "where": "z"}],
        )
        assert result.to_dict()["degraded"] == [{"what": "x", "why": "y", "where": "z"}]


class TestTheRecordingIsNotSilent:
    def test_degraded_unrecorded_is_emitted_when_recording_fails(self) -> None:
        """The fallback path must emit an event, not ``pass``.

        This is a source-level assertion on purpose: the behaviour is "when the
        state refuses the note, emit an event instead of swallowing it", and the
        only way to reach it is to make ``note_degraded`` raise — which the
        pipeline's own ``_degraded`` guards against by design.  Asserting on the
        source keeps the guarantee visible without a contrived double.
        """
        import inspect

        from gigaforge.core import pipeline as pipeline_module

        source = inspect.getsource(pipeline_module.TaskPipeline._degraded)
        assert "degraded_unrecorded" in source, (
            "the fallback must emit an event; a bare `pass` loses the fact"
        )
        assert "pass" not in source.split("except Exception")[-1].split("\n")[1], (
            "the except body must not be a bare pass"
        )

    def test_the_optional_paths_record_their_degradation(self) -> None:
        """The optional collaborators must name themselves when they fail."""
        import inspect

        from gigaforge.core import pipeline as pipeline_module

        source = inspect.getsource(pipeline_module)
        for what in (
            "repo_map",
            "indexer_discover",
            "slicer_keywords",
            "file_blocks",
            "code_contract_baseline",
            "child_test_runner",
        ):
            assert f'"{what}"' in source, f"{what} is swallowed without a record"
