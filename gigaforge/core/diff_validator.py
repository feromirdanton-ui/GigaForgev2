"""Diff Validator — parses, validates, and applies unified diffs from the Coder.

Pipeline (exact order per spec §8):
1. Strip ``\\`\\`\\```-fences (models add them).
2. Parse as unified diff: extract file paths (``--- a/path``, ``+++ b/path``),
   hunks (``@@ -start,count +start,count @@``), context + removed + added lines.
3. Verify target paths exist under ``worktree`` (paths relative to repo root).
4. Context-line matching, in escalating fidelity:
   - EXACT match of context+removed lines at the hunk's declared start line.
   - If exact fails -> FUZZY (whitespace-normalised) match, search nearby lines.
   - If fuzzy fails -> SYMBOL-level surgical apply: identify the symbol the
     hunk touches from ``def name(`` in context/added lines, then use
     :func:`gigaforge.l0.ast_edit.edit_function_body` (or ``add_symbol`` for
     new-function diffs) to apply the new body. This works even when line
     numbers are stale.
5. Apply to a COPY in ``worktree``. Idempotent: applying the same diff twice
   is detected as a no-op (post-apply content == pre-apply content) and skipped.
6. After apply: run :func:`gigaforge.l0.lint.lint` (ruff). Reject if errors.
7. AST re-parse the file (tree-sitter) to confirm valid syntax. Reject if invalid.
8. Signature/caller compatibility: re-index the file (L0); for the edited
   symbol, confirm it still exists with its name unchanged (best-effort).

CONTRACT — NEVER raises on a bad diff. Always returns a structured
:class:`DiffApplyResult` with ``ok=False`` and a precise ``reason`` string.
The pipeline feeds ``reason`` back to the Coder for retry (max 2 retries;
that's the pipeline's concern — this module just produces the reason).

REASON STRING FORMAT (consumed by the pipeline for retries):
    ``"<stage>:<detail>"``
where ``<stage>`` is one of: ``parse``, ``apply``, ``ast_surgical``,
``lint``, ``syntax``, ``caller``, and ``<detail>`` is a short actionable
description. Multi-error cases comma-separate. Examples:
    - ``"parse:no diff headers (--- /+++ /@@) found in model output"``
    - ``"apply:file not found in worktree: foo.py"``
    - ``"ast_surgical:symbol not found: bar"``
    - ``"lint:F401:5 'os' imported but unused, E711:8 ..."``  (capped to first 5)
    - ``"syntax:tree-sitter parse error at line 12"``
    - ``"caller:symbol 'bar' missing after edit"``
On success: ``reason="ok"`` or ``"no changes (diff already applied)"`` for
idempotent no-ops (method="none").
"""
from __future__ import annotations

import re
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import tree_sitter_python as tspython
from pydantic import BaseModel, Field
from tree_sitter import Language, Parser

from gigaforge.l0.ast_edit import ASTEditError, add_symbol, edit_function_body
from gigaforge.l0.index import L0Index
from gigaforge.l0.lint import lint

__all__ = ["DiffValidator", "DiffApplyResult", "Hunk", "HunkLine"]

# tree-sitter setup (form verified by Task 2).
PY_LANG = Language(tspython.language())
_PARSER = Parser(PY_LANG)

Method = Literal["exact", "fuzzy", "ast_surgical", "none"]

# Rank: higher = more desperate. Overall method reported is the max across hunks.
_METHOD_RANK: dict[str, int] = {"exact": 0, "fuzzy": 1, "ast_surgical": 2}

# Max lint errors surfaced in the reason string (kept short for retry prompt).
_MAX_LINT_IN_REASON = 5

# Search radius (lines either side of declared start) for the fuzzy matcher.
_FUZZY_RADIUS = 10


# --- public models ----------------------------------------------------------


class DiffApplyResult(BaseModel):
    """Structured result of a diff apply attempt. NEVER raised — always returned."""

    ok: bool
    reason: str
    applied_files: list[str] = Field(default_factory=list)
    lint_clean: bool = False
    method: Method = "none"
    raw: str = ""


# --- internal hunk representation -------------------------------------------


@dataclass
class HunkLine:
    kind: Literal["context", "removed", "added"]
    content: str  # without the leading prefix (" ", "-", "+")


@dataclass
class Hunk:
    file_path: str  # path from the b/ side, relative to repo root
    old_start: int  # 1-indexed start line in the old file
    new_start: int  # 1-indexed start line in the new file
    lines: list[HunkLine] = field(default_factory=list)

    @property
    def context_lines(self) -> list[str]:
        return [ln.content for ln in self.lines if ln.kind == "context"]

    @property
    def removed_lines(self) -> list[str]:
        return [ln.content for ln in self.lines if ln.kind == "removed"]

    @property
    def added_lines(self) -> list[str]:
        return [ln.content for ln in self.lines if ln.kind == "added"]

    @property
    def pre_image(self) -> list[str]:
        """Lines (in order) that the hunk expects to find in the old file."""
        return [ln.content for ln in self.lines if ln.kind in ("context", "removed")]

    @property
    def post_image(self) -> list[str]:
        """Lines (in order) that the hunk produces in the new file."""
        return [ln.content for ln in self.lines if ln.kind in ("context", "added")]


# --- the validator ----------------------------------------------------------


class DiffValidator:
    """Validates and applies unified diffs from the Coder model.

    Pass an :class:`~gigaforge.l0.index.L0Index` to enable the caller-compat
    check (step 8). Without an index, the caller check is skipped (best-effort).
    """

    def __init__(self, index: L0Index | None = None) -> None:
        self.index = index

    # -- public --

    @staticmethod
    def is_diff(text: str) -> bool:
        """Quick check: contains ``--- ``, ``+++ `` and ``@@`` markers."""
        return "--- " in text and "+++ " in text and "@@" in text

    def validate_and_apply(
        self,
        diff_text: str,
        repo_root: Path,
        *,
        worktree: Path,
    ) -> DiffApplyResult:
        repo_root = Path(repo_root)
        worktree = Path(worktree)

        # 1. Strip ```-fences (models add them).
        cleaned = _strip_fences(diff_text)

        # 2. Parse as unified diff.
        if not DiffValidator.is_diff(cleaned):
            return DiffApplyResult(
                ok=False,
                reason="parse:no diff headers (--- /+++ /@@) found in model output",
                raw=diff_text,
            )
        try:
            hunks = _parse_unified_diff(cleaned)
        except Exception as exc:  # never raise on bad diff
            return DiffApplyResult(
                ok=False,
                reason=f"parse:failed to parse unified diff: {exc.__class__.__name__}: {exc}",
                raw=diff_text,
            )
        if not hunks:
            return DiffApplyResult(
                ok=False,
                reason="parse:no hunks found in diff",
                raw=diff_text,
            )

        # 3. Verify target paths exist under worktree.
        for hunk in hunks:
            target = worktree / hunk.file_path
            if not target.exists():
                return DiffApplyResult(
                    ok=False,
                    reason=f"apply:file not found in worktree: {hunk.file_path}",
                    raw=diff_text,
                )

        # 4-5. Apply each hunk (exact -> fuzzy -> ast_surgical).
        all_methods: list[Method] = []
        applied_files: list[str] = []
        original_contents: dict[str, str] = {}

        for hunk in hunks:
            target_path = worktree / hunk.file_path
            target_str = str(target_path)
            if target_str not in original_contents:
                original_contents[target_str] = target_path.read_text(encoding="utf-8")

            method, reason = self._apply_hunk(target_path, hunk)
            if method == "none" and reason.startswith("reject:"):
                # _apply_hunk signals rejection by returning ("none", "reject:<...>")
                return DiffApplyResult(
                    ok=False,
                    reason=reason[len("reject:") :],
                    applied_files=applied_files,
                    raw=diff_text,
                )
            all_methods.append(method)
            if hunk.file_path not in applied_files:
                applied_files.append(hunk.file_path)

        # CRIT-04: "none" (no-op) must NOT win over real apply methods. If a
        # diff has one exact hunk and one no-op hunk, the reported method is the
        # real one ("exact"); only report "none" when ALL hunks were no-ops.
        real_methods = [m for m in all_methods if m != "none"]
        if real_methods:
            overall_method: Method = max(real_methods, key=lambda m: _METHOD_RANK.get(m, 0))
        else:
            overall_method = "none"

        # Idempotency: if NO file content changed, the diff is a no-op.
        any_changed = False
        for target_str, original in original_contents.items():
            current = Path(target_str).read_text(encoding="utf-8")
            if current != original:
                any_changed = True
                break
        if not any_changed:
            return DiffApplyResult(
                ok=True,
                reason="no changes (diff already applied)",
                applied_files=applied_files,
                lint_clean=True,
                method="none",
                raw=diff_text,
            )

        # 6. Lint each applied file (ruff).
        for fp in applied_files:
            full = worktree / fp
            lint_result = lint(full)
            if not lint_result.ok:
                errs = ", ".join(
                    f"{e['code']}:{e['line']} {e['message']}"
                    for e in lint_result.errors[:_MAX_LINT_IN_REASON]
                )
                return DiffApplyResult(
                    ok=False,
                    reason=f"lint:{errs}",
                    applied_files=applied_files,
                    lint_clean=False,
                    method=overall_method,
                    raw=diff_text,
                )

        # 7. AST re-parse each applied file (tree-sitter) — confirm valid syntax.
        for fp in applied_files:
            full = worktree / fp
            src = full.read_text(encoding="utf-8")
            root = _PARSER.parse(bytes(src, "utf8")).root_node
            if root.has_error:
                err_line = _first_error_line(root)
                return DiffApplyResult(
                    ok=False,
                    reason=f"syntax:tree-sitter parse error at line {err_line}",
                    applied_files=applied_files,
                    lint_clean=True,
                    method=overall_method,
                    raw=diff_text,
                )

        # 8. Caller/signature compatibility (best-effort, requires L0).
        if self.index is not None:
            caller_reason = self._caller_compat_check(worktree, hunks, applied_files)
            if caller_reason is not None:
                return DiffApplyResult(
                    ok=False,
                    reason=caller_reason,
                    applied_files=applied_files,
                    lint_clean=True,
                    method=overall_method,
                    raw=diff_text,
                )

        return DiffApplyResult(
            ok=True,
            reason="ok",
            applied_files=applied_files,
            lint_clean=True,
            method=overall_method,
            raw=diff_text,
        )

    # ------------------------------------------------------------------ apply

    def _apply_hunk(self, target_path: Path, hunk: Hunk) -> tuple[Method, str]:
        """Apply a single hunk. Returns (method, reason).

        method='none' with reason='no changes' indicates a no-op (caller will
        decide final reporting).
        method='none' with reason='reject:<...>' indicates a hard rejection;
        the caller strips the ``reject:`` prefix and returns ok=False.
        """
        source = target_path.read_text(encoding="utf-8")
        lines = source.split("\n")

        # EXACT match at declared old_start.
        new_source, ok = _try_exact(lines, hunk)
        if ok:
            if new_source == source:
                return "none", "no changes"
            target_path.write_text(new_source, encoding="utf-8")
            return "exact", "ok"

        # FUZZY match (whitespace-normalised, search nearby).
        new_source, ok = _try_fuzzy(lines, hunk)
        if ok:
            if new_source == source:
                return "none", "no changes"
            target_path.write_text(new_source, encoding="utf-8")
            return "fuzzy", "ok"

        # AST surgical fallback.
        try:
            method, reason_or_src = _ast_surgical_apply(target_path, hunk)
        except ASTEditError as exc:
            return "none", f"reject:ast_surgical:{exc}"
        if method == "none":
            # _ast_surgical_apply signals either a no-op ("no changes") or a
            # hard rejection ("reject:ast_surgical:...") via the reason string.
            # MED-16 introduced the rename-rejection path; preserve it verbatim
            # so the caller can surface "rename not supported" to the Coder.
            if isinstance(reason_or_src, str) and reason_or_src.startswith("reject:"):
                return "none", reason_or_src
            return "none", "no changes"
        # _ast_surgical_apply already wrote the file via ast_edit.
        return "ast_surgical", "ok"

    # ------------------------------------------------------------------ callers

    def _caller_compat_check(
        self,
        worktree: Path,
        hunks: list[Hunk],
        applied_files: list[str],
    ) -> str | None:
        """Re-index edited files; confirm touched symbols still exist.

        Returns a reason string if a caller would break, else ``None``.
        Best-effort: if re-indexing fails for any file, we skip that file
        rather than rejecting (a faulty L0 doesn't block a syntactically
        valid diff).

        HIGH-04: this MUST NOT pollute the shared/persistent L0 index with
        worktree paths (e.g. ``.gigaforge/worktrees/task-XXX/foo.py``) — those
        would never be re-indexed, shadow the real symbol, and corrupt
        ``slice_hash`` on the next run. We build a THROWAWAY in-memory index in
        a temp dir and query that.
        """
        import tempfile

        from gigaforge.l0.index import L0Index  # local import avoids cycle

        with tempfile.TemporaryDirectory() as tmp:
            tmp_index = L0Index(db_path=Path(tmp) / "compat.db")
            try:
                for fp in applied_files:
                    full = worktree / fp
                    try:
                        tmp_index.index_file(full)
                    except Exception:
                        continue  # best-effort
                for hunk in hunks:
                    symbol_name = _extract_symbol_name(hunk)
                    if symbol_name is None:
                        continue
                    sym = tmp_index.get_symbol(symbol_name)
                    if sym is None:
                        # The hunk touched a symbol that no longer exists after
                        # the edit. May be intentional (rename/delete) or
                        # accidental — signal so the Coder can confirm on retry.
                        return (
                            f"caller:symbol {symbol_name!r} missing after edit "
                            "(if intentional rename, update callers explicitly)"
                        )
                return None
            finally:
                try:
                    tmp_index.close()
                except Exception:  # noqa: BLE001 — best-effort cleanup
                    pass


# --- diff parsing -----------------------------------------------------------


_FENCE_OPEN_RE = re.compile(r"^```(\w*)\s*$")
_FENCE_CLOSE_RE = re.compile(r"^```\s*$")

_OLD_FILE_RE = re.compile(r"^---\s+(?:a/)?(?P<p>.+?)\s*(?:\t.*)?$")
_NEW_FILE_RE = re.compile(r"^\+\+\+\s+(?:b/)?(?P<p>.+?)\s*(?:\t.*)?$")
_HUNK_RE = re.compile(
    r"^@@\s+-(?P<o_start>\d+)(?:,(?P<o_count>\d+))?\s+\+(?P<n_start>\d+)(?:,(?P<n_count>\d+))?\s+@@"
)


def _strip_fences(text: str) -> str:
    """Strip leading/trailing ```-fences (and optional language tag).

    A model that wraps its diff as ``\\`\\`\\`diff\\n<diff>\\n\\`\\`\\``` is
    handled cleanly. If no fences are present, the text is returned unchanged
    (after strip).
    """
    text = text.strip()
    if not text.startswith("```"):
        return text
    lines = text.split("\n")
    # Drop the opening fence line.
    if lines and _FENCE_OPEN_RE.match(lines[0]):
        lines = lines[1:]
    # Drop the closing fence line (last occurrence).
    for i in range(len(lines) - 1, -1, -1):
        if _FENCE_CLOSE_RE.match(lines[i]):
            lines = lines[:i]
            break
    return "\n".join(lines).strip()


def _parse_unified_diff(text: str) -> list[Hunk]:
    """Parse a unified diff into a list of :class:`Hunk` objects.

    A diff may touch multiple files; each ``--- a/`` / ``+++ b/`` pair starts
    a new file context, and each ``@@`` line starts a new hunk within that file.
    Lines inside a hunk are classified by their first character: `` `` context,
    ``-`` removed, ``+`` added. Blank lines are skipped (they're not real diff
    content — they appear from fence stripping or trailing newlines).

    LOW-12: ``--- /dev/null`` (file creation) and ``+++ /dev/null`` (file
    deletion) are NOT silently treated as ``b/dev/null`` / ``a/dev/null``
    paths. They're surfaced as an explicit ``ValueError`` with reason
    ``apply:create/delete file not supported`` so the validator returns a
    structured ``DiffApplyResult(ok=False, reason=...)`` rather than trying
    to write to a literal ``dev/null`` file inside the worktree.
    """
    hunks: list[Hunk] = []
    current_path: str | None = None
    current_hunk: Hunk | None = None

    for line in text.split("\n"):
        if line.startswith("--- "):
            # LOW-12: /dev/null on the --- side means "create a new file"
            # (there was no old file). We don't support creation; surface it
            # explicitly rather than treating "/dev/null" as a real path.
            m = _OLD_FILE_RE.match(line)
            if m and m.group("p").strip() == "/dev/null":
                raise ValueError("apply:create/delete file not supported (/dev/null in --- header)")
            # Prefer the +++ line's b/ path; only use --- if no +++ yet.
            if current_path is None:
                if m:
                    current_path = m.group("p").strip()
            continue
        if line.startswith("+++ "):
            m = _NEW_FILE_RE.match(line)
            # LOW-12: /dev/null on the +++ side means "delete this file".
            # Same treatment as creation — explicit rejection, no silent apply.
            if m and m.group("p").strip() == "/dev/null":
                raise ValueError("apply:create/delete file not supported (/dev/null in +++ header)")
            if m:
                current_path = m.group("p").strip()
            continue
        m = _HUNK_RE.match(line)
        if m:
            if current_hunk is not None:
                hunks.append(current_hunk)
            if current_path is None:
                raise ValueError("hunk header without preceding +++ b/path")
            current_hunk = Hunk(
                file_path=current_path,
                old_start=int(m.group("o_start")),
                new_start=int(m.group("n_start")),
            )
            continue
        if current_hunk is None:
            continue
        if line.startswith("+++"):
            # Already handled above; defensive.
            continue
        if line.startswith("---"):
            continue
        if line.startswith("+"):
            current_hunk.lines.append(HunkLine(kind="added", content=line[1:]))
        elif line.startswith("-"):
            current_hunk.lines.append(HunkLine(kind="removed", content=line[1:]))
        elif line.startswith(" "):
            current_hunk.lines.append(HunkLine(kind="context", content=line[1:]))
        elif line == "":
            # Skip blank lines in the diff body (not real content).
            continue
        elif line.startswith("\\"):
            # "\ No newline at end of file" — metadata; skip.
            continue
        else:
            # Unknown line — skip rather than crash.
            continue

    if current_hunk is not None:
        hunks.append(current_hunk)

    return hunks


# --- apply strategies -------------------------------------------------------


def _try_exact(lines: list[str], hunk: Hunk) -> tuple[str, bool]:
    """Try to apply hunk at declared ``old_start`` with exact pre-image match.

    Returns (new_source, ok). On failure, returns (original_source_as_string, False).
    """
    pre_image = hunk.pre_image
    if not pre_image:
        return "\n".join(lines), False

    start_idx = hunk.old_start - 1  # 0-indexed
    if start_idx < 0:
        start_idx = 0
    if start_idx + len(pre_image) > len(lines):
        return "\n".join(lines), False

    for i, expected in enumerate(pre_image):
        if lines[start_idx + i] != expected:
            return "\n".join(lines), False

    new_lines = lines[:start_idx] + hunk.post_image + lines[start_idx + len(pre_image) :]
    return "\n".join(new_lines), True


def _try_fuzzy(lines: list[str], hunk: Hunk) -> tuple[str, bool]:
    """Fuzzy match: whitespace-normalised, search ±_FUZZY_RADIUS lines."""
    pre_image = hunk.pre_image
    if not pre_image:
        return "\n".join(lines), False

    def norm(s: str) -> str:
        return re.sub(r"\s+", " ", s).strip()

    pre_norm = [norm(ln) for ln in pre_image]
    declared = hunk.old_start - 1
    lo = max(0, declared - _FUZZY_RADIUS)
    hi = min(len(lines), declared + _FUZZY_RADIUS + 1)

    for start in range(lo, hi):
        if start + len(pre_norm) > len(lines):
            continue
        ok = True
        for i, expected in enumerate(pre_norm):
            if norm(lines[start + i]) != expected:
                ok = False
                break
        if ok:
            new_lines = (
                lines[:start] + hunk.post_image + lines[start + len(pre_image) :]
            )
            return "\n".join(new_lines), True
    return "\n".join(lines), False


def _ast_surgical_apply(target_path: Path, hunk: Hunk) -> tuple[Method, str]:
    """Symbol-level surgical apply via L0 ``ast_edit``.

    Identifies the symbol the hunk touches from ``def name(`` in context or
    added lines, then either:
    - calls :func:`edit_function_body` for an existing function body edit, or
    - calls :func:`add_symbol` for a new-function diff.

    Returns (method, new_source). Raises :class:`ASTEditError` if no symbol
    can be identified or the AST edit fails.
    """
    # The "new image" of the hunk region = context + added lines, in order.
    new_image_lines = [ln.content for ln in hunk.lines if ln.kind in ("context", "added")]
    added_only_lines = [ln.content for ln in hunk.lines if ln.kind == "added"]

    # Try to find a def name in the new image.
    symbol_name: str | None = None
    def_line_idx: int | None = None
    for i, line in enumerate(new_image_lines):
        m = re.match(r"\s*(?:async\s+)?def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", line)
        if m:
            symbol_name = m.group(1)
            def_line_idx = i
            break

    if symbol_name is not None and def_line_idx is not None:
        # Body = everything after the def line.
        body_lines = new_image_lines[def_line_idx + 1 :]
        new_body = textwrap.dedent("\n".join(body_lines))
        # Try editing the existing function's body; if the symbol doesn't
        # exist (this is an "add function" diff), fall back to add_symbol.
        try:
            result = edit_function_body(target_path, symbol_name, new_body)
            return "ast_surgical", result.new_text
        except ASTEditError as exc:
            if "symbol not found" not in str(exc):
                raise  # invalid Python, missing body, etc. — propagate.
            # MED-16: rename diff (-def old() / +def new()) would silently
            # call add_symbol (adds `new`, doesn't remove `old`) — that's
            # silent corruption. Detect the rename explicitly and reject it
            # so the Coder is told to use rename_symbol instead.
            old_name = _extract_removed_def_name(hunk.removed_lines)
            if old_name is not None and old_name != symbol_name:
                return (
                    "none",
                    "reject:ast_surgical:rename not supported "
                    f"({old_name} -> {symbol_name}); use rename_symbol explicitly",
                )
            # Symbol doesn't exist and there's no `def old()` being removed —
            # this is a genuine "add new function" diff. Add it as a new symbol.
            new_src = "\n".join(new_image_lines[def_line_idx:])
            result = add_symbol(target_path, None, new_src)
            return "ast_surgical", result.new_text

    # No def in context — check if this is an "add function" diff.
    added_text = "\n".join(added_only_lines)
    m = re.search(
        r"^\s*(?:async\s+)?def\s+([A-Za-z_][A-Za-z0-9_]*)",
        added_text,
        re.MULTILINE,
    )
    if m:
        result = add_symbol(target_path, None, added_text)
        return "ast_surgical", result.new_text

    # Could not identify a symbol — give up.
    raise ASTEditError(
        f"no def symbol found in hunk for {hunk.file_path}; "
        "neither exact/fuzzy context matched nor AST surgical fallback applied"
    )


# --- helpers ----------------------------------------------------------------


def _extract_symbol_name(hunk: Hunk) -> str | None:
    """Best-effort: find a ``def name(`` in the hunk's context/added lines."""
    new_image_lines = [ln.content for ln in hunk.lines if ln.kind in ("context", "added")]
    for line in new_image_lines:
        m = re.match(r"\s*(?:async\s+)?def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", line)
        if m:
            return m.group(1)
    return None


def _extract_removed_def_name(removed_lines: list[str]) -> str | None:
    """Best-effort: find a ``def name(`` in the hunk's removed lines.

    Used by MED-16 to detect rename diffs (``-def old()`` / ``+def new()``)
    so they can be rejected explicitly rather than silently corrupted via
    a fallthrough to ``add_symbol``.
    """
    for line in removed_lines:
        m = re.match(r"\s*(?:async\s+)?def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", line)
        if m:
            return m.group(1)
    return None


def _first_error_line(root) -> int:
    """Find the first line (1-indexed) of a parse-error node in the tree."""
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type == "ERROR" or node.is_missing:
            return node.start_point[0] + 1
        for child in node.children:
            stack.append(child)
    return 1
