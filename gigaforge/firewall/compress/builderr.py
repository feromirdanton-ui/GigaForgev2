"""Build / compiler diagnostics → structured JSON.

One compressor, five dialects, all normalized to the same record shape
``{severity, code, file, line, column, message}``:

* ``gcc`` / ``clang`` — ``path:line:col: error: msg [-Wflag]``
* ``tsc`` — ``path(line,col): error TS1234: msg``
* ``rustc`` — two-line form: ``error[E0308]: msg`` then ``  --> path:line:col``
* ``mypy`` — ``path:line: error: msg  [error-code]``
* ``pytrace`` — a Python traceback: exception type, message and frames
* ``generic`` — a best-effort ``severity word`` scan

The dialect is auto-detected (or forced), duplicate diagnostics are collapsed,
and the result is sorted severity-first then file/line, so the most useful signal
survives the truncation limits.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from ..config import FirewallConfig
from .base import CompressionResult, Truncator, finalise, squeeze, take

# ── dialect detection ────────────────────────────────────────────────────
_TSC_RE = re.compile(r"^\S.*?\(\d+,\d+\):\s*(?:error|warning)\s+TS\d+")
_RUST_HDR_RE = re.compile(r"^(?:error|warning)(?:\[(?P<code>[A-Z]\d+)\])?:\s*(?P<msg>.+)$")
_RUST_SPAN_RE = re.compile(r"^\s*-->\s+(?P<file>\S+?):(?P<line>\d+):(?P<col>\d+)\s*$")
_GCC_RE = re.compile(
    r"^(?P<file>[^\s:][^:]*?):(?P<line>\d+)(?::(?P<col>\d+))?:\s*"
    r"(?P<sev>fatal error|error|warning|note)\s*:\s*(?P<msg>.*)$"
)
_MYPY_RE = re.compile(
    r"^(?P<file>[^\s:][^:]*?):(?P<line>\d+)(?::(?P<col>\d+))?:\s*"
    r"(?P<sev>error|warning|note)\s*:\s*(?P<msg>.*?)(?:\s+\[(?P<code>[\w-]+)\])?$"
)
_PY_FRAME_RE = re.compile(r'^\s*File "(?P<file>[^"]+)", line (?P<line>\d+), in (?P<func>.+)$')
_PY_EXC_RE = re.compile(
    r"^(?P<type>[A-Za-z_][\w.]*(?:Error|Exception|Warning|Exit|Interrupt|StopIteration|SystemExit))(?::\s*(?P<msg>.*))?$"
)
_WS_FLAG_RE = re.compile(r"\s*\[-W(?P<flag>[\w=-]+)\]\s*$")

_SEVERITY_RANK = {"error": 0, "fatal error": 0, "warning": 1, "note": 2}


class BuildErrorCompressor:
    """Normalises compiler / build output across dialects."""

    kind = "builderr"

    def __init__(self, config: Optional[FirewallConfig] = None) -> None:
        self.config = config or FirewallConfig()

    # ── detection ────────────────────────────────────────────────────────
    def detect(self, source: str) -> str:
        """Return the dialect name for ``source``."""
        lines = source.splitlines()
        for line in lines:
            if _TSC_RE.match(line):
                return "tsc"
        if "Traceback (most recent call last):" in source:
            return "pytrace"
        for line in lines:
            if _RUST_HDR_RE.match(line) and "-->" in source:
                return "rustc"
        body = [ln for ln in lines if ln.strip()]
        # mypy and gcc lines share the ``path:line: error: msg`` shape, so the
        # only reliable discriminator is mypy's trailing ``[error-code]``
        # suffix.  Check it FIRST: a gcc line never carries that suffix.
        has_mypy_code = any(re.search(r"\[(?:error|warning)-[\w-]+\]\s*$", ln) for ln in body)
        if has_mypy_code:
            return "mypy"
        if any(_GCC_RE.match(ln) for ln in body):
            return "gcc"
        if any(re.search(r":\s*(?:error|warning)\b", ln) for ln in body):
            return "generic"
        return "generic"

    # ── public ───────────────────────────────────────────────────────────
    def compress(
        self, source: str, *, dialect: str = "auto", **overrides: Any
    ) -> CompressionResult:
        cfg = self.config.with_(**overrides) if overrides else self.config
        tr = Truncator()
        text = squeeze(source)
        lines = text.splitlines()
        kind = self.detect(text) if dialect in {"auto", "", None} else dialect

        extra: dict[str, Any] = {}
        if kind == "tsc":
            records = self._scan_colon_form(lines, dialect="tsc")
        elif kind == "rustc":
            records, extra = self._rustc(lines)
        elif kind == "pytrace":
            records, extra = self._pytrace(text)
        elif kind == "mypy":
            records = self._scan_colon_form(lines, dialect="mypy")
        elif kind == "gcc":
            records = self._scan_colon_form(lines, dialect="gcc")
        else:
            records = self._generic(lines)

        if cfg.build_dedup:
            records, removed = self._dedup(records)
        else:
            removed = 0
        records.sort(
            key=lambda r: (_SEVERITY_RANK.get(r["severity"], 3), r["file"], r["line"], r["message"])
        )

        errors = [r for r in records if r["severity"] in {"error", "fatal error"}]
        warnings = [r for r in records if r["severity"] not in {"error", "fatal error"}]
        errors_total, warnings_total = len(errors), len(warnings)

        kept_errors = take(errors, cfg.build_max_errors, truncator=tr)
        kept_warnings = take(warnings, cfg.build_max_warnings, truncator=tr)
        kept = kept_errors + kept_warnings
        if len(kept) > cfg.build_max_total:
            kept = take(kept, cfg.build_max_total, truncator=tr)

        data: dict[str, Any] = {
            "dialect": kind,
            "totals": {
                "errors": errors_total,
                "warnings": warnings_total,
                "returned": len(kept),
                "duplicates_removed": removed,
            },
            "errors": [r for r in kept if r["severity"] in {"error", "fatal error"}],
            "warnings": [r for r in kept if r["severity"] not in {"error", "fatal error"}],
            "files": sorted({r["file"] for r in kept if r["file"]}),
            **({"exception": extra} if extra else {}),
        }
        cut = []
        if errors_total > len(data["errors"]):
            cut.append("errors")
        if warnings_total > len(data["warnings"]):
            cut.append("warnings")
        return finalise(
            self.kind,
            data,
            source,
            tr,
            chars_per_token=cfg.chars_per_token,
            truncated_fields=cut,
        )

    # ── dialect scanners ─────────────────────────────────────────────────
    def _scan_colon_form(self, lines: list[str], *, dialect: str) -> list[dict[str, Any]]:
        """Parse ``path:line[:col]: severity: msg`` dialects.

        ``tsc`` and ``mypy`` carry a diagnostic code; ``gcc`` carries it as a
        trailing ``[-Wflag]``.  Each dialect therefore has its own regex —
        sharing one would swallow the code into the message.
        """
        out: list[dict[str, Any]] = []
        for line in lines:
            if dialect == "tsc":
                match = re.match(
                    r"^(?P<file>[^\s(][^(]*?)\((?P<line>\d+),(?P<col>\d+)\):\s*"
                    r"(?P<sev>error|warning)\s+(?P<code>TS\d+)\s*:\s*(?P<msg>.*)$",
                    line,
                )
                if match:
                    out.append(self._record(match, default_code="TS"))
                continue
            if dialect == "mypy":
                match = _MYPY_RE.match(line)
                if match:
                    out.append(
                        {
                            "severity": match.group("sev"),
                            "code": match.groupdict().get("code") or "",
                            "file": match.group("file"),
                            "line": int(match.group("line")),
                            "column": int(match.group("col") or 0),
                            "message": match.group("msg").strip(),
                        }
                    )
                continue
            match = _GCC_RE.match(line)
            if match:
                msg = match.group("msg")
                flag = _WS_FLAG_RE.search(msg)
                code = f"-W{flag.group('flag')}" if flag else ""
                out.append(
                    {
                        "severity": match.group("sev"),
                        "code": code,
                        "file": match.group("file"),
                        "line": int(match.group("line")),
                        "column": int(match.group("col") or 0),
                        "message": msg,
                    }
                )
        return out

    def _rustc(self, lines: list[str]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        out: list[dict[str, Any]] = []
        pending: Optional[dict[str, Any]] = None
        for line in lines:
            span = _RUST_SPAN_RE.match(line)
            if span and pending is not None:
                pending["file"] = span.group("file")
                pending["line"] = int(span.group("line"))
                pending["column"] = int(span.group("col"))
                out.append(pending)
                pending = None
                continue
            head = _RUST_HDR_RE.match(line)
            if head:
                if pending is not None:
                    out.append(pending)
                severity = line.split("[", 1)[0].split(":", 1)[0].strip()
                pending = {
                    "severity": severity,
                    "code": head.group("code") or "",
                    "file": "",
                    "line": 0,
                    "column": 0,
                    "message": head.group("msg"),
                }
        if pending is not None:
            out.append(pending)
        return out, {}

    def _pytrace(self, text: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Return one error record for the exception plus one entry per frame."""
        lines = text.splitlines()
        frames: list[dict[str, Any]] = []
        exc_type = ""
        exc_msg = ""
        start = 0
        for idx, line in enumerate(lines):
            if "Traceback (most recent call last):" in line:
                start = idx
        for line in lines[start:]:
            frame = _PY_FRAME_RE.match(line)
            if frame:
                frames.append(
                    {
                        "file": frame.group("file"),
                        "line": int(frame.group("line")),
                        "function": frame.group("func").strip(),
                    }
                )
                continue
            exc = _PY_EXC_RE.match(line.strip())
            if exc:
                exc_type = exc.group("type")
                exc_msg = (exc.group("msg") or "").strip()

        records: list[dict[str, Any]] = []
        origin_file, origin_line = "", 0
        if frames:
            origin_file, origin_line = frames[-1]["file"], frames[-1]["line"]
        if exc_type:
            records.append(
                {
                    "severity": "error",
                    "code": exc_type,
                    "file": origin_file,
                    "line": origin_line,
                    "column": 0,
                    "message": f"{exc_type}: {exc_msg}" if exc_msg else exc_type,
                }
            )
        for frame in frames[-5:]:
            records.append(
                {
                    "severity": "note",
                    "code": "frame",
                    "file": frame["file"],
                    "line": frame["line"],
                    "column": 0,
                    "message": f"in {frame['function']}",
                }
            )
        extra: dict[str, Any] = {
            "type": exc_type,
            "message": exc_msg,
            "frames": frames[-10:],
            "frames_total": len(frames),
        }
        return records, extra

    def _generic(self, lines: list[str]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        pattern = re.compile(
            r"^(?P<file>[^\s:][^:]*?):(?P<line>\d+)(?::(?P<col>\d+))?:\s*"
            r"(?P<sev>error|warning|note)\b[:\s]*(?P<msg>.*)$",
            re.IGNORECASE,
        )
        for line in lines:
            match = pattern.match(line)
            if match:
                out.append(self._record(match, default_code=""))
        if not out:
            for line in lines:
                lowered = line.lower()
                if "error" in lowered or "failed" in lowered:
                    out.append(
                        {
                            "severity": "error",
                            "code": "",
                            "file": "",
                            "line": 0,
                            "column": 0,
                            "message": line.strip(),
                        }
                    )
        return out

    # ── helpers ──────────────────────────────────────────────────────────
    def _record(self, match: re.Match[str], *, default_code: str) -> dict[str, Any]:
        groups = match.groupdict()
        return {
            "severity": (groups.get("sev") or "error").lower(),
            "code": groups.get("code") or default_code,
            "file": groups.get("file") or "",
            "line": int(groups.get("line") or 0),
            "column": int(groups.get("col") or 0),
            "message": groups.get("msg") or "",
        }

    @staticmethod
    def _dedup(records: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
        seen: set[tuple[str, str, int, str]] = set()
        out: list[dict[str, Any]] = []
        for rec in records:
            key = (rec["file"], rec["code"], rec["line"], rec["message"])
            if key in seen:
                continue
            seen.add(key)
            out.append(rec)
        return out, len(records) - len(out)


__all__ = ["BuildErrorCompressor"]
