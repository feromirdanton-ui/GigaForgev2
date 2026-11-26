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

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Sequence

if TYPE_CHECKING:  # avoid circular import at runtime
    from gigaforge.bench.harness import BenchResult

__all__ = [
    "bench_report",
    "bench_report_json",
    "DeltaVerdict",
    "noise_floor",
    "delta_report",
]


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
    cost_w = len("cost_usd")
    cache_w = len("cache")

    sep = "-" * (name_w + succ_w + tests_w + calls_w + tok_w + lat_w + cost_w
                 + cache_w + 8 * 3 + 1)

    lines: list[str] = []
    lines.append(f"{'task'.ljust(name_w)}   {'success'.ljust(succ_w)}   "
                 f"{'tests_passed'.ljust(tests_w)}   {'llm_calls'.rjust(calls_w)}   "
                 f"{'tokens_total'.rjust(tok_w)}   {'latency_s'.rjust(lat_w)}   "
                 f"{'cost_usd'.rjust(cost_w)}   {'cache'.rjust(cache_w)}")
    lines.append(sep)

    total_success = 0
    total_tokens = 0
    total_latency = 0.0
    total_llm_calls = 0
    total_tests_passed = 0
    total_cost_micros = 0
    total_cache_hits = 0
    total_cache_lookups = 0

    for r in results:
        row_tokens = r.tokens_in + r.tokens_out
        row_str = (
            f"{r.name.ljust(name_w)}   "
            f"{_ok(r.success).ljust(succ_w)}   "
            f"{_ok(r.tests_passed_after_apply).ljust(tests_w)}   "
            f"{str(r.llm_calls).rjust(calls_w)}   "
            f"{str(row_tokens).rjust(tok_w)}   "
            f"{f'{r.latency_s:.3f}'.rjust(lat_w)}   "
            f"{f'{r.cost_usd:.4f}'.rjust(cost_w)}   "
            f"{f'{r.cache_hit_rate:.0%}'.rjust(cache_w)}"
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
        total_cost_micros += r.cost_micros
        total_cache_hits += r.cache_hits
        total_cache_lookups += r.cache_lookups

    lines.append(sep)
    success_rate = total_success / len(results) if results else 0.0
    test_rate = total_tests_passed / len(results) if results else 0.0
    avg_tokens = total_tokens / total_success if total_success > 0 else 0
    cache_rate = (
        total_cache_hits / total_cache_lookups if total_cache_lookups else 0.0
    )
    lines.append(
        f"total: {len(results)} task(s) | success: {total_success}/{len(results)} "
        f"({success_rate:.0%}) | tests_passed: {total_tests_passed}/{len(results)} "
        f"({test_rate:.0%}) | llm_calls: {total_llm_calls} | tokens: {total_tokens} "
        f"(avg {avg_tokens:.0f}/success) | latency: {total_latency:.3f}s | "
        f"cost: ${total_cost_micros / 1_000_000:.4f} "
        f"(${total_cost_micros / 1_000_000 / len(results):.4f}/task) | "
        f"cache: {cache_rate:.0%} of {total_cache_lookups} lookup(s)"
    )

    return "\n".join(lines)


def bench_report_json(results: list[BenchResult]) -> list[dict[str, Any]]:
    """Machine-readable JSON rows (one per task) for downstream tooling."""
    return [r.to_row() for r in results]


# ── noise-adjusted floor ─────────────────────────────────────────────────────
@dataclass
class DeltaVerdict:
    """Whether a change in pass rate is bigger than the noise that produced it.

    **The defect this answers.**  The bench reported "5/5" and "6/6" as if they
    were different facts.  They are not: with a stochastic pipeline, a task that
    passes 4 times in 5 and a task that passes 5 times in 5 are the same task
    observed twice.  A harness that "improved" from 4/5 to 5/5 has learned
    nothing, and a harness that "regressed" from 5/5 to 4/5 has broken nothing.
    Without a floor, every run is a reason to celebrate or panic, and both are
    wrong.

    **The floor.**  The standard error of a pass rate over ``k`` attempts is
    ``sqrt(p(1-p)/k)``.  Two runs differ by more than noise when the gap exceeds
    ``z * sqrt(se_before^2 + se_after^2)``.

    ``z = 1.96`` (95%) is the default, and the choice is measured rather than
    conventional.  At ``z = 1.0`` the exact case this module exists for — 4/5
    becoming 5/5 — computes a delta of 0.20 against a floor of 0.179 and is
    called a real change.  That is the false positive the floor is supposed to
    prevent, so one sigma is too loose.  At 95% the same comparison has a floor
    of 0.35 and is correctly reported as noise, while a genuine collapse
    (5/5 → 0/5) still clears it with room to spare.
    """

    task: str
    before: float
    after: float
    delta: float
    floor: float
    significant: bool
    attempts_before: int
    attempts_after: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "task": self.task,
            "before": round(self.before, 4),
            "after": round(self.after, 4),
            "delta": round(self.delta, 4),
            "floor": round(self.floor, 4),
            "significant": self.significant,
            "attempts_before": self.attempts_before,
            "attempts_after": self.attempts_after,
        }


def _standard_error(rate: float, attempts: int) -> float:
    if attempts <= 0:
        return 0.0
    return math.sqrt(max(0.0, rate * (1.0 - rate)) / attempts)


def noise_floor(
    before: Sequence[BenchResult],
    after: Sequence[BenchResult],
    *,
    z: float = 1.96,
) -> list[DeltaVerdict]:
    """Compare two bench runs task by task, and say what actually changed.

    A task present in only one run is reported with the other side at 0 and
    ``significant=True`` only when the gap clears the floor — a task that was
    added is a real change, but a task that was *renamed* is not, and the caller
    sees both as "present in one run" and can judge.
    """
    by_before = {r.name: r for r in before}
    by_after = {r.name: r for r in after}
    verdicts: list[DeltaVerdict] = []
    for name in sorted(set(by_before) | set(by_after)):
        b = by_before.get(name)
        a = by_after.get(name)
        rate_b = b.pass_at_k if b is not None else 0.0
        rate_a = a.pass_at_k if a is not None else 0.0
        k_b = max(1, b.attempts) if b is not None else 1
        k_a = max(1, a.attempts) if a is not None else 1
        se = math.sqrt(_standard_error(rate_b, k_b) ** 2
                       + _standard_error(rate_a, k_a) ** 2)
        floor = z * se
        delta = rate_a - rate_b
        verdicts.append(DeltaVerdict(
            task=name,
            before=rate_b,
            after=rate_a,
            delta=delta,
            floor=floor,
            significant=abs(delta) > floor,
            attempts_before=k_b,
            attempts_after=k_a,
        ))
    return verdicts


def delta_report(verdicts: Sequence[DeltaVerdict]) -> str:
    """Render the comparison, separating real changes from noise."""
    if not verdicts:
        return "(no tasks to compare)"
    lines = [
        f"{'task':<28} {'before':>8} {'after':>8} {'delta':>8} {'floor':>8}  verdict",
        "-" * 78,
    ]
    real = 0
    for v in verdicts:
        verdict = "CHANGED" if v.significant else "within noise"
        if v.significant:
            real += 1
        lines.append(
            f"{v.task:<28} {v.before:>8.2f} {v.after:>8.2f} "
            f"{v.delta:>+8.2f} {v.floor:>8.2f}  {verdict}"
        )
    lines.append("-" * 78)
    lines.append(
        f"{real} of {len(verdicts)} task(s) changed by more than the noise floor; "
        f"the rest are the same result observed twice"
    )
    return "\n".join(lines)
