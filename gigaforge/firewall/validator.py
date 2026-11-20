"""Diff validator — apply, verify, and repair within a bounded retry budget.

The validator is the only place where a model's proposed change meets reality.
It runs a fixed pipeline on every attempt and refuses to guess:

1. **parse** the diff (:mod:`gigaforge.firewall.diff`) — fenced, prose-wrapped
   and miscounted diffs are all normalised here;
2. **apply to the worktree** — path-safety pre-flight, then an atomic write:
   a failure restores every file already touched;
3. **verify** the result, on the text that is now on disk:

   * ``syntax`` — tree-sitter re-parse via the L0 :class:`Linter`; a syntax
     break is fatal;
   * ``lint`` — ``ruff check`` with the breakage-only fatal set (``E9`` syntax,
     ``F82`` undefined names); style codes are reported, never blocking;
   * ``signatures`` — every symbol whose parameter list or return type changed
     is compared against the pre-change index;
   * ``callers`` — if a changed signature has call sites that the diff did **not**
     update, the change is incomplete and is rejected;
   * ``missing`` — names called in the changed files that resolve to nothing in
     the index (a symbol the agent forgot to add);
4. **roll back** on failure and hand the agent a *structured repair request* —
   not a raw traceback, but the exact file/line/diagnostic that failed plus what
   the next attempt must fix.

The retry budget is **exactly** ``max_retries`` (:class:`FirewallConfig`, default
2) repair attempts after the initial pass, so at most three passes run.  The loop
stops early the moment a pass is accepted, and a retry callback that returns
nothing ends it immediately rather than burning the budget on no-op repairs.
"""

from __future__ import annotations

import builtins
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from gigaforge.l0.lint import Diagnostic, Linter
from gigaforge.l0.parser import SymbolParser
from gigaforge.l0.store import L0Store

from .config import FirewallConfig
from .diff import Applier, ApplyResult, ParsedDiff, parse
from .errors import DiffParseError

#: A repair callback receives ``(attempt, feedback, previous)`` and returns a new
#: diff text (or ``None`` to stop the loop).
RetryFn = Callable[[int, "RepairRequest", str], Optional[str]]

#: Language builtins are never "missing symbols": ``ValueError("x")`` is a call
#: the symbol index can never resolve, and reporting it would be pure noise.
_BUILTINS = frozenset(dir(builtins))


# ── reporting ────────────────────────────────────────────────────────────
@dataclass
class SignatureChange:
    """A symbol whose callable interface changed."""

    name: str
    qualified_name: str
    path: str
    kind: str
    before: str
    after: str

    def describe(self) -> str:
        return f"{self.qualified_name} ({self.path}): {self.before!r} -> {self.after!r}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "qualified_name": self.qualified_name,
            "path": self.path,
            "kind": self.kind,
            "before": self.before,
            "after": self.after,
        }


@dataclass
class CallerBreak:
    """A call site that a signature change left stale."""

    callee: str
    caller_name: str
    caller_path: str
    caller_line: int
    reason: str

    def describe(self) -> str:
        return f"{self.caller_path}:{self.caller_line} {self.caller_name}() calls {self.callee} — {self.reason}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "callee": self.callee,
            "caller_name": self.caller_name,
            "caller_path": self.caller_path,
            "caller_line": self.caller_line,
            "reason": self.reason,
        }


@dataclass
class RepairRequest:
    """Everything the agent needs to fix its diff, in one place.

    ``text`` is the prompt-ready rendering; the structured fields exist so the
    caller can log, metric and branch on each failure class separately.
    """

    attempt: int
    reason: str
    diagnostics: list[Diagnostic] = field(default_factory=list)
    signature_changes: list[SignatureChange] = field(default_factory=list)
    caller_breaks: list[CallerBreak] = field(default_factory=list)
    missing_symbols: list[str] = field(default_factory=list)
    apply_errors: list[str] = field(default_factory=list)
    parse_error: str = ""
    files: list[str] = field(default_factory=list)
    hints: list[str] = field(default_factory=list)

    # ── rendering ────────────────────────────────────────────────────────
    @property
    def text(self) -> str:
        parts: list[str] = [f"Your diff was rejected (attempt {self.attempt}): {self.reason}."]
        if self.parse_error:
            parts.append(f"\n## Diff could not be parsed\n{self.parse_error}")
        if self.apply_errors:
            parts.append(
                "\n## The diff did not apply\n" + "\n".join(f"- {e}" for e in self.apply_errors)
            )
        fatal = [d for d in self.diagnostics if d.code.startswith(("E9", "F82"))]
        if fatal:
            parts.append(
                "\n## Fatal diagnostics (must be fixed)\n"
                + "\n".join(f"- {d.code} {d.line}:{d.column} {d.message}" for d in fatal)
            )
        style = [d for d in self.diagnostics if d not in fatal]
        if style:
            parts.append(
                "\n## Style diagnostics (non-blocking)\n"
                + "\n".join(f"- {d.code} {d.line}:{d.column} {d.message}" for d in style[:10])
            )
        if self.signature_changes:
            parts.append(
                "\n## Signatures you changed\n"
                + "\n".join(f"- {c.describe()}" for c in self.signature_changes)
            )
        if self.caller_breaks:
            parts.append(
                "\n## Call sites you left stale (update these in the diff)\n"
                + "\n".join(f"- {b.describe()}" for b in self.caller_breaks)
            )
        if self.missing_symbols:
            parts.append(
                "\n## Referenced but not defined\n"
                + "\n".join(f"- {s}" for s in self.missing_symbols)
            )
        if self.hints:
            parts.append("\n## Do this next\n" + "\n".join(f"- {h}" for h in self.hints))
        if self.files:
            parts.append("\n## Files in your diff\n" + ", ".join(self.files))
        parts.append(
            "\nEmit the COMPLETE corrected unified diff (all files, all hunks, "
            "with correct @@ counts). Do not include prose."
        )
        return "\n".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempt": self.attempt,
            "reason": self.reason,
            "parse_error": self.parse_error,
            "apply_errors": list(self.apply_errors),
            "diagnostics": [d.to_dict() for d in self.diagnostics],
            "signature_changes": [c.to_dict() for c in self.signature_changes],
            "caller_breaks": [b.to_dict() for b in self.caller_breaks],
            "missing_symbols": list(self.missing_symbols),
            "files": list(self.files),
            "hints": list(self.hints),
            "text": self.text,
        }


@dataclass
class Attempt:
    """The record of one validation pass."""

    attempt: int
    accepted: bool
    apply: Optional[ApplyResult] = None
    repair: Optional[RepairRequest] = None
    diagnostics: list[Diagnostic] = field(default_factory=list)
    signature_changes: list[SignatureChange] = field(default_factory=list)
    caller_breaks: list[CallerBreak] = field(default_factory=list)
    missing_symbols: list[str] = field(default_factory=list)
    rolled_back: bool = False
    changed_files: list[str] = field(default_factory=list)
    dry_run: bool = False

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "attempt": self.attempt,
            "accepted": self.accepted,
            "changed_files": list(self.changed_files),
            "diagnostics_count": len(self.diagnostics),
            "signature_changes": [c.to_dict() for c in self.signature_changes],
            "caller_breaks": [b.to_dict() for b in self.caller_breaks],
            "missing_symbols": list(self.missing_symbols),
            "rolled_back": self.rolled_back,
            "dry_run": self.dry_run,
        }
        if self.apply is not None:
            out["apply"] = self.apply.to_dict()
        if self.repair is not None:
            out["repair"] = self.repair.to_dict()
        return out


@dataclass
class ValidationOutcome:
    """The result of the whole validate-and-repair loop."""

    accepted: bool
    attempts: list[Attempt] = field(default_factory=list)
    retries_used: int = 0
    max_retries: int = 2
    applied: bool = False
    final_diff: str = ""
    changed_files: list[str] = field(default_factory=list)
    reason: str = ""

    @property
    def tries(self) -> int:
        return len(self.attempts)

    @property
    def last_repair(self) -> Optional[RepairRequest]:
        for attempt in reversed(self.attempts):
            if attempt.repair is not None:
                return attempt.repair
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "tries": self.tries,
            "retries_used": self.retries_used,
            "max_retries": self.max_retries,
            "applied": self.applied,
            "reason": self.reason,
            "changed_files": list(self.changed_files),
            "attempts": [a.to_dict() for a in self.attempts],
        }


# ── the validator ────────────────────────────────────────────────────────
class DiffValidator:
    """Applies and verifies a model's diff, with a bounded repair loop."""

    def __init__(
        self,
        store: Optional[L0Store] = None,
        *,
        config: Optional[FirewallConfig] = None,
        applier: Optional[Applier] = None,
        linter: Optional[Linter] = None,
        parser: Optional[SymbolParser] = None,
    ) -> None:
        self.store = store
        self.config = config or FirewallConfig()
        self.root = self.config.root
        self.applier = applier or Applier(self.config)
        self.parser = parser or SymbolParser(self._l0_config())
        self.linter = linter or Linter(self._l0_config(), parser=self.parser)

    # ── public API ───────────────────────────────────────────────────────
    def validate(
        self,
        diff_text: str,
        *,
        retry_fn: Optional[RetryFn] = None,
        task: str = "",
        dry_run: bool = False,
        max_retries: Optional[int] = None,
    ) -> ValidationOutcome:
        """Validate ``diff_text``, retrying up to ``max_retries`` times.

        ``retry_fn(attempt, repair, previous_diff)`` must return a corrected diff
        or ``None``.  Returning ``None`` stops the loop — there is no point
        spending the remaining budget on a repair the caller cannot produce.
        """
        budget = self.config.max_retries if max_retries is None else int(max_retries)
        outcome = ValidationOutcome(
            accepted=False,
            max_retries=budget,
            reason="not attempted",
        )
        current = diff_text

        for index in range(budget + 1):
            attempt = self._run_once(current, index + 1, dry_run=dry_run)
            outcome.attempts.append(attempt)
            if attempt.accepted:
                outcome.accepted = True
                outcome.applied = not dry_run
                outcome.changed_files = list(attempt.changed_files)
                outcome.final_diff = current
                outcome.reason = "ok"
                outcome.retries_used = self._retries(len(outcome.attempts))
                return outcome

            outcome.reason = attempt.repair.reason if attempt.repair else "rejected"
            if index >= budget:
                break
            if retry_fn is None:
                outcome.reason = f"{outcome.reason} (no retry callback)"
                break
            assert attempt.repair is not None
            candidate = retry_fn(index + 1, attempt.repair, current)
            if candidate is None:
                outcome.reason = f"{outcome.reason} (retry callback declined)"
                break
            if candidate.strip() == current.strip():
                # No new pass runs, so no retry was actually spent.
                outcome.reason = f"{outcome.reason} (retry produced an identical diff)"
                break
            current = candidate

        if not outcome.accepted and not dry_run:
            # Nothing is left applied: the last failing attempt rolled back.
            outcome.applied = False
        outcome.retries_used = self._retries(len(outcome.attempts))
        outcome.final_diff = current
        return outcome

    @staticmethod
    def _retries(tries: int) -> int:
        """Retries actually spent: every pass after the first one.

        Counting the passes rather than the callback invocations keeps the
        number honest: a callback that returns an identical diff (or declines)
        never runs a pass, so it spent no budget.
        """
        return max(0, tries - 1)

    # ── one pass ─────────────────────────────────────────────────────────
    def _run_once(self, diff_text: str, attempt_no: int, *, dry_run: bool) -> Attempt:
        cfg = self.config
        attempt = Attempt(attempt=attempt_no, accepted=False, dry_run=dry_run)

        # 1. parse
        try:
            parsed = parse(diff_text, cfg)
        except DiffParseError as exc:
            attempt.repair = RepairRequest(
                attempt=attempt_no,
                reason="diff could not be parsed",
                parse_error=str(exc.message),
                hints=[
                    "Emit a standard unified diff: '--- a/path' / '+++ b/path' then '@@ -x,y +u,v @@'.",
                    "Include the '@@' hunk header with correct counts before every hunk body.",
                ],
            )
            return attempt

        attempt.changed_files = parsed.paths

        # 2. apply
        # Snapshot the files this diff can touch *before* writing.  The applier's
        # own backups are not enough: a rejected pass has to leave the worktree
        # exactly as it found it, otherwise every later attempt is generated against
        # content that no longer exists and fails to anchor — observed live as an
        # endless "did not apply cleanly" cascade.
        snapshot: dict[str, str] = {}
        for rel in parsed.paths:
            try:
                path = (self.root / rel).resolve()
                if path.is_file():
                    snapshot[str(path)] = path.read_text(encoding="utf-8",
                                                          errors="replace")
                else:
                    snapshot[str(path)] = ""
            except OSError:
                continue

        apply_result = self.applier.apply(parsed, dry_run=dry_run)
        attempt.apply = apply_result
        if not apply_result.ok:
            # Name the worktree state: "did not apply cleanly" is only half a
            # sentence, and without the tree state the reason cannot be reproduced.
            try:
                dirty = ""
                if self.root is not None and (self.root / ".git").exists():
                    probe = subprocess.run(
                        ["git", "-C", str(self.root), "status", "--porcelain"],
                        capture_output=True, text=True, timeout=20)
                    dirty = "; ".join((probe.stdout or "").strip().splitlines()[:6])
                if dirty:
                    apply_result.errors.append(f"worktree state before apply: {dirty}")
            except Exception:  # noqa: BLE001 - diagnostics only
                pass
            attempt.rolled_back = apply_result.rolled_back
            hints = self._apply_hints(apply_result)
            # Parser repairs (a header that renamed a file, a creation hunk merged
            # into another file's block) are the most actionable feedback the model
            # can get; without them it repeats the same shape of diff.
            hints.extend(parsed.warnings[:4])
            # Keep the rejected diff next to the run: without it a failure is only
            # a sentence, and the sentence ("did not apply cleanly") cannot be
            # reproduced or fixed.
            try:
                dump = Path(str(getattr(self.config, "db_path", ".gigaforge/core.sqlite3")))
                dump = dump.parent / f"rejected-{attempt_no}.diff"
                dump.parent.mkdir(parents=True, exist_ok=True)
                dump.write_text(diff_text, encoding="utf-8")
                (dump.parent / f"rejected-{attempt_no}.errors.txt").write_text(
                    "\n".join(str(e) for e in apply_result.errors), encoding="utf-8")
                hints.append(f"rejected diff saved to {dump.name} for inspection")
            except OSError:
                pass
            attempt.repair = RepairRequest(
                attempt=attempt_no,
                reason="the diff did not apply cleanly",
                apply_errors=list(apply_result.errors),
                files=parsed.paths,
                hints=hints,
            )
            return attempt

        attempt.changed_files = apply_result.applied_paths or parsed.paths

        # 2b. repair what a formatter can repair, then judge the result
        for note in self._auto_fix(parsed.paths):
            self._log_autofix(note)

        # 3. verify
        diagnostics, sig_changes, caller_breaks, missing = self._verify(parsed)
        attempt.diagnostics = diagnostics
        attempt.signature_changes = sig_changes
        attempt.caller_breaks = caller_breaks
        attempt.missing_symbols = missing

        fatal = [d for d in diagnostics if _is_fatal(d.code, cfg.ruff_fatal_select)]
        if not fatal and not caller_breaks and not missing:
            attempt.accepted = True
            if not dry_run and self.config.reindex_after_apply and self.store is not None:
                self._reindex(attempt.changed_files)
            return attempt

        # 4. reject and roll back
        attempt.rolled_back = (
            self._rollback(apply_result, attempt.changed_files) if not dry_run else False)
        attempt.repair = RepairRequest(
            attempt=attempt_no,
            reason=self._verdict(fatal, caller_breaks, missing),
            diagnostics=diagnostics,
            signature_changes=sig_changes,
            caller_breaks=caller_breaks,
            missing_symbols=missing,
            files=list(attempt.changed_files),
            hints=self._verify_hints(fatal, caller_breaks, missing, sig_changes),
        )
        return attempt

    # ── verification ─────────────────────────────────────────────────────
    def _verify(
        self, parsed: ParsedDiff
    ) -> tuple[list[Diagnostic], list[SignatureChange], list[CallerBreak], list[str]]:
        cfg = self.config
        diagnostics: list[Diagnostic] = []
        sig_changes: list[SignatureChange] = []
        caller_breaks: list[CallerBreak] = []
        missing: list[str] = []

        for diff_file in parsed.files:
            path = diff_file.path
            abs_path = (self.root / path).resolve()
            try:
                text = abs_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue

            # syntax re-parse
            if cfg.check_syntax and not self.linter.syntax_ok(text, path):
                diagnostics.append(
                    Diagnostic(
                        code="E999",
                        message="tree-sitter re-parse found a syntax error after applying the diff",
                        line=0,
                        column=0,
                        source="ast",
                    )
                )

            # ruff
            if cfg.check_ruff:
                diagnostics.extend(self.linter.check(text, path))

            # structural sanity for HTML: it is the only gate a page can pass
            diagnostics.extend(self._markup_diagnostics(path, text))

            # signature + missing-symbol checks need the pre-change index
            if self.store is None:
                continue
            # A whole-file rewrite (a stub being filled in) has no relationship to
            # the old index: its signatures are *new by design*, so the stored call
            # sites are stale truth, not broken code.  Running those checks turned a
            # correct implementation into "19 stale call sites, 1 undefined symbol"
            # and rejected every attempt of a live long goal.
            if getattr(diff_file, "replacement", ""):
                continue
            if cfg.check_signatures or cfg.check_callers:
                changes = self._signature_changes(path, text)
                sig_changes.extend(changes)
                if cfg.check_callers:
                    caller_breaks.extend(self._caller_breaks(changes, parsed))
            if cfg.check_missing_symbols:
                missing.extend(self._missing_symbols(path, text, parsed))

        # de-duplicate
        seen: set[tuple[str, int, str]] = set()
        unique_diag: list[Diagnostic] = []
        for diag in diagnostics:
            key = (diag.code, diag.line, diag.message)
            if key in seen:
                continue
            seen.add(key)
            unique_diag.append(diag)
        return unique_diag, sig_changes, caller_breaks, sorted(set(missing))

    def _signature_changes(self, path: str, text: str) -> list[SignatureChange]:
        """Compare the file's current signatures against the indexed ones."""
        assert self.store is not None
        try:
            parsed_now = self.parser.parse_source(text, path)
        except Exception:  # noqa: BLE001 - a parse failure is reported elsewhere
            return []
        before: dict[str, Any] = {}
        for row in self.store.symbols_in_file(path):
            data = dict(row)
            if data.get("kind") in {"function", "method"}:
                before[str(data["qualified_name"])] = str(data.get("signature") or "")

        out: list[SignatureChange] = []
        for sym in parsed_now.symbols:
            if sym.kind not in {"function", "method"}:
                continue
            old = before.get(sym.qualified_name)
            if old is None:
                continue  # brand-new symbol: nothing to compare
            if _norm_sig(old) != _norm_sig(sym.signature):
                out.append(
                    SignatureChange(
                        name=sym.name,
                        qualified_name=sym.qualified_name,
                        path=path,
                        kind=sym.kind,
                        before=old,
                        after=sym.signature,
                    )
                )
        if len(out) > self.config.signature_check_limit:
            out = out[: self.config.signature_check_limit]
        return out

    def _caller_breaks(
        self, changes: Sequence[SignatureChange], parsed: ParsedDiff
    ) -> list[CallerBreak]:
        """Call sites left stale by a signature change.

        A caller is *fine* when it lives in a file the diff also touched (the
        agent was in there and may have updated the call) — but only when the
        callee name is actually still used consistently.  A caller in an
        untouched file is a genuine break.
        """
        assert self.store is not None
        touched = {f.path for f in parsed.files}
        limit = self.config.caller_check_limit
        out: list[CallerBreak] = []
        for change in changes:
            rows = self.store.callers_of_name(change.name, limit=limit)
            for row in rows:
                data = dict(row)
                caller_path = str(data.get("path") or "")
                if caller_path in touched:
                    continue
                out.append(
                    CallerBreak(
                        callee=change.name,
                        caller_name=str(data.get("caller_name") or data.get("callee") or "?"),
                        caller_path=caller_path,
                        caller_line=int(data.get("line") or 0),
                        reason="caller is in a file your diff does not touch",
                    )
                )
        return out

    def _missing_symbols(self, path: str, text: str, parsed: ParsedDiff) -> list[str]:
        """Called names in a *changed* file that resolve to nothing in the index.

        Only high-signal names are reported.  A call is skipped when the callee:

        * is defined in this file;
        * is imported by this file (``from x import y`` binds ``y``);
        * is a language builtin (``ValueError``, ``len``, ``print`` …);
        * is resolvable anywhere in the symbol index;
        * is a method call on a receiver (``obj.run()``) — the receiver's static
          type is unknowable from a symbol index, so flagging those is noise.

        That narrows the check to the case it exists for: a free function the
        agent called but forgot to define or import.
        """
        assert self.store is not None
        if not self.config.check_missing_symbols:
            return []
        try:
            parsed_now = self.parser.parse_source(text, path)
        except Exception:  # noqa: BLE001
            return []
        known = {s.name for s in parsed_now.symbols}
        for imp in parsed_now.imports:
            known.update(imp.names)
            if imp.alias:
                known.add(imp.alias)
            if imp.module:
                known.add(imp.module.split(".")[0])

        out: list[str] = []
        limit = self.config.missing_symbol_limit
        for call in parsed_now.calls:
            name = call.callee
            if not name or name in known:
                continue
            if call.is_method or call.receiver:
                continue
            if name.startswith("_"):
                continue
            if name in _BUILTINS:
                continue
            if self.store.find_symbol(name, exact=True):
                continue
            out.append(name)
            if len(out) >= limit:
                break
        return out

    # ── rollback / reindex ───────────────────────────────────────────────
    def _auto_fix(self, paths: Sequence[str]) -> list[str]:
        """Repair what a formatter can repair, instead of rejecting the change.

        A model that writes correct code with an unused import or ragged spacing
        used to be sent away with "F401/F811" — the harness then paid for another
        100k-token attempt.  `ruff --fix` and `ruff format` are deterministic and
        local, so they run first and the verdict is taken on the repaired file.
        """
        notes: list[str] = []
        if not self.config.auto_fix:
            return notes
        ruff = shutil.which("ruff")
        if ruff is None:
            return notes
        for rel in paths:
            if Path(rel).suffix.lower() not in {".py", ".pyi"}:
                continue
            target = self.root / rel
            if not target.is_file():
                continue
            for args, label in ((["check", "--fix", "--quiet", str(target)], "fix"),
                                (["format", "--quiet", str(target)], "format")):
                try:
                    proc = subprocess.run([ruff, *args], capture_output=True, text=True,
                                          timeout=90)
                except Exception:  # noqa: BLE001 - formatting is best effort
                    continue
                if proc.returncode == 0 and label == "fix":
                    notes.append(f"{rel}: auto-fixed by ruff")
        return notes

    def _restore(self, snapshot: Mapping[str, str]) -> bool:
        """Put every snapshotted file back exactly as it was.

        Files that did not exist before (empty snapshot entry) are removed, so a
        rejected pass cannot leave created files behind either.
        """
        ok = True
        for path_str, original in snapshot.items():
            path = Path(path_str)
            try:
                if original == "":
                    if path.is_file():
                        path.unlink()
                    continue
                path.parent.mkdir(parents=True, exist_ok=True)
                if not path.is_file() or path.read_text(encoding="utf-8",
                                                        errors="replace") != original:
                    path.write_text(original, encoding="utf-8")
            except OSError:
                ok = False
        return ok

    def _rollback(self, apply_result: ApplyResult, changed: Sequence[str]) -> bool:
        """Restore every file the apply touched, from the applier's backups."""
        ok = True
        restored: list[str] = []
        for abs_path_str, original in apply_result.backups.items():
            target = Path(abs_path_str)
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(original, encoding="utf-8")
                restored.append(abs_path_str)
            except OSError:
                ok = False
        # Remove files the diff created (they have no backup entry).
        for path in changed:
            abs_path = (self.root / path).resolve()
            if str(abs_path) in apply_result.backups:
                continue
            if any(outcome.path == path and outcome.status == "created" for outcome in apply_result.files):
                try:
                    if abs_path.exists():
                        abs_path.unlink()
                except OSError:
                    ok = False
        if self.store is not None and restored:
            self._reindex(list(changed))
        return ok

    def _reindex(self, paths: Sequence[str]) -> None:
        """Bring the L0 index back in line with the working tree."""
        if self.store is None:
            return
        from gigaforge.l0.indexer import Indexer

        try:
            Indexer(self.store, config=self._l0_config(), parser=self.parser).index_paths(
                [p for p in paths if p], force=True
            )
        except Exception:  # noqa: BLE001 - indexing must never fail the validation
            pass

    # ── messaging ────────────────────────────────────────────────────────
    @staticmethod
    def _verdict(
        fatal: Sequence[Diagnostic],
        caller_breaks: Sequence[CallerBreak],
        missing: Sequence[str],
    ) -> str:
        bits: list[str] = []
        if fatal:
            bits.append(f"{len(fatal)} fatal diagnostic(s) ({fatal[0].code})")
        if caller_breaks:
            bits.append(f"{len(caller_breaks)} stale call site(s)")
        if missing:
            bits.append(f"{len(missing)} undefined symbol(s)")
        return "; ".join(bits) or "verification failed"

    # ── markup sanity ────────────────────────────────────────────────────
    @staticmethod
    def _markup_diagnostics(path: str, text: str) -> list[Diagnostic]:
        """Structural checks for HTML — the only gate a page can pass.

        ruff cannot judge HTML and tree-sitter has no spec for it, so without this
        a page missing ``</html>``, or 40 characters of placeholder text, would be
        accepted as "no diagnostics".  The checks are structural, not stylistic:
        a page must be a page.
        """
        if Path(path).suffix.lower() not in {".html", ".htm"}:
            return []
        body = text.lower()
        problems: list[tuple[str, str]] = []
        if "<html" not in body:
            problems.append(("WEB001", "no <html> element"))
        if "</html>" not in body:
            problems.append(("WEB002", "no closing </html>"))
        if "<body" not in body:
            problems.append(("WEB003", "no <body> element"))
        if "<title" not in body:
            problems.append(("WEB004", "no <title> in <head>"))
        if len(text.strip()) < 400:
            problems.append(("WEB005", f"page is only {len(text.strip())} characters"))
        # A page must not depend on third-party assets: a placeholder image host is
        # a broken page the moment it is opened offline, and "self-contained" is a
        # promise the harness can actually check.
        import re as _re

        external = _re.findall(r"""(?:src|url)\s*[=(]\s*["']?(https?://[^"')\s]+)""",
                               text, _re.IGNORECASE)
        if external:
            problems.append(("WEB006", f"external asset(s): {', '.join(external[:3])}"))
        return [
            Diagnostic(code=code, message=message, line=0, column=0, source="markup")
            for code, message in problems
        ]

    @staticmethod
    def _apply_hints(apply_result: ApplyResult) -> list[str]:
        hints: list[str] = []
        text = " ".join(apply_result.errors)
        if "does not exist" in text:
            hints.append(
                "The target file is missing: if you are adding it, mark the diff with "
                "'new file mode' / '--- /dev/null' and include the full contents."
            )
        if "match" in text.lower() or "anchor" in text.lower():
            hints.append(
                "Include 3 lines of unchanged context around each edit so the hunk can be located."
            )
        if "deny-glob" in text or "allow-list" in text:
            hints.append("Choose a different file: that path is not writable by the firewall.")
        hints.append("Re-read the file and regenerate the hunk against its current content.")
        return hints

    @staticmethod
    def _verify_hints(
        fatal: Sequence[Diagnostic],
        caller_breaks: Sequence[CallerBreak],
        missing: Sequence[str],
        sig_changes: Sequence[SignatureChange],
    ) -> list[str]:
        hints: list[str] = []
        if fatal:
            hints.append(
                "Fix the syntax/undefined-name errors first — nothing else can be verified until the file parses."
            )
        if caller_breaks:
            names = sorted({b.callee for b in caller_breaks})
            hints.append(
                "Add hunks updating these call sites (or keep the original signature): "
                + ", ".join(names)
            )
        if missing and not fatal:
            hints.append(
                "Define or import these before use: " + ", ".join(missing[:8])
            )
        if sig_changes and not caller_breaks:
            hints.append(
                "Signature changes are allowed here — every caller is inside your diff."
            )
        hints.append("Re-emit the complete diff for all files, not just the changed hunk.")
        return hints

    # ── helpers ──────────────────────────────────────────────────────────
    def _l0_config(self) -> Any:
        """The L0 config that matches this firewall's repo root and budgets."""
        if self.store is not None:
            base = getattr(self.store, "config", None)
            if base is not None:
                return base
        from gigaforge.l0.config import L0Config

        return L0Config(repo_root=self.config.repo_root)


def _is_fatal(code: str, prefixes: Sequence[str]) -> bool:
    return any(code.startswith(prefix) for prefix in prefixes)


def _norm_sig(signature: str) -> str:
    """Whitespace-insensitive signature comparison key."""
    return " ".join(signature.split())


__all__ = [
    "DiffValidator",
    "ValidationOutcome",
    "Attempt",
    "RepairRequest",
    "SignatureChange",
    "CallerBreak",
]
