"""Who owns a file the harness wrote — expressed once, in a type.

**The defect.**  Three mechanisms solved one problem, and none of them said what
the problem was:

1. ``_drop_hunks_for(diff, owned)`` removed the model's hunks for an owned path;
2. ``_merge_repaired_diff(diff, _keep_hunks_for(prefetched, owned))`` put the
   harness's own hunks back;
3. ``_restore_harness_owned(ctx)`` wrote the harness's bytes over whatever the
   fix loop had applied.

Each was added to fix a live failure of the previous one, and the invariant they
were all approximating — *the harness's assembled page is the artifact; the
model's answer is a guess at the same file* — lived only in comments.  The
consequence the review found: ``_restore_harness_owned`` overwrote the file with
the harness's content **after** the model's diff, so a model that fixed a real
bug in that file had its fix silently discarded.  Ownership was absolute because
nothing in the code could say it was not.

**The fix is a type.**  A file the harness wrote is an :class:`OwnedFile` with a
:class:`Policy`:

* ``AUTHORITATIVE`` — the harness assembled this file from a plan.  Its bytes
  are the artifact; a model edit to it is a regression and is discarded.
* ``SEED`` — the harness wrote this file as a *starting point* (an existing page
  being edited, a scaffold).  The model's edits are the work; the harness's
  bytes are only the baseline, and are never written back over them.

One function, :meth:`OwnedFiles.resolve`, applies the whole invariant to a diff
and a tree.  The three mechanisms become one call, and the policy is a value a
test can assert on instead of a comment a reader has to trust.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping

__all__ = ["Policy", "OwnedFile", "OwnedFiles", "Resolution"]


class Policy(str, enum.Enum):
    """What the harness's bytes mean for a file it wrote."""

    #: The harness assembled this file from a plan.  Its content IS the
    #: artifact: a model edit to it is a regression, not an improvement.
    AUTHORITATIVE = "authoritative"
    #: The harness wrote this file as a baseline.  The model's edits are the
    #: work; the harness's bytes are never written back over them.
    SEED = "seed"


@dataclass(frozen=True)
class OwnedFile:
    """A file the harness wrote, and what its content means."""

    path: str
    content: str
    policy: Policy = Policy.AUTHORITATIVE

    @property
    def authoritative(self) -> bool:
        return self.policy is Policy.AUTHORITATIVE


@dataclass
class Resolution:
    """What :meth:`OwnedFiles.resolve` decided, for the trace and the report."""

    #: Paths whose model hunks were dropped (authoritative files).
    dropped: list[str] = field(default_factory=list)
    #: Paths whose harness content was written back over the tree.
    restored: list[str] = field(default_factory=list)
    #: Paths whose harness hunks were put back into the diff.
    harness_hunks_kept: list[str] = field(default_factory=list)
    #: Paths the model was allowed to change (seed files).
    model_kept: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, list[str]]:
        return {
            "dropped": list(self.dropped),
            "restored": list(self.restored),
            "harness_hunks_kept": list(self.harness_hunks_kept),
            "model_kept": list(self.model_kept),
        }


@dataclass
class OwnedFiles:
    """The set of files the harness wrote, with their policies."""

    files: dict[str, OwnedFile] = field(default_factory=dict)

    @classmethod
    def from_mapping(
        cls,
        content: Mapping[str, str] | None,
        *,
        policy: Policy = Policy.AUTHORITATIVE,
        policies: Mapping[str, Policy] | None = None,
    ) -> "OwnedFiles":
        """Build from the ``{path: content}`` shape the pipeline already keeps.

        ``policies`` overrides the default per path, which is how an edit of an
        existing page is marked ``SEED`` while a freshly assembled page stays
        ``AUTHORITATIVE``.
        """
        out: dict[str, OwnedFile] = {}
        for path, text in (content or {}).items():
            chosen = (policies or {}).get(path, policy)
            out[str(path)] = OwnedFile(path=str(path), content=str(text), policy=chosen)
        return cls(files=out)

    def __bool__(self) -> bool:
        return bool(self.files)

    def __contains__(self, path: object) -> bool:
        return str(path) in self.files

    @property
    def authoritative_paths(self) -> set[str]:
        return {p for p, f in self.files.items() if f.authoritative}

    @property
    def seed_paths(self) -> set[str]:
        return {p for p, f in self.files.items() if not f.authoritative}

    def resolve(
        self,
        diff: str,
        root: Path | str,
        *,
        harness_diff: str = "",
    ) -> tuple[str, Resolution]:
        """Apply the ownership invariant to *diff* and the tree at *root*.

        Returns the diff to use and a :class:`Resolution` describing what was
        done.  The three old mechanisms are one call:

        * model hunks for an ``AUTHORITATIVE`` path are dropped from the diff;
        * the harness's **own** hunks for that path (``harness_diff`` — the
          assembled page's diff, which the fix loop was seeded with) are put
          back, so the diff describes the page that exists;
        * the harness's bytes for an ``AUTHORITATIVE`` path are written back to
          the tree (the fix loop may already have applied the model's version);
        * a ``SEED`` path is left exactly as the model left it — the harness
          does not overwrite work it only seeded.

        Dropping the model's hunks without putting the harness's back was the
        first version of this refactor, and it reproduced an older live failure:
        the fix loop accepted the page, then settle reported ``diff_chars: 0,
        files: 0`` and the goal loop rejected three cycles running with the
        error "ok".  The bench's page fixture caught it on the first run.
        """
        from gigaforge.core.pipeline import (  # local: avoid a cycle
            _drop_hunks_for,
            _keep_hunks_for,
            _merge_repaired_diff,
        )

        resolution = Resolution()
        if not self.files:
            return diff, resolution

        authoritative = self.authoritative_paths
        if diff and authoritative:
            before = diff
            diff = _drop_hunks_for(diff, authoritative)
            if diff != before:
                resolution.dropped = sorted(authoritative)
            if harness_diff:
                own = _keep_hunks_for(harness_diff, authoritative)
                if own:
                    diff = _merge_repaired_diff(diff, own)
                    resolution.harness_hunks_kept = sorted(authoritative)

        root_path = Path(root)
        for path, owned in self.files.items():
            if not owned.authoritative:
                resolution.model_kept.append(path)
                continue
            target = root_path / path
            try:
                current = target.read_text(encoding="utf-8") if target.is_file() else ""
            except OSError:
                current = ""
            if current == owned.content:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(owned.content, encoding="utf-8")
            resolution.restored.append(path)
        return diff, resolution

    def to_dict(self) -> dict[str, dict[str, str]]:
        return {
            path: {"policy": owned.policy.value, "chars": str(len(owned.content))}
            for path, owned in self.files.items()
        }
