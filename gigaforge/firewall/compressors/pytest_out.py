"""Firewall compressor for pytest console output.

Parses the canonical pytest output (>=7.x) — progress lines, the short summary
block, the failure-traceback blocks, and the final tally line — into a small
structural JSON dict. Targets >=10x compression on a 5000-line run.
"""
from __future__ import annotations

import re
from typing import Any

from gigaforge.firewall.base import cap_list, cap_str, enforce_token_budget, fail_dict
from gigaforge.firewall.tokens import truncate_for_tokens

__all__ = ["PytestCompressor", "compress", "truncate_for_tokens"]

# A per-test progress line, e.g. ``tests/test_a.py::test_one PASSED [ 1%]``.
# Status keyword is one of PASSED / FAILED / SKIPPED / ERROR / XFAIL / XPASS.
_PROGRESS_RE = re.compile(
    r"^(?P<test>\S+::\S+)\s+(?P<status>PASSED|FAILED|SKIPPED|ERROR|XFAIL|XPASS)\b"
)

# A short-summary line, e.g. ``FAILED tests/test_a.py::test_one - AssertionError``.
# Pytest also writes ``ERROR tests/...::name - reason`` for collection errors.
_SUMMARY_RE = re.compile(
    r"^(?P<status>FAILED|ERROR)\s+(?P<test>\S+::\S+)\s*-\s*(?P<reason>.*)$"
)

# The final tally line: ``= 2 failed, 98 passed, 0 skipped in 5.23s =``.
_TALLY_RE = re.compile(
    r"(?P<n>\d+)\s+(?P<kind>passed|failed|skipped|error|warnings|deselected)"
)

# The header of a failure traceback block: a row of underscores, whitespace, the
# test name (possibly parametrized), whitespace, then another row of underscores.
_TRACEBACK_HEADER_RE = re.compile(r"^_+\s+\S.*?\s+_+\s*$")

# Inside a traceback block, the canonical location line is
# ``<abs_or_rel_path>:<line>: in <funcname>``  OR
# ``<abs_or_rel_path>:<line>: <ExceptionType>`` (the assertion site).
_TRACEBACK_LOC_RE = re.compile(r"^(?P<file>[^\s:]+):(?P<line>\d+):\s*(?P<err>.*)$")

# Pytest writes ``E   <message>`` lines (the captured exception line).
_E_LINE_RE = re.compile(r"^E\s+(?P<msg>.*)$")

_MAX_FAILED = 25


class PytestCompressor:
    """Compress pytest console output to a small JSON dict."""

    name = "pytest"

    def compress(self, raw: str, *, limit_tokens: int = 4000) -> dict[str, Any]:
        if not raw or not raw.strip():
            return {"error": "empty pytest output", "raw_truncated": ""}

        try:
            result = self._parse(raw, limit_tokens=limit_tokens)
        except Exception as exc:  # never raise on malformed input
            return fail_dict(f"pytest parse failure: {exc.__class__.__name__}: {exc}", raw)

        return enforce_token_budget(result, limit_tokens)

    # ------------------------------------------------------------------ parse
    def _parse(self, raw: str, *, limit_tokens: int) -> dict[str, Any]:
        lines = raw.splitlines()

        progress_status: dict[str, int] = {}
        # test_id -> failure info. test_id is the full ``path::name`` form.
        failures: dict[str, dict[str, Any]] = {}
        # name-after-:: -> full test_id (used to map traceback headers to test_ids).
        name_to_id: dict[str, str] = {}

        # --- Pass 1: per-test progress + short summary block.
        # ``errored_tests`` tracks distinct test_ids that errored — counted once
        # even if both the progress line and the summary line mention them.
        errored_tests: set[str] = set()
        for line in lines:
            m = _PROGRESS_RE.match(line)
            if m:
                status = m.group("status")
                test = m.group("test")
                if status == "PASSED":
                    progress_status["PASSED"] = progress_status.get("PASSED", 0) + 1
                elif status == "FAILED":
                    progress_status["FAILED"] = progress_status.get("FAILED", 0) + 1
                    failures.setdefault(test, {"reason": "", "file": "", "line": 0})
                    _register_name(name_to_id, test)
                elif status == "SKIPPED":
                    progress_status["SKIPPED"] = progress_status.get("SKIPPED", 0) + 1
                elif status == "ERROR":
                    # Collection / setup error: count distinct tests.
                    if test not in errored_tests:
                        errored_tests.add(test)
                        progress_status["ERROR"] = progress_status.get("ERROR", 0) + 1
                continue
            m = _SUMMARY_RE.match(line)
            if m:
                test = m.group("test")
                reason = m.group("reason").strip()
                if m.group("status") == "FAILED":
                    failures.setdefault(test, {"reason": reason, "file": "", "line": 0})
                    _register_name(name_to_id, test)
                elif m.group("status") == "ERROR" and test not in errored_tests:
                    errored_tests.add(test)
                    progress_status["ERROR"] = progress_status.get("ERROR", 0) + 1
                continue

        # --- Pass 2: traceback blocks. Each block opens with an ``_..._ name _..._``
        # header. Track the LAST ``file:line:`` location in the block (that's the
        # assertion site — the most useful signal for a fix-loop). Map the block to
        # its test_id via the name in the header.
        in_traceback = False
        current_id: str | None = None
        last_loc: tuple[str, int, str] | None = None
        last_e_line: str | None = None

        for line in lines:
            if _TRACEBACK_HEADER_RE.match(line):
                # Close the previous block.
                if current_id is not None and last_loc is not None:
                    _fill_location(failures, current_id, last_loc, last_e_line)
                in_traceback = True
                current_id = _match_header_to_id(line, name_to_id)
                last_loc = None
                last_e_line = None
                continue
            if in_traceback:
                # A row of ``=`` characters marks the end of the block.
                if line.startswith("=" * 5) and line.rstrip("=").rstrip().endswith("="):
                    if current_id is not None and last_loc is not None:
                        _fill_location(failures, current_id, last_loc, last_e_line)
                    in_traceback = False
                    current_id = None
                    last_loc = None
                    last_e_line = None
                    continue
                loc = _TRACEBACK_LOC_RE.match(line)
                if loc:
                    last_loc = (loc.group("file"), int(loc.group("line")), loc.group("err").strip())
                e = _E_LINE_RE.match(line)
                if e:
                    last_e_line = e.group("msg").strip()

        # Close a trailing block if the file ended without an ``=`` terminator.
        if in_traceback and current_id is not None and last_loc is not None:
            _fill_location(failures, current_id, last_loc, last_e_line)

        # --- Tally from final summary line (fallback when progress not present).
        tally: dict[str, int] = {}
        for line in lines:
            if " in " in line and re.search(r"\d+\s+(passed|failed)", line):
                for m in _TALLY_RE.finditer(line):
                    tally[m.group("kind")] = int(m.group("n"))
                break

        passed_count = progress_status.get("PASSED") or tally.get("passed", 0)
        failed_count = progress_status.get("FAILED") or tally.get("failed", 0)
        error_count = progress_status.get("ERROR") or tally.get("error", 0)
        skipped_count = progress_status.get("SKIPPED") or tally.get("skipped", 0)

        failed_items: list[dict[str, Any]] = [
            {
                "test": info.get("test_id", test_id),
                "error": info.get("reason", ""),
                "file": info.get("file", ""),
                "line": info.get("line", 0) or 0,
            }
            for test_id, info in failures.items()
        ]
        failed_items.sort(key=lambda d: (d.get("file", ""), d.get("line", 0), d.get("test", "")))
        kept_failed, dropped_failed = cap_list(failed_items, _MAX_FAILED)

        per_field_cap = max(40, limit_tokens // 20)

        result: dict[str, Any] = {
            "passed_count": passed_count,
            "failed_count": failed_count,
            "error_count": error_count,
            "skipped_count": skipped_count,
            "failed": [
                {
                    "test": cap_str(it["test"], 120),
                    "error": cap_str(it["error"], per_field_cap),
                    "file": cap_str(it["file"], per_field_cap),
                    "line": it["line"],
                }
                for it in kept_failed
            ],
        }
        if dropped_failed:
            result["_truncated_failed"] = dropped_failed
        return result


# --------------------------------------------------------------------- helpers
def _register_name(name_to_id: dict[str, str], test_id: str) -> None:
    """Map ``name_after_::`` -> full test_id (first occurrence wins)."""
    if "::" in test_id:
        name = test_id.split("::", 1)[1]
        name_to_id.setdefault(name, test_id)


def _match_header_to_id(header_line: str, name_to_id: dict[str, str]) -> str | None:
    """Find the test_id for a traceback header line.

    The header looks like ``___ test_name[params] ___`` — the token between the
    underscore rows is the test name (without the file path).
    """
    # Strip leading/trailing underscore rows.
    m = re.match(r"^_+\s+(.*?)\s+_+\s*$", header_line)
    if not m:
        return None
    name = m.group(1).strip()
    # Pytest sometimes appends ``_[param]`` or includes call signatures. The
    # ``name_to_id`` map was built from the summary block, so look up the whole
    # header token first, then progressively shorter prefixes.
    if name in name_to_id:
        return name_to_id[name]
    # Parametrized: ``test_name[param1-param2]`` -> try ``test_name`` prefix.
    base = name.split("[", 1)[0]
    if base in name_to_id:
        return name_to_id[base]
    # Last resort: any name_to_id value whose name-after-:: is contained.
    for nm, tid in name_to_id.items():
        if nm == name or nm.startswith(base):
            return tid
    return None


def _fill_location(
    failures: dict[str, dict[str, Any]],
    test_id: str,
    loc: tuple[str, int, str],
    e_line: str | None,
) -> None:
    """Populate the file/line/reason for a failure from its traceback."""
    info = failures.get(test_id)
    if info is None:
        return
    info["file"] = loc[0]
    info["line"] = loc[1]
    if not info.get("reason"):
        info["reason"] = (e_line or loc[2] or "").strip()


# Module-level instance + function for registry / convenience.
_pytest_compressor = PytestCompressor()


def compress(raw: str, *, limit_tokens: int = 4000) -> dict[str, Any]:
    """Compress pytest console output. See :class:`PytestCompressor`."""
    return _pytest_compressor.compress(raw, limit_tokens=limit_tokens)
