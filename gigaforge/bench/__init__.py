"""GigaForge bench package — deterministic, free benchmark harness.

The bench runs the pipeline against a set of task fixtures with a
:class:`BenchFakeGigaClient` (no real API calls). Produces :class:`BenchResult`
objects + a SWE-bench-style ASCII report.

CLI: ``gigaforge bench`` runs the default task set + prints the report.
"""
from gigaforge.bench.harness import (
    TASK1,
    TASK2,
    TASK3,
    TASK4,
    TASKS,
    BenchFakeGigaClient,
    BenchResult,
    BenchRunner,
    LooserBudgetTracker,
    TaskFixture,
)
from gigaforge.bench.report import bench_report, bench_report_json

__all__ = [
    "TASK1",
    "TASK2",
    "TASK3",
    "TASK4",
    "TASKS",
    "BenchFakeGigaClient",
    "BenchResult",
    "BenchRunner",
    "LooserBudgetTracker",
    "TaskFixture",
    "bench_report",
    "bench_report_json",
]
