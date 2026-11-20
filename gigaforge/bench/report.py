"""SWE-bench-style report renderer for the GigaForge benchmark harness.

Renders a :class:`BenchResult` list as a table:

    task                       success  tests_passed  llm_calls  tokens  latency_s
    ---------------------------------------------------------------------------------
    toy-validate-email         ✓        ✓             3          450     0.234
    toy-add-fix                ✓        ✓             3          420     0.198
    ...

The table is plain ASCII (no rich dependency in this module — keeps the bench
report testable without a TTY). The caller can wrap it in rich panels if
needed.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # avoid circular import at runtime
    from gigaforge.bench.harness import BenchResult

__all__ = ["bench_report"]


def _ok(b: bool) -> str:
    return "✓" if b else "✗"


def bench_report(results: list[BenchResult]) -> str:
    """Render a SWE-bench-style ASCII table + summary line.

    Each row: task, success, tests_passed, llm_calls, tokens_total, latency_s.
    Summary: total tasks, success rate, total tokens, total latency, avg tokens per success.
    """
    if not results:
        return "(no bench results)"

    # Column widths.
    name_w = max(len("task"), max(len(r.name) for r in results))
    succ_w = len("success")
    tests_w = len("tests_passed")
    calls_w = len("llm_calls")
    tok_w = len("tokens_total")
    lat_w = len("latency_s")

    sep = "-" * (name_w + succ_w + tests_w + calls_w + tok_w + lat_w + 6 * 3 + 1)

    lines: list[str] = []
    lines.append(f"{'task'.ljust(name_w)}   {'success'.ljust(succ_w)}   "
                 f"{'tests_passed'.ljust(tests_w)}   {'llm_calls'.rjust(calls_w)}   "
                 f"{'tokens_total'.rjust(tok_w)}   {'latency_s'.rjust(lat_w)}")
    lines.append(sep)

    total_success = 0
    total_tokens = 0
    total_latency = 0.0
    total_llm_calls = 0
    total_tests_passed = 0

    for r in results:
        row_tokens = r.tokens_in + r.tokens_out
        row_str = (
            f"{r.name.ljust(name_w)}   "
            f"{_ok(r.success).ljust(succ_w)}   "
            f"{_ok(r.tests_passed_after_apply).ljust(tests_w)}   "
            f"{str(r.llm_calls).rjust(calls_w)}   "
            f"{str(row_tokens).rjust(tok_w)}   "
            f"{f'{r.latency_s:.3f}'.rjust(lat_w)}"
        )
        if r.error:
            row_str += f"   err={r.error[:80]}"
        lines.append(row_str)
        if r.success:
            total_success += 1
        if r.tests_passed_after_apply:
            total_tests_passed += 1
        total_tokens += row_tokens
        total_latency += r.latency_s
        total_llm_calls += r.llm_calls

    lines.append(sep)
    success_rate = total_success / len(results) if results else 0.0
    test_rate = total_tests_passed / len(results) if results else 0.0
    avg_tokens = total_tokens / total_success if total_success > 0 else 0
    lines.append(
        f"total: {len(results)} task(s) | success: {total_success}/{len(results)} "
        f"({success_rate:.0%}) | tests_passed: {total_tests_passed}/{len(results)} "
        f"({test_rate:.0%}) | llm_calls: {total_llm_calls} | tokens: {total_tokens} "
        f"(avg {avg_tokens:.0f}/success) | latency: {total_latency:.3f}s"
    )

    return "\n".join(lines)


def bench_report_json(results: list[BenchResult]) -> list[dict[str, Any]]:
    """Machine-readable JSON rows (one per task) for downstream tooling."""
    return [r.to_row() for r in results]
