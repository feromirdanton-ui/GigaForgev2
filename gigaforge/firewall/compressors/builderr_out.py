"""Firewall compressor for build / stack-trace / compiler errors.

Handles three input families and emits a single uniform shape:

- Python tracebacks (``Traceback (most recent call last): ... <ExcType>: <msg>``).
- ruff / flake8-style diagnostics (``path:line:col: CODE message``).
- gcc-style compiler diagnostics (``path:line:col: severity: message``).

Output dict: ``{"error","file","line","col","expected","actual","traceback_tail":[...]}``.
The FIRST error location + message wins. Only the last ~15 traceback lines are
kept; each line is individually token-capped.
"""
from __future__ import annotations

import re
from typing import Any

from gigaforge.firewall.base import cap_list, cap_str, enforce_token_budget, fail_dict

__all__ = ["BuildErrCompressor", "compress"]

# Python traceback frame header: ``  File "path", line N, in func``.
_TB_FRAME_RE = re.compile(
    r'^\s*File\s+"(?P<file>[^"]+)",\s+line\s+(?P<line>\d+)(?:,\s+in\s+(?P<func>\S+))?\s*$'
)
# Python traceback final line: ``ExceptionType: message``.
_TB_FINAL_RE = re.compile(r"^(?P<type>[A-Za-z_][\w.]*)(?::\s*(?P<msg>.*))?\s*$")

# ruff / gcc-style: ``path:line:col: SEVERITY/CODE: message`` or
# ``path:line:col: message`` (no severity). We require at least two colons.
_DIAG_RE = re.compile(
    r"^(?P<file>[^:\s][^:]*?):(?P<line>\d+):(?:(?P<col>\d+):)?\s*"
    r"(?:(?P<sev>error|warning|note|info)\s*:\s*)?(?P<msg>.+?)\s*$"
)

# Heuristics for extracting "expected X" / "actual Y" from a message.
_EXPECTED_RE = re.compile(
    r"expected\s+(?:'(?P<e1>[^']*)'|\"(?P<e2>[^\"]*)\"|(?P<e3>[A-Za-z_][\w\s'\"]+?))"
    r"(?:\s*(?:,|;|before|after|but|got|found))",
    re.IGNORECASE,
)
_GOT_RE = re.compile(
    r"(?:got|found|but\s+got|actual:?)\s+(?:'(?P<g1>[^']*)'|\"(?P<g2>[^\"]*)\"|(?P<g3>[A-Za-z_][\w\s'\"]+?))\s*(?:[,;.]|$)",
    re.IGNORECASE,
)

_MAX_TAIL_LINES = 15


class BuildErrCompressor:
    """Compress build / stack-trace / compiler errors to a small JSON dict."""

    name = "builderr"

    def compress(self, raw: str, *, limit_tokens: int = 4000) -> dict[str, Any]:
        if not raw or not raw.strip():
            return {"error": "empty error output", "file": "", "line": 0, "col": 0}

        try:
            result = self._parse(raw, limit_tokens=limit_tokens)
        except Exception as exc:
            return fail_dict(f"builderr parse failure: {exc.__class__.__name__}: {exc}", raw)

        return enforce_token_budget(result, limit_tokens)

    # ------------------------------------------------------------------ parse
    def _parse(self, raw: str, *, limit_tokens: int) -> dict[str, Any]:
        lines = raw.splitlines()

        # Detect a Python traceback. If present, the first error location is the
        # LAST ``File "..."`` frame (the actual failure site) and the error
        # message is the final ``ExceptionType: msg`` line.
        if _looks_like_python_traceback(lines):
            return self._parse_python_traceback(lines, limit_tokens=limit_tokens)

        # Otherwise, treat each line as a potential gcc/ruff-style diagnostic
        # and pick the FIRST one we can parse.
        return self._parse_compiler_diag(lines, limit_tokens=limit_tokens)

    # ---------------------------------------------------- python traceback
    def _parse_python_traceback(self, lines: list[str], *, limit_tokens: int) -> dict[str, Any]:
        first_file: str = ""
        first_line: int = 0
        last_file: str = ""
        last_line: int = 0
        error_msg = ""
        error_type = ""

        # We want the FIRST frame (entry point — best for "where did the call
        # originate") AND the LAST frame (where the exception was raised — best
        # for "what exactly blew up"). The spec asks for the first error
        # location; we report the LAST frame as ``file``/``line`` (the raise
        # site) and keep the entry-point frame in the traceback tail.
        for line in lines:
            m = _TB_FRAME_RE.match(line)
            if m:
                if not first_file:
                    first_file = m.group("file")
                    first_line = int(m.group("line"))
                last_file = m.group("file")
                last_line = int(m.group("line"))
                continue
            # The final ``ExcType: msg`` line. Skip if it's still inside a frame.
            if line.startswith(" "):
                continue
            if line.strip() and not line.startswith("Traceback"):
                m2 = _TB_FINAL_RE.match(line.strip())
                if m2 and not m2.group("type").startswith("File "):
                    error_type = m2.group("type")
                    error_msg = m2.group("msg") or ""

        # Keep only the LAST _MAX_TAIL_LINES lines of the traceback body.
        tail = [ln for ln in lines if ln.strip()]
        # Drop the trailing "Traceback (most recent call last):" header line if
        # it's the first entry — it carries no information.
        if tail and tail[0].strip().startswith("Traceback"):
            tail = tail[1:]
        kept_tail, dropped_tail = cap_list(tail[-_MAX_TAIL_LINES:], _MAX_TAIL_LINES)

        full_msg = f"{error_type}: {error_msg}".strip(": ").strip() if error_type else error_msg
        expected, actual = _extract_expected_actual(full_msg)

        per_field_cap = max(60, limit_tokens // 10)
        per_tail_cap = max(40, limit_tokens // 16)

        result: dict[str, Any] = {
            "error": cap_str(full_msg, per_field_cap),
            "file": cap_str(last_file or first_file, per_field_cap),
            "line": last_line or first_line or 0,
            "col": 0,
            "expected": cap_str(expected, 80),
            "actual": cap_str(actual, 80),
            "traceback_tail": [cap_str(ln, per_tail_cap) for ln in kept_tail],
        }
        if dropped_tail:
            result["_truncated_tail"] = dropped_tail
        return result

    # ------------------------------------------------------ compiler diag
    def _parse_compiler_diag(self, lines: list[str], *, limit_tokens: int) -> dict[str, Any]:
        # Note: we need access to the *raw* text for the fallback path. Stitch
        # the lines back together — they came from ``raw.splitlines()`` so the
        # join is lossless for our purposes.
        raw = "\n".join(lines)
        first: dict[str, Any] | None = None
        tail_lines: list[str] = []
        for line in lines:
            if not line.strip():
                continue
            tail_lines.append(line)
            m = _DIAG_RE.match(line)
            if m and first is None:
                sev = (m.group("sev") or "").lower()
                # Only treat as a primary error if it's not a ``note``/``info``.
                if sev not in {"note", "info"}:
                    msg = m.group("msg").strip()
                    expected, actual = _extract_expected_actual(msg)
                    first = {
                        "error": msg,
                        "file": m.group("file"),
                        "line": int(m.group("line")),
                        "col": int(m.group("col")) if m.group("col") else 0,
                        "expected": expected,
                        "actual": actual,
                    }

        if first is None:
            # Couldn't parse any diagnostic line. Fall back to a bounded signal.
            return fail_dict("no parseable compiler diagnostic found", raw, raw_limit_tokens=400)

        per_field_cap = max(60, limit_tokens // 10)
        per_tail_cap = max(40, limit_tokens // 16)

        kept_tail, dropped_tail = cap_list(tail_lines[-_MAX_TAIL_LINES:], _MAX_TAIL_LINES)

        result: dict[str, Any] = {
            "error": cap_str(first["error"], per_field_cap),
            "file": cap_str(first["file"], per_field_cap),
            "line": first["line"],
            "col": first["col"],
            "expected": cap_str(first["expected"], 80),
            "actual": cap_str(first["actual"], 80),
            "traceback_tail": [cap_str(ln, per_tail_cap) for ln in kept_tail],
        }
        if dropped_tail:
            result["_truncated_tail"] = dropped_tail
        return result


# --------------------------------------------------------------------- helpers
def _looks_like_python_traceback(lines: list[str]) -> bool:
    for line in lines[:10]:
        s = line.strip()
        if s.startswith("Traceback (most recent call last)"):
            return True
    # Some tracebacks (e.g. multiline) omit the header; detect by ``File "..."``.
    for line in lines[:20]:
        if _TB_FRAME_RE.match(line):
            return True
    return False


def _extract_expected_actual(msg: str) -> tuple[str, str]:
    """Best-effort pull of ``expected X`` / ``got Y`` clauses from a message."""
    expected = ""
    actual = ""
    m = _EXPECTED_RE.search(msg)
    if m:
        expected = m.group("e1") or m.group("e2") or (m.group("e3") or "").strip()
    m = _GOT_RE.search(msg)
    if m:
        actual = m.group("g1") or m.group("g2") or (m.group("g3") or "").strip()
    return expected, actual


# Module-level instance + convenience function.
_builderr_compressor = BuildErrCompressor()


def compress(raw: str, *, limit_tokens: int = 4000) -> dict[str, Any]:
    """Compress build / traceback / compiler errors. See :class:`BuildErrCompressor`."""
    return _builderr_compressor.compress(raw, limit_tokens=limit_tokens)
