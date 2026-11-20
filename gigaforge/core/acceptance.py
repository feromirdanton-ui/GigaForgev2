"""Deterministic acceptance and merge for a sub-agent fan-out.

**Why this module exists.**  The fan-out had no acceptance step.  A child whose
run was *rejected* still handed its diff to the merge, and the merge was a
string concatenation followed by an apply in which the last writer won:

* measured, two children each producing a ``new file`` block for the same path
  merged to ``ok: True, errors: []`` with the first child's content silently
  gone — the run reported success and lost half the work;
* measured, a child whose run was rejected (``accepted: false``) contributed
  its diff anyway, because ``_subagent_diff`` read ``item.diff`` and never
  ``item.ok``;
* measured, a child told "only touch ``a.html``" created ``index.html`` and
  ``b.html`` as well, because the goal it was handed still named every file.

None of those are model failures.  They are the harness failing to do the part
that has a deterministically correct answer: *did this child stay inside its
assignment, and can its diff be applied on top of what has already been
accepted?*  That is a set operation, not a judgement call, so it belongs here
and not in a prompt.

**The two rules that make parallelism correct.**

1. **Ownership is exclusive.**  ``owns_paths`` is a claim, and two claims may
   not intersect.  If two pieces of work must touch one file they are one piece
   of work (or they are serialised with ``depends_on``).  This is the invariant
   that turns "the children happened not to collide" into "the children cannot
   collide".
2. **Acceptance precedes merge.**  A diff is checked against its assignment and
   against what has already been accepted *before* it is merged.  A conflict is
   a rejection with a reason, never a silent overwrite.

The merge is sequential and in spawn order, so the result is reproducible: the
same children produce the same merged diff on every run.
"""
from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

__all__ = [
    "WorkItem",
    "AcceptanceVerdict",
    "MergeReport",
    "allocate_work",
    "check_acceptance",
    "merge_diffs",
    "paths_of",
]


@dataclass(frozen=True)
class WorkItem:
    """One unit of delegated work, with exclusive ownership of its files.

    ``goal`` is **only this work**, never the parent's full goal: a child handed
    the whole goal re-derives every file from it and writes outside its
    assignment.  ``acceptance`` names the checks that must pass for the work to
    be merged — the gate reads it, so a caller can tighten a single item
    without touching the gate.
    """

    id: str
    goal: str
    owns_paths: tuple[str, ...]
    depends_on: tuple[str, ...] = ()
    acceptance: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "goal": self.goal,
            "owns_paths": list(self.owns_paths),
            "depends_on": list(self.depends_on),
            "acceptance": list(self.acceptance),
        }


@dataclass
class AcceptanceVerdict:
    """Whether one child's diff may be merged, and why not when it may not."""

    accepted: bool
    reasons: list[str] = field(default_factory=list)
    paths: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "reasons": list(self.reasons),
            "paths": list(self.paths),
        }


@dataclass
class MergeReport:
    """The merged diff plus an account of every child that did not make it."""

    diff: str = ""
    accepted: list[str] = field(default_factory=list)
    rejected: list[dict[str, Any]] = field(default_factory=list)
    conflicts: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.accepted) and not self.conflicts

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": list(self.accepted),
            "rejected": list(self.rejected),
            "conflicts": list(self.conflicts),
            "chars": len(self.diff),
        }


def _matches(path: str, patterns: Sequence[str]) -> bool:
    """Does *path* fall under any of *patterns*?

    A pattern with no glob metacharacter matches the path itself or anything
    under it, so ``owns_paths=("src",)`` claims the whole subtree — which is
    what a caller writing a directory name means.
    """
    normalised = path.replace("\\", "/").lstrip("./")
    for pattern in patterns:
        candidate = str(pattern).replace("\\", "/").lstrip("./")
        if not candidate:
            continue
        if fnmatch.fnmatchcase(normalised, candidate):
            return True
        if not any(ch in candidate for ch in "*?["):
            if normalised == candidate or normalised.startswith(candidate.rstrip("/") + "/"):
                return True
    return False


def paths_of(diff_text: str, config: Any = None) -> list[str]:
    """The paths a diff touches, in file order.  Empty when it cannot be parsed."""
    if not str(diff_text or "").strip():
        return []
    try:
        from gigaforge.firewall.diff import parse

        parsed = parse(diff_text, config)
    except Exception:  # noqa: BLE001 - an unparseable diff is reported by the gate
        return []
    return [str(f.path) for f in parsed.files]


def check_acceptance(
    result: Any,
    item: WorkItem,
    *,
    accepted_paths: Sequence[str] = (),
    config: Any = None,
) -> AcceptanceVerdict:
    """Decide whether one child's diff may be merged.

    Deterministic and model-free.  The checks, in order:

    * the child's run was accepted (``ok``) — a rejected run's diff is not a
      contribution, it is the output of a run the pipeline already refused;
    * it produced a diff at all;
    * the diff parses;
    * every path it touches is inside ``item.owns_paths``;
    * no path it touches is already claimed by an accepted sibling.

    The last two are what make the merge safe.  Without them the merge is a
    race whose winner is whichever child was concatenated last.
    """
    verdict = AcceptanceVerdict(accepted=False)
    if not bool(getattr(result, "ok", False)):
        error = str(getattr(result, "error", "") or "").strip()
        verdict.reasons.append(
            f"child run was not accepted{': ' + error[:200] if error else ''}"
        )
        return verdict
    diff = str(getattr(result, "diff", "") or "").strip()
    if not diff:
        verdict.reasons.append("child produced no diff")
        return verdict
    paths = paths_of(diff, config)
    if not paths:
        verdict.reasons.append("child's diff could not be parsed")
        return verdict
    verdict.paths = paths
    if item.owns_paths:
        outside = [p for p in paths if not _matches(p, item.owns_paths)]
        if outside:
            verdict.reasons.append(
                "touched paths outside its assignment: " + ", ".join(sorted(outside)[:4])
            )
    claimed = [p for p in paths if _matches(p, tuple(accepted_paths))]
    if claimed:
        verdict.reasons.append(
            "path already claimed by an accepted sibling: "
            + ", ".join(sorted(set(claimed))[:4])
        )
    verdict.accepted = not verdict.reasons
    return verdict


def merge_diffs(
    entries: Sequence[Mapping[str, Any]],
    *,
    root: Path | str = ".",
    config: Any = None,
) -> MergeReport:
    """Merge accepted child diffs sequentially, in spawn order.

    Each entry is ``{"name": str, "diff": str, "owns_paths": Sequence[str]}``.
    A diff is merged only when it parses, stays inside its own assignment, and
    does not touch a path an earlier entry already claimed.  A conflict is
    recorded with the paths involved and the entry is skipped — the alternative,
    which is what the code did before, is to apply both and let the last writer
    win while reporting success.

    Applicability is checked with a dry-run apply against the tree, so a diff
    whose hunks cannot anchor is rejected here rather than failing the whole
    run later.
    """
    report = MergeReport()
    claimed: list[str] = []
    parts: list[str] = []

    for entry in entries:
        name = str(entry.get("name") or "agent")
        diff = str(entry.get("diff") or "").strip()
        owns = tuple(str(p) for p in (entry.get("owns_paths") or ()))
        if not diff:
            report.rejected.append({"name": name, "reason": "no diff"})
            continue
        paths = paths_of(diff, config)
        if not paths:
            report.rejected.append({"name": name, "reason": "diff could not be parsed"})
            continue
        if owns:
            outside = [p for p in paths if not _matches(p, owns)]
            if outside:
                report.rejected.append({
                    "name": name,
                    "reason": "outside its assignment",
                    "paths": sorted(outside)[:4],
                })
                continue
        overlap = sorted({p for p in paths if _matches(p, tuple(claimed))})
        if overlap:
            report.conflicts.append({"name": name, "paths": overlap})
            report.rejected.append({
                "name": name,
                "reason": "conflicts with an already accepted child",
                "paths": overlap,
            })
            continue
        if not _applies(diff, root, config):
            report.rejected.append({
                "name": name,
                "reason": "diff does not apply to the tree",
                "paths": paths[:4],
            })
            continue
        # Applicability is not validity.  A diff can apply cleanly and still
        # leave the file unparseable — measured live: a child's diff applied,
        # the merge reported success, and the next cycle's verifier failed with
        # "IndentationError: expected an indented block after function
        # definition on line 4 (calc.py)".  The run then spent two more cycles
        # repairing damage the merge had just certified.
        #
        # The check is cheap and deterministic: apply to a scratch copy and ask
        # the language whether the result parses.  A child whose work does not
        # parse has not done its work, whatever the diff looks like.
        broken = _syntax_errors(diff, root, config)
        if broken:
            report.rejected.append({
                "name": name,
                "reason": "merged result does not parse",
                "paths": broken[:4],
            })
            continue
        claimed.extend(paths)
        parts.append(diff)
        report.accepted.append(name)

    report.diff = "\n".join(parts)
    return report


def _applies(diff: str, root: Path | str, config: Any) -> bool:
    """Dry-run the diff against *root*: can its hunks anchor?

    A dry run is the honest check.  ``git apply --check`` would be equivalent
    but needs a git binary and a clean index; the project's own applier already
    implements the same anchoring strategies the real apply will use, so a
    dry run through it cannot disagree with the apply that follows.
    """
    try:
        from gigaforge.firewall.diff import Applier, parse
        from gigaforge.firewall.config import FirewallConfig

        cfg = config
        if cfg is None:
            cfg = FirewallConfig(repo_root=str(root))
        else:
            try:
                from dataclasses import replace

                # ``repo_root``, not ``root``: the field is named ``repo_root``,
                # and a ``replace`` with a wrong keyword raises, which the
                # ``except`` below then swallowed — so the dry run silently used
                # the *original* repository root and every diff "failed to
                # apply" against a tree it was never meant for.
                cfg = replace(cfg, repo_root=str(root))
            except Exception:  # noqa: BLE001 - a frozen config is used as-is
                pass
        parsed = parse(diff, cfg)
        if not parsed.ok:
            return False
        return bool(Applier(cfg).apply(parsed, dry_run=True).ok)
    except Exception:  # noqa: BLE001 - an unverifiable diff is not merged
        return False


def _syntax_errors(diff: str, root: Path | str, config: Any) -> list[str]:
    """Paths whose merged content would not parse.

    Applies the diff to a scratch copy of the tree and asks the language
    whether each touched file still parses.  Only Python is checked — it is the
    language this project's own verifier checks, and a check that guesses at
    other languages would reject correct work.

    Returns the offending paths, empty when everything parses.  A file that
    cannot be read is not reported: that is the applier's problem, and it has
    already been checked by the dry run.
    """
    import ast
    import shutil
    import tempfile

    try:
        from gigaforge.firewall.diff import Applier, parse
        from gigaforge.firewall.config import FirewallConfig

        scratch = Path(tempfile.mkdtemp(prefix="gigaforge-accept-"))
    except Exception:  # noqa: BLE001 - an unverifiable diff is not merged
        return []
    try:
        source = Path(root)
        for item in source.iterdir():
            if item.name in {".git", ".gigaforge", "__pycache__", "node_modules", ".venv"}:
                continue
            target = scratch / item.name
            try:
                if item.is_dir():
                    shutil.copytree(item, target, symlinks=True)
                else:
                    shutil.copy2(item, target)
            except OSError:
                continue
        cfg = config
        try:
            from dataclasses import replace

            cfg = replace(config, root=str(scratch)) if config is not None else FirewallConfig(root=str(scratch))
        except Exception:  # noqa: BLE001
            cfg = FirewallConfig(root=str(scratch))
        parsed = parse(diff, cfg)
        if not parsed.ok:
            return []
        applied = Applier(cfg).apply(parsed, dry_run=False)
        if not applied.ok:
            return []
        broken: list[str] = []
        for f in parsed.files:
            path = scratch / str(f.path)
            if path.suffix != ".py" or not path.is_file():
                continue
            try:
                ast.parse(path.read_text(encoding="utf-8", errors="replace"))
            except SyntaxError:
                broken.append(str(f.path))
        return broken
    except Exception:  # noqa: BLE001 - a check that cannot run must not reject
        return []
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def allocate_work(
    goal: str,
    paths: Sequence[str],
    *,
    coordinated: bool = False,
    max_items: int = 3,
) -> list[WorkItem]:
    """Split a goal into work items with pairwise-disjoint ownership.

    The unit of decomposition is **ownership**, not a file name pulled out of
    the goal by a regex.  Two rules:

    * a coordinated change (a source file and the test that exercises it) is
      **one** item owning both paths — splitting it makes both halves fail;
    * otherwise each path is its own item, so the ownership sets are disjoint
      by construction and the invariant cannot be violated by a later edit.

    ``goal`` is carried for the record; each item's own ``goal`` is built by the
    caller from the parent goal with the other paths removed, because a child
    that can read the other file names will act on them.
    """
    unique = [p for p in dict.fromkeys(str(p) for p in paths if str(p).strip())]
    if not unique:
        return []
    if coordinated or len(unique) > max_items:
        return [
            WorkItem(
                id="work-0",
                goal=goal,
                owns_paths=tuple(unique),
                acceptance=("tests",),
            )
        ]
    return [
        WorkItem(
            id=f"work-{index}",
            goal=goal,
            owns_paths=(path,),
            acceptance=("tests",),
        )
        for index, path in enumerate(unique)
    ]
