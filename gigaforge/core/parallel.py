"""Wave (level-based) DAG scheduling (V2).

V1's :class:`~gigaforge.core.dag.DagExecutor` walks the topological order one
node at a time.  That is correct, but it leaves the machine idle: ``index`` and
``slice`` do not depend on each other, and a fan-out of sub-agents is
embarrassingly parallel.

V2 adds a *wave* scheduler.  Nodes are grouped into levels by their artifact
dependencies; every node in a level has all of its inputs already produced, so
the whole level can run concurrently.  Two invariants are carried over from V1:

* **determinism** — results are returned in declared order, never completion
  order, so a trace is reproducible and a test cannot flake on thread timing;
* **isolation** — a node only ever sees the artifacts it declared in
  ``requires``, so running two nodes at once cannot leak state between them.

The scheduler is deliberately transport-agnostic: it takes a ``run_node``
callable, so the same wave logic drives the real executor, a dry-run planner and
the tests without any of them sharing a thread pool.
"""
from __future__ import annotations

import concurrent.futures as futures
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional, Sequence

from .contracts import NodeSpec
from .errors import CycleError

NodeFn = Callable[[str, NodeSpec, Mapping[str, Any]], Mapping[str, Any]]


def _producers(nodes: Sequence[NodeSpec]) -> dict[str, str]:
    out: dict[str, str] = {}
    for spec in nodes:
        for artifact in spec.provides:
            out.setdefault(artifact, spec.name)
    return out


def compute_levels(nodes: Sequence[NodeSpec]) -> list[list[str]]:
    """Group nodes into dependency levels.

    Level *k* contains exactly the nodes whose every dependency lives in levels
    ``< k``.  Ties are broken by declared order so the grouping is stable.
    """
    declared = [s.name for s in nodes]
    index = {name: i for i, name in enumerate(declared)}
    producer = _producers(nodes)

    deps: dict[str, set[str]] = {}
    for spec in nodes:
        needed: set[str] = set()
        for artifact in spec.requires:
            source = producer.get(artifact)
            if source and source != spec.name:
                needed.add(source)
        deps[spec.name] = needed

    levels: list[list[str]] = []
    done: set[str] = set()
    remaining = dict(deps)
    while remaining:
        ready = sorted(
            (name for name, needed in remaining.items() if needed <= done),
            key=lambda name: index[name],
        )
        if not ready:
            raise CycleError(
                "the graph is not acyclic",
                detail={"unresolved": sorted(remaining)},
            )
        levels.append(ready)
        for name in ready:
            done.add(name)
            remaining.pop(name)
    return levels


@dataclass
class WaveResult:
    """Outcome of a wave run."""

    levels: list[list[str]] = field(default_factory=list)
    outputs: dict[str, Mapping[str, Any]] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)
    order: list[str] = field(default_factory=list)
    max_workers: int = 1
    stopped_early: bool = False

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "levels": [list(level) for level in self.levels],
            "order": list(self.order),
            "max_workers": self.max_workers,
            "stopped_early": self.stopped_early,
            "errors": dict(self.errors),
            "produced": sorted(self.outputs),
        }


class WaveScheduler:
    """Runs a DAG level by level, with bounded concurrency inside a level."""

    def __init__(
        self,
        nodes: Sequence[NodeSpec],
        *,
        max_workers: int = 4,
    ) -> None:
        self.nodes: dict[str, NodeSpec] = {spec.name: spec for spec in nodes}
        self.declared: list[str] = [spec.name for spec in nodes]
        self.levels: list[list[str]] = compute_levels(nodes)
        self.max_workers = max(1, int(max_workers))

    # ── execution ─────────────────────────────────────────────────────────────
    def run(
        self,
        run_node: NodeFn,
        *,
        initial: Optional[Mapping[str, Any]] = None,
        stop_on_error: bool = True,
    ) -> WaveResult:
        """Execute every level, merging each level's outputs before the next."""
        artifacts: dict[str, Any] = dict(initial or {})
        result = WaveResult(
            levels=[list(level) for level in self.levels],
            max_workers=self.max_workers,
        )

        for level in self.levels:
            if len(level) == 1 or self.max_workers == 1:
                produced = {name: self._invoke(run_node, name, artifacts) for name in level}
            else:
                produced = self._run_level(run_node, level, artifacts)

            for name in level:
                outcome = produced.get(name)
                if isinstance(outcome, Mapping):
                    artifacts.update(outcome)
                    result.outputs[name] = outcome
                    result.order.append(name)
                else:
                    result.errors[name] = str(outcome)
                    if stop_on_error:
                        result.stopped_early = True
                        return result
        return result

    def _run_level(
        self,
        run_node: NodeFn,
        level: Sequence[str],
        artifacts: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Run one level concurrently, returning results keyed by node name.

        The snapshot of ``artifacts`` is taken *before* the level starts, so two
        nodes in the same level can never observe each other's partial output —
        which is exactly the guarantee that makes a level safe to parallelise.
        """
        snapshot = dict(artifacts)
        produced: dict[str, Any] = {}
        with futures.ThreadPoolExecutor(
            max_workers=min(self.max_workers, len(level)),
            thread_name_prefix="wave",
        ) as pool:
            future_map = {
                pool.submit(self._invoke, run_node, name, snapshot): name for name in level
            }
            for future in futures.as_completed(future_map):
                name = future_map[future]
                try:
                    produced[name] = future.result()
                except BaseException as exc:  # noqa: BLE001 - reported per node
                    produced[name] = f"{type(exc).__name__}: {exc}"
        return produced

    def _invoke(
        self, run_node: NodeFn, name: str, artifacts: Mapping[str, Any]
    ) -> Any:
        spec = self.nodes[name]
        inputs = {artifact: artifacts.get(artifact) for artifact in spec.requires}
        try:
            return run_node(name, spec, inputs)
        except BaseException as exc:  # noqa: BLE001 - reported, not raised
            return f"{type(exc).__name__}: {exc}"

    def describe(self) -> dict[str, Any]:
        return {
            "levels": [list(level) for level in self.levels],
            "max_workers": self.max_workers,
            "nodes": len(self.nodes),
            "critical_path": len(self.levels),
        }

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return (
            f"WaveScheduler(nodes={len(self.nodes)}, levels={len(self.levels)}, "
            f"max_workers={self.max_workers})"
        )


__all__ = ["WaveScheduler", "WaveResult", "compute_levels"]
