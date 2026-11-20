"""Lint / format / re-parse gate for L0 edits.

Every structural edit made through :mod:`gigaforge.l0.ast_tools` passes
through :class:`Linter.gate` before it is written to disk:

1. **re-parse** the candidate with tree-sitter — a syntax break is fatal;
2. **format** with ``ruff format`` (optional, config-gated);
3. **lint** with ``ruff check`` (optional, config-gated);
4. **roll back** to the original text when a gate fails and the config says
   the failure is fatal.

The linter is deliberately tolerant: if ``ruff`` is not installed the gate
degrades to syntax-check-only instead of blocking every edit.  That keeps the
L0 layer usable in a bare environment while still enforcing correctness.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Sequence

from .config import L0Config
from .errors import LintToolError, UnsupportedLanguageError
from .languages import spec_for_path
from .parser import SymbolParser


@dataclass
class Diagnostic:
    code: str
    message: str
    line: int = 0
    column: int = 0
    end_line: int = 0
    end_column: int = 0
    fixable: bool = False
    source: str = "ruff"

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "line": self.line,
            "column": self.column,
            "end_line": self.end_line,
            "end_column": self.end_column,
            "fixable": self.fixable,
            "source": self.source,
        }


@dataclass
class GateResult:
    """Outcome of running the lint/format/re-parse gate on a candidate."""

    source: str
    ok: bool
    rolled_back: bool = False
    reason: str = ""
    formatted: bool = False
    diagnostics: list[Diagnostic] = field(default_factory=list)
    syntax_ok: bool = True
    lint_ran: bool = False
    format_ran: bool = False

    @property
    def errors(self) -> list[Diagnostic]:
        return [d for d in self.diagnostics if d.code.startswith(("E", "F"))]

    @property
    def fatal(self) -> list[Diagnostic]:
        """Diagnostics that represent breakage rather than style."""
        return [d for d in self.diagnostics if _is_fatal(d.code)]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "rolled_back": self.rolled_back,
            "reason": self.reason,
            "formatted": self.formatted,
            "syntax_ok": self.syntax_ok,
            "lint_ran": self.lint_ran,
            "format_ran": self.format_ran,
            "diagnostics": [d.to_dict() for d in self.diagnostics],
        }


def _is_fatal(code: str, prefixes: Sequence[str] = ("E9", "F82")) -> bool:
    """True when a ruff code denotes breakage (syntax / undefined name).

    Style codes (F401 unused import, F841 unused variable, E501 line length)
    are reported but never block an edit — a legitimate refactor such as
    removing a symbol routinely leaves an unused import behind.
    """
    return any(code.startswith(prefix) for prefix in prefixes)


class Linter:
    """ruff-backed lint/format gate with tree-sitter syntax verification."""

    def __init__(
        self,
        config: Optional[L0Config] = None,
        *,
        parser: Optional[SymbolParser] = None,
    ) -> None:
        self.config = config or L0Config()
        self.parser = parser or SymbolParser(self.config)
        self._ruff_path: Optional[str] = None
        self._ruff_checked = False

    # ── availability ─────────────────────────────────────────────────────
    def ruff_path(self) -> Optional[str]:
        if not self._ruff_checked:
            self._ruff_path = shutil.which(self.config.ruff_bin)
            self._ruff_checked = True
        return self._ruff_path

    def available(self) -> bool:
        return self.config.ruff_enabled and self.ruff_path() is not None

    @staticmethod
    def _is_python(path: str) -> bool:
        """True when ruff is the right tool for this file at all."""
        suffix = ("." + str(path).rsplit(".", 1)[-1].lower()) if "." in str(path) else ""
        return suffix in {".py", ".pyi"}

    # ── syntax ───────────────────────────────────────────────────────────
    def syntax_ok(self, source: str, path: str) -> bool:
        """Re-parse ``source`` without confusing missing grammars with bad code.

        Non-Python grammars are optional.  When the JavaScript grammar is not
        installed, use Node's parser for the common JS extensions when available;
        otherwise skip that syntax check instead of rejecting valid code because
        this installation lacks an optional parser package.
        """
        spec = spec_for_path(path)
        if spec is None:
            return True  # unknown language: nothing to verify
        try:
            parsed = self.parser.parse_source(source, path)
        except UnsupportedLanguageError:
            suffix = Path(path).suffix.lower()
            if spec.name == "javascript" and suffix in {".js", ".mjs", ".cjs"}:
                return self._node_syntax_ok(source, suffix)
            return True
        except Exception:
            return False
        return not parsed.errors

    @staticmethod
    def _node_syntax_ok(source: str, suffix: str) -> bool:
        """Check JavaScript with Node when its optional tree-sitter grammar is absent."""
        node = shutil.which("node")
        if not node:
            return True
        input_type = "module" if suffix == ".mjs" else "commonjs"
        try:
            result = subprocess.run(
                [node, "--check", f"--input-type={input_type}"],
                input=source,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return True
        return result.returncode == 0

    # ── ruff primitives ──────────────────────────────────────────────────
    def _run_ruff(self, args: Sequence[str], *, stdin: str) -> subprocess.CompletedProcess:
        exe = self.ruff_path()
        if exe is None:
            raise LintToolError(f"ruff not found: {self.config.ruff_bin}")
        try:
            return subprocess.run(
                [exe, *args],
                input=stdin,
                capture_output=True,
                text=True,
                timeout=self.config.ruff_timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise LintToolError(f"ruff timed out after {self.config.ruff_timeout_s}s") from exc

    def check(self, source: str, path: str) -> list[Diagnostic]:
        """Run ``ruff check`` over ``source`` and return diagnostics.

        Only Python is linted.  ruff happily "lints" HTML/CSS/JSON and reports
        ``invalid-syntax`` for them; the validator treats that as fatal, so every
        edit to a web file was rejected with "syntax error after applying the
        diff" — observed on a real ``index.html`` task.  A file ruff cannot parse
        is not a file with a syntax error.
        """
        if not self.available():
            return []
        if not self._is_python(path):
            return []
        select = ",".join(self.config.ruff_select) if self.config.ruff_select else ""
        args = ["check", "--output-format=json", "--stdin-filename", path]
        if select:
            args += ["--select", select]
        args.append("-")
        proc = self._run_ruff(args, stdin=source)

        # ruff exits 1 when it finds problems; that is not an error for us.
        payload = proc.stdout.strip()
        if not payload:
            return []
        try:
            raw = json.loads(payload)
        except json.JSONDecodeError:
            return []
        out: list[Diagnostic] = []
        for item in raw:
            loc = item.get("location") or {}
            end = item.get("end_location") or {}
            out.append(
                Diagnostic(
                    code=str(item.get("code") or "RUFF"),
                    message=str(item.get("message") or ""),
                    line=int(loc.get("row") or 0),
                    column=int(loc.get("column") or 0),
                    end_line=int(end.get("row") or 0),
                    end_column=int(end.get("column") or 0),
                    fixable=bool(item.get("fix")),
                )
            )
        return out

    def format(self, source: str, path: str) -> Optional[str]:
        """Run ``ruff format`` and return the formatted text (or ``None``)."""
        if not self.available():
            return None
        proc = self._run_ruff(
            ["format", "--stdin-filename", path, "-"], stdin=source
        )
        if proc.returncode != 0:
            return None
        return proc.stdout

    # ── the gate ─────────────────────────────────────────────────────────
    def gate(
        self,
        original: str,
        candidate: str,
        path: str,
        *,
        allow_format: Optional[bool] = None,
        allow_lint: Optional[bool] = None,
    ) -> GateResult:
        """Validate ``candidate``; return the text that should be written.

        On a fatal failure the returned ``source`` is ``original`` and
        ``rolled_back`` is ``True``.
        """
        do_format = self.config.format_on_edit if allow_format is None else allow_format
        do_lint = self.config.lint_on_edit if allow_lint is None else allow_lint

        result = GateResult(source=candidate, ok=True)

        # 1. syntax
        if not self.syntax_ok(candidate, path):
            result.syntax_ok = False
            if self.config.rollback_on_syntax_error:
                result.source = original
                result.ok = False
                result.rolled_back = True
                result.reason = "syntax_error"
                return result
            result.ok = False
            result.reason = "syntax_error"

        # 2. format
        if do_format and self.available():
            formatted = self.format(result.source, path)
            if formatted is not None and formatted != result.source:
                result.source = formatted
                result.formatted = True
            result.format_ran = True

        # 3. lint
        if do_lint and self.available():
            diagnostics = self.check(result.source, path)
            result.diagnostics = diagnostics
            result.lint_ran = True
            fatal = [d for d in diagnostics if _is_fatal(d.code, self.config.ruff_fatal_select)]
            if fatal and self.config.rollback_on_lint_error:
                result.source = original
                result.ok = False
                result.rolled_back = True
                result.reason = f"lint_error:{fatal[0].code}"
                return result
            if fatal:
                result.ok = False
                result.reason = f"lint_error:{fatal[0].code}"

        return result


@dataclass
class LintResult:
    ok: bool
    errors: list[dict] = field(default_factory=list)
    # True when the linter could not run at all (binary missing, timeout).
    # A missing tool is not a defect in the code under test, so it must not
    # reject an edit: the module contract is "degrade to syntax-check-only".
    skipped: bool = False

    @property
    def count(self) -> int:
        return len(self.errors)


def _run_ruff_check(path: Path, *, ruff_bin: str = "") -> tuple[int, str, str]:
    """Run ``ruff check``, resolving the binary instead of assuming it.

    The hard-coded ``"ruff"`` ignored the ``Linter``'s own configured
    ``ruff_bin``, so a deployment that pinned a specific ruff still ran
    whatever happened to be first on ``PATH`` — or nothing at all.
    """
    from gigaforge.l0.toolchain import resolve_tool

    tool = resolve_tool("ruff", override=ruff_bin)
    if not tool.found:
        return -1, "", tool.reason
    try:
        proc = subprocess.run(
            [tool.path, "check", "--output-format=json", "--no-fix", str(path)],
            capture_output=True,
            text=True,
            timeout=30.0,
        )
    except FileNotFoundError:
        return -1, "", "ruff not found on PATH"
    except subprocess.TimeoutExpired:
        return -1, "", "ruff check timed out after 30s"
    return proc.returncode, proc.stdout, proc.stderr


def lint(path: Path | str) -> LintResult:
    """Run `ruff check` on path; return structured errors."""
    path = Path(path)
    rc, out, err = _run_ruff_check(path)
    if rc == 0:
        return LintResult(ok=True, errors=[])
    if rc == -1:
        # The linter itself is unavailable.  Reporting this as a lint error made
        # every edit fail on a machine without ruff, which is the opposite of
        # the documented degradation and blocked the HITL edit path entirely.
        return LintResult(ok=True, errors=[], skipped=True)
    if not out.strip():
        return LintResult(
            ok=False,
            errors=[{"line": 0, "col": 0, "code": "RUFF-INVOKE", "message": (err or "").strip() or f"ruff exited {rc} with no output"}],
        )
    try:
        data = json.loads(out)
        errors = []
        for item in data:
            loc = item.get("location", {})
            errors.append({
                "line": loc.get("row", 0),
                "col": loc.get("column", 0),
                "code": item.get("code", "RUFF"),
                "message": item.get("message", ""),
            })
        return LintResult(ok=len(errors) == 0, errors=errors)
    except json.JSONDecodeError:
        return LintResult(ok=False, errors=[{"line": 0, "col": 0, "code": "RUFF-PARSE", "message": "ruff output not JSON"}])


def format(path: Path | str) -> str:
    """Run `ruff format` in-place on file; return the (now-formatted) source."""
    from gigaforge.l0.toolchain import resolve_tool

    path = Path(path)
    tool = resolve_tool("ruff")
    if not tool.found:
        raise RuntimeError(f"{tool.reason}; cannot format {path}")
    try:
        proc = subprocess.run(
            [tool.path, "format", str(path)],
            capture_output=True,
            text=True,
            timeout=30.0,
        )
    except FileNotFoundError as e:
        raise RuntimeError(f"ruff not found on PATH; cannot format {path}") from e
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"ruff format timed out after 30s on {path}") from e
    if proc.returncode != 0:
        raise RuntimeError(f"ruff format failed: {proc.stderr}")
    return path.read_text(encoding="utf-8")


def format_code(path: Path | str) -> tuple[bool, str]:
    """Run ``ruff format`` on path.

    A missing ruff is **not** a failure of the code: it returns ``(True, source)``
    with the file unchanged, which is the same degradation ``lint()`` documents.
    This function used to return ``(False, "[Errno 2] No such file or
    directory: 'ruff'")`` — the identical defect that was fixed in ``lint()``
    one function above, left in place here.
    """
    from gigaforge.l0.toolchain import resolve_tool

    target = Path(path)
    tool = resolve_tool("ruff")
    if not tool.found:
        try:
            return True, target.read_text(encoding="utf-8")
        except OSError as exc:
            return False, str(exc)
    try:
        proc = subprocess.run(
            [tool.path, "format", str(target)],
            capture_output=True, text=True, timeout=30.0,
        )
        if proc.returncode == 0:
            return True, target.read_text(encoding="utf-8")
        return False, proc.stderr
    except Exception as e:  # noqa: BLE001 - reported, not raised
        return False, str(e)


__all__ = ["Linter", "Diagnostic", "GateResult", "LintResult", "lint", "format_code", "format"]

