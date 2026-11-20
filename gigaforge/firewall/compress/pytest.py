"""Pytest output → structured JSON.

Raw ``pytest`` output is dominated by captured stdout, source excerpts and
repeated tracebacks; a 40 000-character failure dump routinely carries fewer
than ten facts the agent needs.  This compressor extracts exactly those facts:

* ``summary`` — parsed counts and duration from the final ``=== ... ===`` line;
* ``failures`` — per-test name, location, error type, message, captured output;
* ``errors`` — collection / setup / teardown errors;
* ``short_summary`` — the ``short test summary info`` lines, if present;
* ``short_summary_text`` — a one-paragraph, prompt-ready recap.

Every string field is clipped to a hard limit from :class:`FirewallConfig` and
the ``truncated`` flag records whether anything was cut.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from ..config import FirewallConfig
from .base import CompressionResult, Truncator, clip, clip_multiline, finalise, squeeze, take

# ``==== FAILURES ====`` style section banner.
_SECTION_RE = re.compile(r"^=+\s*(?P<name>[A-Z][A-Za-z0-9 _-]*?)\s*=+\s*$")
# ``________ test_foo ________`` style individual test banner.
_TEST_BANNER_RE = re.compile(r"^_{3,}\s*(?P<name>.+?)\s*_{3,}\s*$")
# ``===== 2 failed, 3 passed in 0.42s =====``
_SUMMARY_LINE_RE = re.compile(r"^=+\s*(?P<body>.*\b(?:in)\s+[\d.]+s.*?)\s*=+\s*$")
_COUNT_RE = re.compile(
    r"(?P<n>\d+)\s+(?P<label>passed|failed|error|errors|skipped|xfailed|xpassed|warnings?|deselected|rerun)"
)
_DURATION_RE = re.compile(r"in\s+(?P<sec>[\d.]+)s")
# ``path/to/test_x.py:42: in test_name``
_LOCATION_RE = re.compile(r"^(?P<path>[\w./\\-]+\.py):(?P<line>\d+):\s*(?P<ctx>.*)$")
# ``E   AssertionError: ...``
_ERROR_LINE_RE = re.compile(r"^E\s+(?P<text>\S.*)$")
# ``FAILED path::test - message``
_SHORT_RE = re.compile(
    r"^(?P<status>FAILED|ERROR|XFAIL|XPASS)\s+(?P<loc>\S+?::\S+?)(?:\s+-\s+(?P<msg>.*))?$"
)
# Captured-output banners: ``---- Captured stdout call ----``
_CAPTURED_RE = re.compile(r"^-{3,}\s*(?P<name>Captured [^-]+?)\s*-{3,}\s*$")
_EXC_TYPE_RE = re.compile(r"^(?P<type>[A-Za-z_][\w.]*(?:Error|Exception|Warning|Exit|Failure|Fault))\b:?\s*(?P<msg>.*)$")


class PytestCompressor:
    """Deterministic pytest-output compressor (no LLM, no heuristics guessing)."""

    kind = "pytest"

    def __init__(self, config: Optional[FirewallConfig] = None) -> None:
        self.config = config or FirewallConfig()

    # ── public ───────────────────────────────────────────────────────────
    def compress(self, source: str, **overrides: Any) -> CompressionResult:
        cfg = self.config.with_(**overrides) if overrides else self.config
        tr = Truncator()
        text = squeeze(source)
        lines = text.splitlines()

        summary = self._summary(lines, tr)
        sections = self._split_sections(lines)
        failures = self._collect(sections.get("failures", []), "failed", cfg, tr)
        errors = self._collect(sections.get("errors", []), "error", cfg, tr)
        short = self._short_summary(sections.get("short test summary info", []), cfg, tr)

        data: dict[str, Any] = {
            "summary": summary,
            "counts": {
                "failures": len(failures),
                "errors": len(errors),
            },
            "failures": failures,
            "errors": errors,
            "short_summary": short,
            "short_summary_text": self._recap(summary, failures, errors, short),
        }
        # ``summary.counts`` reports what pytest *said*; if fewer records were
        # recovered than it announced, the payload is partial by definition and
        # must say so even when no individual field hit its own cap.
        return finalise(
            self.kind,
            data,
            source,
            tr,
            chars_per_token=cfg.chars_per_token,
            truncated_fields=self._cut_fields(summary, failures, errors, cfg, tr),
        )

    # ── summary ──────────────────────────────────────────────────────────
    def _summary(self, lines: list[str], tr: Truncator) -> dict[str, Any]:
        body = ""
        duration: Optional[float] = None
        for line in reversed(lines):
            match = _SUMMARY_LINE_RE.match(line)
            if match:
                body = match.group("body")
                dur = _DURATION_RE.search(body)
                if dur:
                    try:
                        duration = float(dur.group("sec"))
                    except ValueError:  # pragma: no cover - regex guarantees digits
                        duration = None
                break

        counts: dict[str, int] = {}
        status = "unknown"
        if body:
            for match in _COUNT_RE.finditer(body):
                label = match.group("label")
                if label == "error":
                    label = "errors"
                if label == "warning":
                    label = "warnings"
                counts[label] = counts.get(label, 0) + int(match.group("n"))
            if counts.get("failed") or counts.get("errors"):
                status = "failed"
            elif counts.get("passed"):
                status = "passed"
        else:
            # ``-q`` runs may only emit a single status char line; fall back to
            # scanning for a bare status word.
            joined = "\n".join(lines[-5:])
            for word in ("failed", "passed", "error"):
                if word in joined:
                    status = "failed" if word in {"failed", "error"} else "passed"
                    break
        return {
            "status": status,
            "counts": counts,
            "duration_s": duration,
            "raw": clip(body.strip() or "no summary line found", self.config.pytest_max_message_chars, truncator=tr),
        }

    # ── sectioning ───────────────────────────────────────────────────────
    def _split_sections(self, lines: list[str]) -> dict[str, list[str]]:
        """Split pytest output into ``{'failures': [...], 'errors': [...]}``."""
        out: dict[str, list[str]] = {}
        current: Optional[str] = None
        buffer: list[str] = []
        for line in lines:
            match = _SECTION_RE.match(line)
            if match and not _TEST_BANNER_RE.match(line):
                name = match.group("name").strip().lower()
                if current is not None:
                    out[current] = buffer
                current = name
                buffer = []
                continue
            if current is not None:
                buffer.append(line)
        if current is not None:
            out[current] = buffer
        return out

    # ── individual failures / errors ─────────────────────────────────────
    def _collect(
        self,
        lines: list[str],
        severity: str,
        cfg: FirewallConfig,
        tr: Truncator,
    ) -> list[dict[str, Any]]:
        if not lines:
            return []
        blocks: list[tuple[str, list[str]]] = []
        name: Optional[str] = None
        buffer: list[str] = []
        for line in lines:
            match = _TEST_BANNER_RE.match(line)
            if match:
                if name is not None:
                    blocks.append((name, buffer))
                name = match.group("name").strip()
                buffer = []
                continue
            if name is not None:
                buffer.append(line)
        if name is not None:
            blocks.append((name, buffer))

        limit = cfg.pytest_max_failures if severity == "failed" else cfg.pytest_max_errors
        out: list[dict[str, Any]] = []
        for block_name, block_lines in blocks[:limit]:
            out.append(self._parse_block(block_name, block_lines, severity, cfg, tr))
        if len(blocks) > limit:
            tr.note(True)
        return out

    def _parse_block(
        self,
        name: str,
        lines: list[str],
        severity: str,
        cfg: FirewallConfig,
        tr: Truncator,
    ) -> dict[str, Any]:
        location = {"path": "", "line": 0}
        exc_type = ""
        message = ""
        captured: list[dict[str, str]] = []

        i = 0
        while i < len(lines):
            line = lines[i]
            cap = _CAPTURED_RE.match(line)
            if cap:
                # Consume until the next captured banner or the block end.
                j = i + 1
                body: list[str] = []
                while j < len(lines) and not _CAPTURED_RE.match(lines[j]):
                    body.append(lines[j])
                    j += 1
                if len(captured) < cfg.pytest_max_captured_sections:
                    captured.append(
                        {
                            "name": cap.group("name").strip(),
                            "text": clip_multiline(
                                "\n".join(body).strip(),
                                cfg.pytest_max_excerpt_lines,
                                cfg.pytest_max_excerpt_chars,
                                line_chars=cfg.pytest_max_line_chars,
                                marker=cfg.truncation_marker,
                                truncator=tr,
                            ),
                        }
                    )
                elif body:
                    tr.note(True)
                i = j
                continue
            if not location["path"]:
                loc = _LOCATION_RE.match(line)
                if loc:
                    location = {"path": loc.group("path"), "line": int(loc.group("line"))}
                    context = loc.group("ctx").strip()
                    if context and not name:
                        name = context
            err = _ERROR_LINE_RE.match(line)
            if err:
                payload = err.group("text").strip()
                if not exc_type:
                    exc = _EXC_TYPE_RE.match(payload)
                    if exc:
                        exc_type = exc.group("type")
                        message = exc.group("msg").strip()
                    else:
                        message = payload
                elif not message:
                    message = payload
            i += 1

        if not exc_type:
            exc = _EXC_TYPE_RE.match(message)
            if exc:
                exc_type = exc.group("type")
                message = exc.group("msg").strip()

        excerpt = clip_multiline(
            "\n".join(lines).strip(),
            cfg.pytest_max_excerpt_lines,
            cfg.pytest_max_excerpt_chars,
            line_chars=cfg.pytest_max_line_chars,
            marker=cfg.truncation_marker,
            truncator=tr,
        )
        return {
            "name": clip(name, 200, marker=cfg.truncation_marker, truncator=tr),
            "severity": severity,
            "file": location["path"],
            "line": location["line"],
            "error_type": exc_type,
            "message": clip(message, cfg.pytest_max_message_chars, marker=cfg.truncation_marker, truncator=tr),
            "captured": captured,
            "excerpt": excerpt,
        }

    # ── short summary info ───────────────────────────────────────────────
    def _short_summary(
        self, lines: list[str], cfg: FirewallConfig, tr: Truncator
    ) -> list[dict[str, str]]:
        out: list[dict[str, str]] = []
        limit = cfg.pytest_max_failures + cfg.pytest_max_errors
        for line in lines:
            match = _SHORT_RE.match(line.strip())
            if not match:
                continue
            if len(out) >= limit:
                tr.note(True)
                break
            out.append(
                {
                    "status": match.group("status"),
                    "location": clip(match.group("loc"), 300, marker=cfg.truncation_marker, truncator=tr),
                    "message": clip(
                        (match.group("msg") or "").strip(),
                        cfg.pytest_max_message_chars,
                        marker=cfg.truncation_marker,
                        truncator=tr,
                    ),
                }
            )
        return out

    # ── helpers ──────────────────────────────────────────────────────────
    @staticmethod
    def _recap(
        summary: dict[str, Any],
        failures: list[dict[str, Any]],
        errors: list[dict[str, Any]],
        short: list[dict[str, str]],
    ) -> str:
        counts = summary.get("counts") or {}
        parts = [f"{n} {label}" for label, n in sorted(counts.items())]
        head = ", ".join(parts) if parts else summary.get("raw", "") or "no summary"
        bits = [f"pytest: {head}"]
        if summary.get("duration_s") is not None:
            bits.append(f"in {summary['duration_s']}s")
        if failures:
            first = failures[0]
            bits.append(
                f"first failure: {first.get('name','?')} "
                f"({first.get('error_type') or 'failure'}: {first.get('message','')[:120]})"
            )
        elif errors:
            first = errors[0]
            bits.append(f"first error: {first.get('name','?')} ({first.get('message','')[:120]})")
        elif short:
            bits.append(f"short summary: {len(short)} entries")
        return " | ".join(bits)

    def _cut_fields(
        self,
        summary: dict[str, Any],
        failures: list[dict[str, Any]],
        errors: list[dict[str, Any]],
        cfg: FirewallConfig,
        tr: Truncator,
    ) -> list[str]:
        """Name every field the payload could not carry in full.

        Two independent reasons are tracked, because either one means the agent
        is looking at a partial view: the announced count exceeds the records
        recovered, or a field hit its own cap.
        """
        announced = summary.get("counts") or {}
        fields: list[str] = []
        if len(failures) >= cfg.pytest_max_failures:
            fields.append("failures")
        if len(errors) >= cfg.pytest_max_errors:
            fields.append("errors")
        if int(announced.get("failed") or 0) > len(failures):
            tr.note(True)
            if "failures" not in fields:
                fields.append("failures")
        if int(announced.get("errors") or 0) > len(errors):
            tr.note(True)
            if "errors" not in fields:
                fields.append("errors")
        return fields


__all__ = ["PytestCompressor"]
