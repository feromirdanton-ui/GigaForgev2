"""Long-lived, verify-and-improve goal execution.

``TaskPipeline.run`` remains the fast one-shot authoring path.  ``GoalRunner``
adds an explicit outer loop: author a change, run deterministic project checks,
feed actionable failures back to the same pipeline, and stop only on verified
success, cancellation, an unavailable verifier, or a configured cycle limit.

The verifier deliberately uses a small set of built-in commands and always
invokes them without a shell. Model-authored command strings are never run.
Browser checking is provisioned automatically and is required for HTML
applications unless the caller explicitly chooses static-only verification.
"""
from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html.parser import HTMLParser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Optional, Protocol, Sequence
from urllib.parse import quote, unquote, urlsplit

from .cancellation import CancellationToken
from .goal_workspace import GoalWorkspace

__all__ = [
    "GoalCycle",
    "GoalResult",
    "GoalRunner",
    "GoalStore",
    "VerificationCheck",
    "VerificationReport",
    "WorkspaceVerifier",
]

_GOAL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")
_SENSITIVE_OUTPUT = re.compile(
    r"(?i)\b(token|secret|password|api[_-]?key|authorization)\b\s*[:=]\s*[^\s,;]+"
)
_PRUNE_DIRS = {".git", ".hg", ".svn", ".venv", "venv", "node_modules", "__pycache__", ".gigaforge"}

#: How many times a throttled cycle is retried before the goal reports that the
#: provider, not the work, is the problem.  Three retries with 5/10/20s backoff
#: covers a short quota window without hanging a run for minutes.
_MAX_TRANSIENT_RETRIES = 3


@dataclass
class VerificationCheck:
    """One deterministic check and the evidence it returned."""

    name: str
    status: str  # passed | failed | unavailable | skipped
    summary: str = ""
    elapsed_s: float = 0.0
    required: bool = True

    @property
    def passed(self) -> bool:
        return self.status == "passed"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "summary": self.summary,
            "elapsed_s": round(self.elapsed_s, 3),
            "required": self.required,
        }


@dataclass
class VerificationReport:
    """Combined result of every required check for one implementation cycle."""

    checks: list[VerificationCheck] = field(default_factory=list)

    @property
    def available(self) -> bool:
        return all(
            check.status != "unavailable"
            for check in self.checks
            if check.required
        )

    @property
    def passed(self) -> Optional[bool]:
        required = [check for check in self.checks if check.required]
        if any(check.status == "failed" for check in required):
            return False
        if any(check.status == "unavailable" for check in required):
            return None
        return all(check.status in {"passed", "skipped"} for check in required)

    def feedback(self, *, limit: int = 3500) -> str:
        lines = [
            f"- {check.name}: {check.status} — {check.summary}"
            for check in self.checks
            if check.required and check.status in {"failed", "unavailable"}
        ]
        return "\n".join(lines)[:limit]

    def to_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "passed": self.passed,
            "checks": [check.to_dict() for check in self.checks],
        }


class Verifier(Protocol):
    def verify(self, root: Path, changed_files: Sequence[str], goal: str) -> VerificationReport: ...


@dataclass
class GoalCycle:
    """Compact, durable summary of one author/verify/improve cycle."""

    index: int
    run_id: str
    pipeline_accepted: bool
    changed_files: list[str] = field(default_factory=list)
    summary: str = ""
    verification: Optional[dict[str, Any]] = None
    tokens: int = 0
    calls: int = 0
    duration_ms: float = 0.0
    error: str = ""
    output_fingerprint: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "run_id": self.run_id,
            "pipeline_accepted": self.pipeline_accepted,
            "changed_files": list(self.changed_files),
            "summary": self.summary,
            "verification": self.verification,
            "tokens": self.tokens,
            "calls": self.calls,
            "duration_ms": round(self.duration_ms, 3),
            "error": self.error,
            "output_fingerprint": self.output_fingerprint,
        }


@dataclass
class GoalResult:
    """Final result of a long-lived goal, including persisted progress."""

    goal_id: str
    goal: str
    status: str
    accepted: bool
    cycles: list[GoalCycle] = field(default_factory=list)
    tokens: int = 0
    budget_tokens: int = 0
    calls: int = 0
    started_at: str = ""
    updated_at: str = ""
    error: str = ""
    workspace_root: str = ""
    published_files: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "completed" and self.accepted

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal_id": self.goal_id,
            "goal": self.goal,
            "status": self.status,
            "accepted": self.accepted,
            "cycles": [cycle.to_dict() for cycle in self.cycles],
            "tokens": self.tokens,
            "budget_tokens": self.budget_tokens,
            "calls": self.calls,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "error": self.error,
            "workspace_root": self.workspace_root,
            "published_files": list(self.published_files),
        }


class GoalStore:
    """Atomic JSON persistence for resumable local goals."""

    def __init__(self, root: Path) -> None:
        self.repo_root = Path(root).resolve()
        self.root = self.repo_root / ".gigaforge" / "goals"
        self._lock = threading.RLock()

    def load(self, goal_id: str) -> dict[str, Any]:
        path = self._path(goal_id)
        with self._lock:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError as exc:
                raise ValueError(f"goal {goal_id!r} does not exist") from exc
            if not isinstance(payload, dict) or payload.get("schema_version") != 1:
                raise ValueError(f"goal {goal_id!r} has an unsupported state format")
            return payload

    def list_recent(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return compact summaries for readable goal records, newest first.

        A damaged record should not hide the other saved goals. Ignore malformed
        JSON, symlinks and records whose ID does not match the file name; the
        full state remains available through :meth:`load` for explicit resumes.
        """
        limit = max(1, min(200, int(limit)))
        if not self.root.is_dir():
            return []

        rows: list[dict[str, Any]] = []
        for path in self.root.glob("*.json"):
            if path.is_symlink() or not path.is_file():
                continue
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            if not isinstance(record, dict) or record.get("schema_version") != 1:
                continue
            goal_id = str(record.get("goal_id") or "")
            if not _GOAL_ID.fullmatch(goal_id) or path.name != f"{goal_id}.json":
                continue
            cycles = record.get("cycles")
            cycles = cycles if isinstance(cycles, list) else []
            latest = cycles[-1] if cycles and isinstance(cycles[-1], dict) else {}
            goal = str(record.get("goal") or "").strip()
            rows.append({
                "goal_id": goal_id,
                "goal": goal[:240],
                "status": str(record.get("status") or "unknown"),
                "updated_at": str(record.get("updated_at") or record.get("started_at") or ""),
                "cycle_count": len(cycles),
                "last_summary": str(latest.get("summary") or "")[:280],
                "resumable": bool(goal) and str(record.get("status") or "") != "completed",
            })
        rows.sort(key=lambda row: row["updated_at"], reverse=True)
        return rows[:limit]

    def save(self, record: dict[str, Any]) -> None:
        goal_id = str(record.get("goal_id") or "")
        path = self._path(goal_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        encoded = json.dumps(record, ensure_ascii=False, indent=2, default=str)
        with self._lock:
            try:
                temporary.write_text(encoded, encoding="utf-8")
                os.replace(temporary, path)
            finally:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    @staticmethod
    def new_id() -> str:
        return f"goal-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"

    def _path(self, goal_id: str) -> Path:
        if not _GOAL_ID.fullmatch(str(goal_id or "")):
            raise ValueError("goal_id must contain 1-80 letters, digits, underscores, or hyphens")
        candidate = self.root / f"{goal_id}.json"
        try:
            candidate.resolve().relative_to(self.repo_root)
        except ValueError as exc:
            raise ValueError("goal state path escapes the repository root") from exc
        return candidate


class _HtmlAudit(HTMLParser):
    """Collect simple structural signals without pretending to be a browser."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.html_count = 0
        self.body_count = 0
        self.ids: set[str] = set()
        self.internal_targets: set[str] = set()
        self.duplicate_ids: set[str] = set()
        self.script_srcs: list[str] = []
        self.duplicate_scripts: set[str] = set()
        self.errors: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        name = tag.lower()
        if name == "html":
            self.html_count += 1
        elif name == "body":
            self.body_count += 1
        values = {key.lower(): value or "" for key, value in attrs}
        element_id = values.get("id", "")
        if element_id:
            if element_id in self.ids:
                self.duplicate_ids.add(element_id)
            self.ids.add(element_id)
        href = values.get("href", "").strip()
        if href.startswith("#") and len(href) > 1:
            self.internal_targets.add(unquote(href[1:]))
        if name == "script" and values.get("src"):
            source = values["src"].strip()
            if source in self.script_srcs:
                self.duplicate_scripts.add(source)
            self.script_srcs.append(source)


class WorkspaceVerifier:
    """Run built-in syntax, test-suite, HTML and optional browser smoke checks."""

    def __init__(self, *, timeout_s: int = 120, require_browser: bool = True) -> None:
        self.timeout_s = max(1, int(timeout_s))
        self.require_browser = bool(require_browser)

    def verify(self, root: Path, changed_files: Sequence[str], goal: str) -> VerificationReport:
        root = Path(root).resolve()
        checks: list[VerificationCheck] = []
        from gigaforge.runtime_dependencies import ensure_goal_support_packages

        ensure_goal_support_packages(auto_install=True)
        paths = self._changed_paths(root, changed_files)
        python_files = [path for path in paths if path.suffix.lower() == ".py" and path.is_file()]
        javascript_files = [
            path for path in paths if path.suffix.lower() in {".js", ".mjs", ".cjs"}
            and path.is_file()
        ]
        html_files = [path for path in paths if path.suffix.lower() in {".html", ".htm"} and path.is_file()]

        if not paths or not (python_files or javascript_files or html_files):
            checks.append(VerificationCheck(
                "executable scope", "passed",
                "no executable source files changed; runtime checks are not applicable",
            ))
            return VerificationReport(checks)

        if python_files:
            checks.append(self._command_check(
                "Python syntax", [sys.executable, "-m", "py_compile", *map(str, python_files)], root
            ))
            api_check = self._python_api_check(python_files, goal)
            if api_check is not None:
                checks.append(api_check)
            if self._has_python_tests(root):
                if importlib.util.find_spec("pytest") is None:
                    checks.append(VerificationCheck(
                        "Python test suite", "unavailable",
                        "pytest could not be provisioned automatically",
                    ))
                else:
                    checks.append(self._command_check(
                        "Python test suite",
                        [sys.executable, "-m", "pytest", "-q", "--tb=short"], root,
                    ))

        node = self._find_command("node")
        if javascript_files:
            if node:
                for path in javascript_files:
                    checks.append(self._command_check(
                        f"JavaScript syntax: {path.relative_to(root).as_posix()}",
                        [node, "--check", str(path)], root,
                    ))
            else:
                checks.extend(
                    self._tree_sitter_javascript_check(root, path) for path in javascript_files
                )

        if html_files:
            for path in html_files:
                checks.append(self._audit_html(root, path))
            if self.require_browser:
                entry = next(
                    (path for path in html_files if path.name.lower() == "index.html"),
                    html_files[0],
                )
                checks.append(self._browser_smoke(root, entry, goal))
            else:
                checks.append(VerificationCheck(
                    "browser smoke", "skipped",
                    "static-only verification was explicitly selected", required=False,
                ))

        return VerificationReport(checks)

    @staticmethod
    def _python_api_check(
        python_files: Sequence[Path], goal: str
    ) -> VerificationCheck | None:
        """Check explicitly requested public functions and duplicate definitions."""
        started = time.monotonic()
        required = list(dict.fromkeys(re.findall(
            r"\b(?:public\s+)?(?:function|method)\s+([A-Za-z_]\w*)\s*\(",
            goal,
            re.I,
        )))
        modules: dict[Path, tuple[str, list[str], dict[str, Any]]] = {}
        issues: list[str] = []
        for path in python_files:
            try:
                source = path.read_text(encoding="utf-8")
                tree = ast.parse(source, filename=str(path))
            except (OSError, UnicodeError, SyntaxError) as exc:
                issues.append(
                    f"{path.name}: Python API could not be inspected ({type(exc).__name__})"
                )
                continue
            definitions = [
                node for node in tree.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            ]
            names = [node.name for node in definitions]
            modules[path] = (
                source,
                names,
                {node.name: node for node in definitions},
            )
            for name in sorted(set(names)):
                if not name.startswith("_") and names.count(name) > 1:
                    issues.append(
                        f"{path.name}: public function {name!r} is defined more than once"
                    )

        def tokens(name: str) -> set[str]:
            return {
                part.lower()
                for part in re.split(r"[^A-Za-z0-9]+|_", name)
                if part and part.lower() not in {"to", "from", "and"}
            }

        for name in required:
            present = {
                path: values[2][name]
                for path, values in modules.items()
                if name in values[2]
            }
            requested_tokens = tokens(name)
            scores: list[tuple[float, Path]] = []
            for path, (_source, names, _by_name) in modules.items():
                score = max(
                    (
                        len(requested_tokens & tokens(existing))
                        / max(1, len(requested_tokens | tokens(existing)))
                        for existing in names
                        if existing != name and not existing.startswith("_")
                    ),
                    default=0.0,
                )
                if score > 0:
                    scores.append((score, path))
            scores.sort(key=lambda item: (-item[0], item[1].as_posix()))
            owner = None
            if scores and scores[0][0] >= 0.5 and (
                len(scores) == 1 or scores[0][0] > scores[1][0]
            ):
                owner = scores[0][1]

            if owner is not None:
                if owner not in present:
                    issues.append(f"{owner.name}: requested function {name!r} is missing")
                misplaced = sorted(path.name for path in present if path != owner)
                if misplaced:
                    issues.append(
                        f"{name!r} belongs with the related API in {owner.name}, "
                        f"not {', '.join(misplaced)}"
                    )
            elif len(present) != 1:
                issues.append(
                    f"expected exactly one definition of requested function {name!r} "
                    f"across changed Python files, found {len(present)}"
                )

            strict_hms = name == "hms_to_seconds" and bool(re.search(
                r"\bstrict\b[\s\S]{0,40}\bHH:MM:SS\b|\bHH:MM:SS\b[\s\S]{0,40}\bstrict\b",
                goal,
                re.I,
            ))
            if strict_hms and present:
                source = modules[next(iter(present))][0]
                body = ast.get_source_segment(source, next(iter(present.values()))) or ""
                fixed_width = bool(
                    re.search(r"(?:\\d|\[0-9\])\{2\}", body)
                    or re.search(r"len\(\s*hms\s*\)\s*(?:!=|==)\s*8", body)
                    or re.search(r"len\(\s*part\s*\)\s*(?:!=|==)\s*2", body)
                )
                if not fixed_width:
                    issues.append(
                        "hms_to_seconds does not enforce two-digit HH:MM:SS fields"
                    )

        if not required and not any("defined more than once" in issue for issue in issues):
            return None
        return VerificationCheck(
            "Python API contract",
            "failed" if issues else "passed",
            "; ".join(issues) if issues else "requested public functions are present and unique",
            time.monotonic() - started,
        )

    @staticmethod
    def _changed_paths(root: Path, changed_files: Sequence[str]) -> list[Path]:
        paths: list[Path] = []
        for raw in changed_files:
            candidate = (root / str(raw)).resolve()
            try:
                candidate.relative_to(root)
            except ValueError:
                continue
            paths.append(candidate)
        return paths

    def _command_check(self, name: str, argv: list[str], root: Path) -> VerificationCheck:
        started = time.monotonic()
        try:
            completed = subprocess.run(
                argv,
                cwd=root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout_s,
                check=False,
                shell=False,
            )
        except FileNotFoundError as exc:
            return VerificationCheck(name, "unavailable", _safe_excerpt(str(exc)), time.monotonic() - started)
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout or ""
            stderr = exc.stderr or ""
            if isinstance(stdout, bytes):
                stdout = stdout.decode("utf-8", errors="replace")
            if isinstance(stderr, bytes):
                stderr = stderr.decode("utf-8", errors="replace")
            excerpt = _safe_excerpt(stdout + "\n" + stderr)
            return VerificationCheck(
                name, "failed", f"timed out after {self.timeout_s}s. {excerpt}".strip(),
                time.monotonic() - started,
            )
        except OSError as exc:
            return VerificationCheck(name, "unavailable", _safe_excerpt(str(exc)), time.monotonic() - started)

        elapsed = time.monotonic() - started
        if completed.returncode == 0:
            return VerificationCheck(name, "passed", "command exited with code 0", elapsed)
        if completed.returncode == 5 and "pytest" in argv:
            return VerificationCheck(name, "passed", "pytest found no tests to collect", elapsed)
        output = _safe_excerpt((completed.stdout or "") + "\n" + (completed.stderr or ""))
        return VerificationCheck(
            name, "failed", f"exit code {completed.returncode}: {output}".strip(), elapsed
        )

    def _audit_html(self, root: Path, path: Path) -> VerificationCheck:
        started = time.monotonic()
        try:
            parser = _HtmlAudit()
            parser.feed(path.read_text(encoding="utf-8", errors="replace"))
            parser.close()
        except Exception as exc:  # noqa: BLE001 - malformed HTML is an actionable check failure
            return VerificationCheck(
                f"HTML structure: {path.relative_to(root).as_posix()}", "failed",
                f"{type(exc).__name__}: {exc}", time.monotonic() - started,
            )

        issues: list[str] = []
        if parser.html_count > 1:
            issues.append(f"multiple <html> roots ({parser.html_count})")
        if parser.body_count > 1:
            issues.append(f"multiple <body> elements ({parser.body_count})")
        if parser.duplicate_ids:
            issues.append("duplicate id values: " + ", ".join(sorted(parser.duplicate_ids)[:8]))
        missing_targets = parser.internal_targets - parser.ids
        if missing_targets:
            issues.append(
                "internal links target missing ids: "
                + ", ".join(f"#{target}" for target in sorted(missing_targets)[:8])
            )
        if parser.duplicate_scripts:
            issues.append("scripts loaded more than once: " + ", ".join(sorted(parser.duplicate_scripts)[:5]))
        # Structural defects a browser shows but the tag counters above cannot
        # see: a stylesheet printed as prose in <body>, and a block emitted
        # twice.  Measured live: a page with 1956 characters of CSS in its body
        # and a doubled <section>/<footer> passed every check above.
        try:
            from gigaforge.harness.html_structure import audit_html_structure

            structure = audit_html_structure(path.read_text(encoding="utf-8", errors="replace"))
            issues.extend(structure.issues())
        except Exception as exc:  # noqa: BLE001 - a broken audit must not pass silently
            issues.append(f"structural audit could not run: {type(exc).__name__}: {exc}")
        missing: list[str] = []
        for source in parser.script_srcs:
            parts = urlsplit(source)
            if parts.scheme or source.startswith("//") or not parts.path:
                continue
            target = (
                root / parts.path.lstrip("/")
                if parts.path.startswith("/")
                else path.parent / parts.path
            ).resolve()
            try:
                target.relative_to(root)
            except ValueError:
                missing.append(f"{source} (outside project root)")
                continue
            if not target.is_file():
                missing.append(source)
        if missing:
            issues.append("missing local scripts: " + ", ".join(missing[:8]))
        name = f"HTML structure: {path.relative_to(root).as_posix()}"
        if issues:
            return VerificationCheck(name, "failed", "; ".join(issues), time.monotonic() - started)
        return VerificationCheck(
            name, "passed",
            f"{len(parser.ids)} unique ids; {len(parser.script_srcs)} local/external script references",
            time.monotonic() - started,
        )

    def _browser_smoke(self, root: Path, html_path: Path, goal: str = "") -> VerificationCheck:
        started = time.monotonic()
        from gigaforge.runtime_dependencies import ensure_browser_runtime

        runtime = ensure_browser_runtime(auto_install=True)
        if not runtime.available:
            return VerificationCheck(
                "browser runtime", "unavailable",
                "automatic Playwright/Chromium setup failed: " + runtime.detail,
                time.monotonic() - started,
            )
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return VerificationCheck(
                "browser runtime", "unavailable",
                "Playwright is unavailable after automatic setup; restart the app to retry",
                time.monotonic() - started,
            )

        class QuietHandler(SimpleHTTPRequestHandler):
            def log_message(self, _format: str, *_args: Any) -> None:
                return

        server: Optional[ThreadingHTTPServer] = None
        browser: Any = None
        page_errors: list[str] = []
        console_errors: list[str] = []
        try:
            handler = lambda *args, **kwargs: QuietHandler(  # noqa: E731
                *args, directory=str(root), **kwargs
            )
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            server.daemon_threads = True
            threading.Thread(target=server.serve_forever, daemon=True).start()
            relative = html_path.relative_to(root).as_posix()
            url = f"http://127.0.0.1:{server.server_port}/{quote(relative)}"
            with sync_playwright() as playwright:
                try:
                    browser = playwright.chromium.launch(headless=True)
                except Exception as exc:
                    return VerificationCheck(
                        "browser runtime", "unavailable",
                        "Chromium could not launch after automatic setup: "
                        + _safe_excerpt(str(exc), limit=350), time.monotonic() - started,
                    )
                page = browser.new_page()
                page.on("pageerror", lambda exc: page_errors.append(str(exc)))
                page.on(
                    "console",
                    lambda message: console_errors.append(message.text)
                    if message.type == "error" else None,
                )
                is_tetris = bool(re.search(r"\b(?:tetris|тетрис)\b", str(goal), re.I))
                if is_tetris:
                    page.add_init_script("""
                        window.__gigaforgeTimerTicks = 0;
                        const originalTimeout = window.setTimeout.bind(window);
                        window.setTimeout = (callback, delay, ...args) => originalTimeout(
                            (...inner) => {
                                if (Number(delay) >= 300) window.__gigaforgeTimerTicks++;
                                return callback(...inner);
                            }, delay, ...args
                        );
                        const originalInterval = window.setInterval.bind(window);
                        window.setInterval = (callback, delay, ...args) => originalInterval(
                            (...inner) => {
                                if (Number(delay) >= 300) window.__gigaforgeTimerTicks++;
                                return callback(...inner);
                            }, delay, ...args
                        );
                    """)
                response = page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_s * 1000)
                if response is None or response.status >= 400:
                    code = response.status if response is not None else "no response"
                    return VerificationCheck(
                        "browser runtime", "failed", f"page load returned {code}",
                        time.monotonic() - started,
                    )
                if is_tetris:
                    return self._tetris_browser_smoke(
                        page, html_path, relative, started, page_errors, console_errors
                    )
                # Start controls where available, then exercise common game/app
                # keys. This catches event-handler errors without assuming a DOM API.
                try:
                    page.get_by_role("button", name=re.compile(r"start|play|начать", re.I)).first.click(
                        timeout=500
                    )
                except Exception:  # no matching control is a normal page shape
                    pass
                for key in ("w", "a", "s", "d", "ArrowLeft", "ArrowRight", "Space", "Enter"):
                    try:
                        page.keyboard.press(key)
                    except Exception:
                        break
                page.wait_for_timeout(250)
                if page_errors or console_errors:
                    errors = [*_unique(page_errors), *_unique(console_errors)]
                    detail = " | ".join(_safe_excerpt(error, limit=350) for error in errors[:6])
                    return VerificationCheck(
                        "browser runtime", "failed", f"JavaScript runtime errors: {detail}",
                        time.monotonic() - started,
                    )
                return VerificationCheck(
                    "browser runtime", "passed",
                    f"loaded {relative} and exercised keyboard input; no page or console errors",
                    time.monotonic() - started,
                )
        except Exception as exc:  # noqa: BLE001 - surfaced as a failed runtime check
            return VerificationCheck(
                "browser runtime", "failed",
                f"{type(exc).__name__}: {_safe_excerpt(str(exc), limit=500)}",
                time.monotonic() - started,
            )
        finally:
            if browser is not None:
                try:
                    browser.close()
                except Exception:
                    pass
            if server is not None:
                server.shutdown()
                server.server_close()

    def _tetris_browser_smoke(
        self,
        page: Any,
        html_path: Path,
        relative: str,
        started: float,
        page_errors: list[str],
        console_errors: list[str],
    ) -> VerificationCheck:
        """Exercise the requested Tetris contract instead of accepting page load alone."""
        issues: list[str] = []
        source = html_path.read_text(encoding="utf-8", errors="replace")
        scripts = "\n".join(page.locator("script").all_text_contents())
        handler_match = re.search(
            r"(?:addEventListener\s*\(\s*['\"]keydown['\"]|onkeydown\s*=)[\s\S]{0,1800}",
            scripts,
            re.I,
        )
        key_handler = handler_match.group(0).lower() if handler_match else ""
        if not key_handler:
            issues.append("no keyboard control handler is registered")
        key_patterns = {
            "A/left": r"(?:case\s*['\"]a['\"]|(?:key|code)\s*={2,3}\s*['\"](?:a|keya)['\"])",
            "D/right": r"(?:case\s*['\"]d['\"]|(?:key|code)\s*={2,3}\s*['\"](?:d|keyd)['\"])",
            "S/soft drop": r"(?:case\s*['\"]s['\"]|(?:key|code)\s*={2,3}\s*['\"](?:s|keys)['\"])",
            "W/rotate": r"(?:case\s*['\"]w['\"]|(?:key|code)\s*={2,3}\s*['\"](?:w|keyw)['\"])",
            "Space/hard drop": r"(?:case\s*['\"](?:space| )['\"]|code\s*={2,3}\s*['\"]space['\"])",
            "P/pause": r"(?:case\s*['\"]p['\"]|key\s*={2,3}\s*['\"]p['\"])",
            "R/restart": r"(?:case\s*['\"]r['\"]|key\s*={2,3}\s*['\"]r['\"])",
        }
        for label, pattern in key_patterns.items():
            if not re.search(pattern, key_handler, re.I):
                issues.append(f"keyboard map is missing {label}")

        has_viewport = page.locator('meta[name="viewport"]').count() > 0
        if not has_viewport:
            issues.append("mobile viewport metadata is missing")
        body_text = page.locator("body").inner_text(timeout=3000).lower()
        for label, pattern in (
            ("score", r"score|очки"),
            ("level", r"level|уровень"),
            ("next piece", r"next|следующ"),
            ("hold piece", r"hold|запасн|удерж"),
            ("best score", r"best|лучший"),
        ):
            if not re.search(pattern, body_text, re.I):
                issues.append(f"visible {label} indicator is missing")

        board_candidates = (
            "canvas", "[role='grid']", "#gameContainer", "#game-grid",
            "[data-testid*='board']", "[class*='board']",
        )
        board_selector = next(
            (selector for selector in board_candidates if page.locator(selector).count()), ""
        )
        if not board_selector:
            issues.append("playfield is missing")
        else:
            board = page.locator(board_selector).first
            board_metrics = board.evaluate("""element => ({
                width: Math.round(element.getBoundingClientRect().width),
                height: Math.round(element.getBoundingClientRect().height),
                cells: element.querySelectorAll('[role=gridcell], .grid-cell, .block').length,
                canvas: element.tagName.toLowerCase() === 'canvas'
            })""")
            if not board_metrics["canvas"] and board_metrics["cells"] < 200:
                issues.append(f"playfield exposes only {board_metrics['cells']} of 200 cells")
            if board_metrics["height"] > page.viewport_size["height"] * 1.25:
                issues.append("playfield is taller than the viewport and forces excessive scrolling")

        source_lower = source.lower()
        if not re.search(r"touch|data-control|aria-label", source_lower):
            issues.append("touch-friendly controls or accessible control labels are missing")
        if not re.search(r"localstorage", source_lower):
            issues.append("best score is not persisted in local storage")

        game_over_dialogs: list[str] = []

        def _accept_dialog(dialog: Any) -> None:
            game_over_dialogs.append(dialog.message)
            dialog.accept()

        page.on("dialog", _accept_dialog)
        try:
            visible_width = page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            if not visible_width:
                issues.append("page has horizontal overflow at desktop width")
            page.set_viewport_size({"width": 390, "height": 844})
            page.wait_for_timeout(80)
            if not page.evaluate("document.documentElement.scrollWidth <= innerWidth"):
                issues.append("page has horizontal overflow at mobile width")
            page.set_viewport_size({"width": 1280, "height": 800})
            page.locator("body").click(position={"x": 8, "y": 8}, force=True)
            snapshot = lambda: page.evaluate("""selector => {
                const element = document.querySelector(selector);
                if (!element) return '';
                return element.tagName.toLowerCase() === 'canvas'
                    ? element.toDataURL() : element.outerHTML;
            }""", board_selector) if board_selector else ""
            for key in ("a", "d", "s"):
                before = snapshot()
                page.keyboard.press(key)
                page.wait_for_timeout(70)
                if snapshot() == before:
                    issues.append(f"{key.upper()} key produced no visible playfield change")
            if board_selector:
                before = snapshot()
                page.keyboard.press("Space")
                page.wait_for_timeout(90)
                if snapshot() == before:
                    issues.append("Space did not visibly hard-drop or lock the piece")
                for _ in range(36):
                    page.keyboard.press("Space")
                    page.wait_for_timeout(45)
                    game_text = page.locator("body").inner_text(timeout=1000).lower()
                    if game_over_dialogs or re.search(
                        r"game\s*over|игра\s+окончена|конец\s+игры", game_text, re.I
                    ):
                        break
                else:
                    issues.append("repeated hard drops never reached a visible game-over state")
                if page.get_by_role(
                    "button", name=re.compile(r"restart|new game|новая игра|перезапуск", re.I)
                ).count() == 0:
                    issues.append("restart control is missing after game over")
        except Exception as exc:  # noqa: BLE001 - browser action issues become verifier feedback
            issues.append("interactive browser check failed: " + _safe_excerpt(str(exc), limit=300))

        if page_errors or console_errors:
            errors = [*_unique(page_errors), *_unique(console_errors)]
            issues.append(
                "JavaScript errors: "
                + " | ".join(_safe_excerpt(error, limit=250) for error in errors[:4])
            )
        if issues:
            return VerificationCheck(
                "browser runtime", "failed",
                "Tetris acceptance checks failed: " + "; ".join(issues[:12]),
                time.monotonic() - started,
            )
        return VerificationCheck(
            "browser runtime", "passed",
            f"loaded {relative}; board, keyboard, score, mobile layout and touch controls passed",
            time.monotonic() - started,
        )

    @staticmethod
    def _find_command(name: str) -> Optional[str]:
        from shutil import which

        return which(name)

    @staticmethod
    def _tree_sitter_javascript_check(root: Path, path: Path) -> VerificationCheck:
        """Parse JavaScript without requiring a separate Node.js installation."""
        name = f"JavaScript syntax: {path.relative_to(root).as_posix()}"
        try:
            import tree_sitter_javascript
            from tree_sitter import Language, Parser

            parser = Parser(Language(tree_sitter_javascript.language()))
            tree = parser.parse(path.read_bytes())
        except ImportError as exc:
            return VerificationCheck(
                name, "unavailable",
                f"JavaScript parser is unavailable: {_safe_excerpt(str(exc), limit=250)}",
            )
        except Exception as exc:  # noqa: BLE001 - parser errors are surfaced as verifier results
            return VerificationCheck(
                name, "failed", f"{type(exc).__name__}: {_safe_excerpt(str(exc), limit=300)}",
            )
        if not tree.root_node.has_error:
            return VerificationCheck(name, "passed", "parsed with the bundled Tree-sitter grammar")
        nodes = [tree.root_node]
        while nodes:
            current = nodes.pop()
            if current.is_error or current.is_missing:
                return VerificationCheck(
                    name, "failed", f"syntax error at line {current.start_point.row + 1}"
                )
            nodes.extend(reversed(current.children))
        return VerificationCheck(name, "failed", "Tree-sitter reported a JavaScript syntax error")

    @staticmethod
    def _has_python_tests(root: Path) -> bool:
        for _current, dirs, files in os.walk(root):
            dirs[:] = [name for name in dirs if name not in _PRUNE_DIRS]
            if any(name in {"test", "tests"} for name in dirs):
                return True
            if any(name.startswith("test_") and name.endswith(".py") for name in files):
                return True
            if any(name.endswith("_test.py") for name in files):
                return True
        return False


class GoalRunner:
    """Keep improving one task until its workspace checks pass or it is blocked."""

    def __init__(
        self,
        pipeline: Any,
        *,
        repo_root: Path,
        verifier: Optional[Verifier] = None,
        store: Optional[GoalStore] = None,
        workspace: Optional[GoalWorkspace] = None,
    ) -> None:
        self.pipeline = pipeline
        self.repo_root = Path(repo_root).resolve()
        if not self.repo_root.is_dir():
            raise ValueError(f"repository root does not exist: {self.repo_root}")
        self.verifier: Verifier = verifier or WorkspaceVerifier()
        self.store = store or GoalStore(self.repo_root)
        self.workspace = workspace

    def run(
        self,
        goal: str = "",
        *,
        goal_id: Optional[str] = None,
        resume: bool = False,
        max_cycles: int = 8,
        max_fix_iterations: int = 2,
        use_subagents: bool = False,
        files: Sequence[str] = (),
        symbols: Sequence[str] = (),
        budget_tokens: int = 0,
        conversation_context: str = "",
        additional_context: str = "",
        approve: Optional[Callable[[str], bool]] = None,
        cancellation: Optional[CancellationToken] = None,
    ) -> GoalResult:
        """Run bounded agent cycles and persist each observation for resume."""
        max_cycles = int(max_cycles)
        if not 1 <= max_cycles <= 20:
            raise ValueError("max_cycles must be between 1 and 20")
        if max_fix_iterations < 1:
            raise ValueError("max_fix_iterations must be at least 1")
        budget_tokens = int(budget_tokens)
        if budget_tokens < 0:
            raise ValueError("budget_tokens cannot be negative")

        if resume:
            if not goal_id:
                raise ValueError("goal_id is required when resuming a goal")
            record = self.store.load(goal_id)
            saved_goal = str(record.get("goal") or "").strip()
            if goal and goal.strip() != saved_goal:
                raise ValueError("the supplied goal does not match the saved goal")
            goal = saved_goal
            if not goal:
                raise ValueError(f"goal {goal_id!r} has no saved objective")
            if record.get("status") == "completed":
                self._emit_goal_event(
                    "goal.finish", node="Goal", status="Цель уже завершена",
                    goal_id=goal_id, resumed=True,
                )
                return self._result(record)
            # A zero budget on resume means "keep the saved limit", not
            # "remove the cap". Pass a larger total allowance to extend it.
            budget_tokens = budget_tokens or int(record.get("budget_tokens") or 0)
            record["budget_tokens"] = budget_tokens
        else:
            goal = str(goal or "").strip()
            if not goal:
                raise ValueError("goal cannot be empty")
            goal_id = goal_id or self.store.new_id()
            if self._goal_exists(goal_id):
                raise ValueError(f"goal {goal_id!r} already exists; use resume")
            stamp = _now()
            record = {
                "schema_version": 1,
                "goal_id": goal_id,
                "goal": goal,
                "status": "running",
                "accepted": False,
                "cycles": [],
                "active_run_id": "",
                "active_cycle_index": 0,
                "tokens": 0,
                "budget_tokens": budget_tokens,
                "conversation_context": "",
                "calls": 0,
                "started_at": stamp,
                "updated_at": stamp,
                "error": "",
            }
            self.store.save(record)

        record["conversation_context"] = _merge_conversation_context(
            str(record.get("conversation_context") or ""),
            conversation_context,
            additional_context,
        )
        self.store.save(record)

        assert goal_id is not None
        if self.workspace is not None:
            record["workspace_root"] = str(self.workspace.root)
            self.store.save(record)

        self._emit_goal_event(
            "goal.start", node="Goal", status="Продолжаю цель" if resume else "Цель запущена",
            goal_id=goal_id, resumed=resume,
            completed_cycles=len(record.get("cycles") or []), cycle_limit=max_cycles,
        )

        runtime_issues = self._runtime_preflight()
        if runtime_issues:
            detail = "Goal runtime is not ready: " + "; ".join(runtime_issues)
            self._set_status(record, "blocked", accepted=False, error=detail)
            self.store.save(record)
            self._emit_goal_event(
                "goal.finish", node="Goal", status="Среда требует внимания",
                goal_id=goal_id, summary=detail,
            )
            return self._result(record)

        cancel = cancellation or CancellationToken()

        # A blocked verifier should be retryable after installing its runtime.
        # Re-run the last accepted artifact before asking the model for another diff.
        previous = record["cycles"][-1] if record.get("cycles") else None
        if resume and previous and previous.get("pipeline_accepted"):
            verification = previous.get("verification") or {}
            if verification.get("available") is False:
                self._emit_goal_event(
                    "goal.verify_start", node="Повторная проверка",
                    status="Проверяю сохранённый результат", goal_id=goal_id,
                    cycle_index=int(previous.get("index") or len(record["cycles"])),
                )
                resume_report = self.verifier.verify(
                    self.repo_root, previous.get("changed_files") or [], goal
                )
                previous["verification"] = resume_report.to_dict()
                self._emit_goal_event(
                    "goal.verify_finish", node="Повторная проверка",
                    status=("Проверка пройдена" if resume_report.passed is True else
                            "Проверка недоступна" if resume_report.passed is None else
                            "Проверка нашла ошибки"),
                    goal_id=goal_id,
                    cycle_index=int(previous.get("index") or len(record["cycles"])),
                    summary=_safe_excerpt(resume_report.feedback(), limit=700),
                )
                self._set_status(record, "running", error="")
                if resume_report.passed is True:
                    self._complete(record)
                    self._emit_goal_event(
                        "goal.finish", node="Goal", status="Цель завершена",
                        goal_id=goal_id, summary="Сохранённый результат прошёл проверку.",
                    )
                    return self._result(record)
                if resume_report.passed is None:
                    self._set_status(record, "blocked", accepted=False,
                                     error=resume_report.feedback() or "required verifier is unavailable")
                    self.store.save(record)
                    self._emit_goal_event(
                        "goal.finish", node="Goal", status="Цель заблокирована",
                        goal_id=goal_id, goal_status="blocked",
                        summary=_safe_excerpt(resume_report.feedback(), limit=700),
                    )
                    return self._result(record)
                self.store.save(record)

        transient_retries = 0
        for _ in range(max_cycles):
            if cancel.cancelled:
                self._set_status(record, "cancelled", error=cancel.reason or "cancelled")
                break

            tokens_used = max(0, int(record.get("tokens") or 0))
            budget_remaining = _remaining_goal_budget(budget_tokens, tokens_used)
            if budget_tokens and budget_remaining == 0:
                message = (
                    f"Goal token budget exhausted ({tokens_used}/{budget_tokens}); "
                    "increase budget_tokens to resume."
                )
                self._set_status(record, "blocked", accepted=False, error=message)
                self.store.save(record)
                self._emit_goal_event(
                    "goal.budget_exhausted",
                    node=f"Цикл {len(record.get('cycles') or []) + 1}",
                    status="Остановлено: исчерпан бюджет токенов",
                    goal_id=goal_id, tokens_used=tokens_used,
                    budget_tokens=budget_tokens,
                )
                break

            pending_run_id = str(record.get("active_run_id") or "") if resume else ""
            cycle_index = (
                int(record.get("active_cycle_index") or len(record.get("cycles") or []) + 1)
                if pending_run_id else len(record.get("cycles") or []) + 1
            )
            run_id = pending_run_id or f"{goal_id}-c{cycle_index}-{uuid.uuid4().hex[:6]}"
            feedback = self._last_feedback(record)
            task_prompt = self._cycle_prompt(goal, feedback)
            record["active_run_id"] = run_id
            record["active_cycle_index"] = cycle_index
            self._set_status(record, "running", accepted=False, error="")
            self.store.save(record)
            self._emit_goal_event(
                "goal.cycle_start", node=f"Цикл {cycle_index} из {max_cycles}",
                status="Агент вносит изменения", goal_id=goal_id,
                cycle_index=cycle_index, cycle_limit=max_cycles,
                resumed=bool(pending_run_id),
            )
            started = time.monotonic()
            try:
                result = self.pipeline.run(
                    task_prompt,
                    run_id=run_id,
                    files=files,
                    symbols=symbols,
                    budget_tokens=budget_remaining,
                    use_cache=False,
                    dry_run=False,
                    use_subagents=use_subagents,
                    react=True,
                    max_fix_iterations=max_fix_iterations,
                    approve=approve,
                    cancellation=cancel,
                    resume=bool(pending_run_id),
                    conversation_context=str(record.get("conversation_context") or ""),
                )
            except Exception as exc:  # noqa: BLE001 - preserve a resumable failure record
                record["active_run_id"] = ""
                record["active_cycle_index"] = 0
                cycle = GoalCycle(
                    index=cycle_index,
                    run_id=run_id,
                    pipeline_accepted=False,
                    summary="pipeline execution raised an exception",
                    error=f"{type(exc).__name__}: {str(exc)[:700]}",
                    duration_ms=(time.monotonic() - started) * 1000,
                )
                record.setdefault("cycles", []).append(cycle.to_dict())
                record["error"] = cycle.error
                self._set_status(
                    record,
                    "cancelled" if cancel.cancelled else "failed",
                    error=cancel.reason if cancel.cancelled else cycle.error,
                )
                self.store.save(record)
                self._emit_goal_event(
                    "goal.cycle_finish", node=f"Цикл {cycle_index}",
                    status="Изменения не завершены", goal_id=goal_id,
                    cycle_index=cycle_index,
                    summary=_safe_excerpt(cycle.error, limit=700),
                )
                break

            accepted = bool(getattr(result, "accepted", False)) and str(
                getattr(result, "status", "") or ""
            ) != "failed"
            changed = list(getattr(result, "changed_files", []) or [])
            report: Optional[VerificationReport] = None
            error = str(getattr(result, "error", "") or "")
            if accepted:
                self._emit_goal_event(
                    "goal.verify_start", node=f"Цикл {cycle_index} из {max_cycles}",
                    status="Проверяю результат", goal_id=goal_id,
                    cycle_index=cycle_index, cycle_limit=max_cycles,
                )
                report = self.verifier.verify(self.repo_root, changed, goal)
                self._emit_goal_event(
                    "goal.verify_finish", node=f"Цикл {cycle_index} из {max_cycles}",
                    status=("Проверка пройдена" if report.passed is True else
                            "Проверка недоступна" if report.passed is None else
                            "Проверка нашла ошибки"),
                    goal_id=goal_id, cycle_index=cycle_index,
                    cycle_limit=max_cycles, passed=report.passed,
                    summary=_safe_excerpt(report.feedback(), limit=700),
                )
            elif not error:
                error = self._pipeline_feedback(result)

            # A rejected cycle used to carry only the pipeline's own reason, and
            # the verifier was never consulted — so the next cycle was told
            # "rejected" and not what was wrong with the artifact.  Measured: a
            # page with a duplicated stylesheet and a stray </style> in the body
            # was rejected three cycles running, each cycle re-deriving the same
            # mistake, because nothing ever said "the body contains CSS text".
            #
            # The cheap subset runs here: syntax and the HTML audit, no test
            # suite and no browser.  It is the part that is a pure function of
            # the file, so it costs milliseconds and cannot itself fail for
            # environmental reasons.  Its findings are appended to the error,
            # which is what the next cycle reads.
            if not accepted and changed:
                cheap = self._cheap_verification(changed, goal)
                if cheap:
                    error = (error + "\n" if error else "") + cheap
                    self._emit_goal_event(
                        "goal.verify_on_reject",
                        node=f"Цикл {cycle_index} из {max_cycles}",
                        status="Проверяю отклонённый результат",
                        goal_id=goal_id, cycle_index=cycle_index,
                        summary=_safe_excerpt(cheap, limit=700),
                    )

            # A provider that is throttling us has not judged the work.  Measured
            # live: two runs in parallel exhausted the quota, every cycle failed
            # with GF-RATE-429, and the goal burned all three cycles in under a
            # minute and reported "cycle limit reached before verification
            # passed" — a statement about the work that was really a statement
            # about the quota.  A transient failure waits and retries the same
            # cycle; it does not consume one.
            if not accepted and _is_transient_provider_error(error):
                if transient_retries < _MAX_TRANSIENT_RETRIES:
                    transient_retries += 1
                    delay = min(60.0, 5.0 * (2 ** (transient_retries - 1)))
                    self._emit_goal_event(
                        "goal.rate_limited", node=f"Цикл {cycle_index} из {max_cycles}",
                        status=f"Провайдер ограничил запросы, жду {delay:.0f}с",
                        goal_id=goal_id, cycle_index=cycle_index,
                        retry=transient_retries, delay_s=delay,
                        summary=_safe_excerpt(error, limit=300),
                    )
                    record["active_run_id"] = ""
                    record["active_cycle_index"] = 0
                    self._set_status(record, "running", accepted=False, error=error)
                    self.store.save(record)
                    if _wait_or_cancel(cancel, delay):
                        self._set_status(record, "cancelled", error=cancel.reason or "cancelled")
                        break
                    continue
                self._set_status(
                    record, "blocked", accepted=False,
                    error=("the provider kept rate-limiting after "
                           f"{_MAX_TRANSIENT_RETRIES} retries; the work was not judged: "
                           + _safe_excerpt(error, limit=400)),
                )
                self.store.save(record)
                break
            transient_retries = 0

            cycle = GoalCycle(
                index=cycle_index,
                run_id=run_id,
                pipeline_accepted=accepted,
                changed_files=changed,
                summary=str(getattr(result, "summary", "") or "")[:1200],
                verification=report.to_dict() if report is not None else None,
                tokens=int(getattr(result, "tokens", 0) or 0),
                calls=int(getattr(result, "calls", 0) or 0),
                duration_ms=float(getattr(result, "duration_ms", 0.0) or 0.0),
                error=error[:900],
                output_fingerprint=_fingerprint_changed_files(self.repo_root, changed),
            )
            record.setdefault("cycles", []).append(cycle.to_dict())
            record["active_run_id"] = ""
            record["active_cycle_index"] = 0
            record["tokens"] = int(record.get("tokens", 0)) + cycle.tokens
            record["calls"] = int(record.get("calls", 0)) + cycle.calls
            record["updated_at"] = _now()

            if budget_tokens and cycle.tokens > budget_remaining:
                message = (
                    "Pipeline exceeded the remaining goal token budget "
                    f"({cycle.tokens}>{budget_remaining}); review usage accounting."
                )
                self._set_status(record, "blocked", accepted=False, error=message)
                self.store.save(record)
                self._emit_goal_event(
                    "goal.budget_exceeded", node=f"Цикл {cycle_index}",
                    status="Pipeline превысил оставшийся бюджет",
                    goal_id=goal_id, cycle_index=cycle_index,
                    cycle_tokens=cycle.tokens, budget_remaining=budget_remaining,
                )
                break

            self._emit_goal_event(
                "goal.cycle_finish", node=f"Цикл {cycle_index} из {max_cycles}",
                status=("Проверка пройдена" if report and report.passed is True else
                        "Нужна дополнительная доработка" if accepted and report and report.passed is False else
                        "Проверка недоступна" if report and report.passed is None else
                        "Изменения не применены"),
                goal_id=goal_id, cycle_index=cycle_index,
                cycle_limit=max_cycles, accepted=accepted,
                summary=(_safe_excerpt(report.feedback(), limit=700) if report
                         else _safe_excerpt(error, limit=700)),
            )

            previous_cycle = record["cycles"][-2] if len(record["cycles"]) >= 2 else {}
            if _repeated_failed_goal_output(cycle.to_dict(), previous_cycle):
                message = (
                    "Goal stopped after the same output and failure repeated; "
                    "review the saved workspace or change the task."
                )
                self._set_status(record, "needs_attention", accepted=False, error=message)
                self.store.save(record)
                self._emit_goal_event(
                    "goal.no_progress", node=f"Цикл {cycle_index}",
                    status="Остановлено: результат не изменился",
                    goal_id=goal_id, cycle_index=cycle_index,
                    output_fingerprint=cycle.output_fingerprint,
                )
                break

            if accepted and report is not None:
                if report.passed is True:
                    self._complete(record)
                    break
                if report.passed is None:
                    self._set_status(
                        record, "blocked", accepted=False,
                        error=report.feedback() or "one or more required verifiers are unavailable",
                    )
                    self.store.save(record)
                    break
                self._set_status(record, "running", accepted=False, error=report.feedback())
            else:
                if "declined by the user" in error.lower():
                    self._set_status(record, "needs_attention", accepted=False, error=error)
                    self.store.save(record)
                    break
                self._set_status(
                    record, "running", accepted=False,
                    error=error or "pipeline did not accept a change",
                )
            self.store.save(record)

        if record.get("status") == "running":
            last_feedback = self._last_feedback(record)
            detail = "cycle limit reached before verification passed"
            if last_feedback:
                detail += "; last cycle: " + last_feedback[:700]
            self._set_status(
                record, "needs_attention", accepted=False, error=detail,
            )
        self.store.save(record)
        final_status = str(record.get("status") or "unknown")
        self._emit_goal_event(
            "goal.finish", node="Goal",
            status={
                "completed": "Цель завершена",
                "blocked": "Цель заблокирована",
                "needs_attention": "Нужна проверка",
                "failed": "Цель завершилась с ошибкой",
                "cancelled": "Цель остановлена",
            }.get(final_status, "Цикл Goal завершён"),
            goal_id=goal_id, goal_status=final_status,
            cycle_count=len(record.get("cycles") or []),
            summary=_safe_excerpt(str(record.get("error") or ""), limit=700),
        )
        return self._result(record)

    def _emit_goal_event(self, event: str, **payload: Any) -> None:
        """Send lifecycle detail to the shared trace without risking the run."""
        trace = getattr(getattr(self.pipeline, "metrics", None), "trace", None)
        emit = getattr(trace, "event", None)
        if callable(emit):
            try:
                emit(event, **payload)
            except Exception:  # noqa: BLE001 - telemetry must never break a goal
                pass

    def _complete(self, record: dict[str, Any]) -> None:
        """Publish only a fully verified result, then mark the goal complete."""
        if self.workspace is None:
            self._set_status(record, "completed", accepted=True, error="")
            self.store.save(record)
            return

        changed_files: list[str] = []
        for cycle in record.get("cycles") or []:
            for path in cycle.get("changed_files") or []:
                if path not in changed_files:
                    changed_files.append(path)
        record["workspace_root"] = str(self.workspace.root)
        try:
            published = self.workspace.publish(changed_files)
        except Exception as exc:  # noqa: BLE001 - publication errors need an inspectable goal state
            self._set_status(
                record,
                "needs_attention",
                accepted=False,
                error=(
                    "verification passed, but publishing to the project failed: "
                    f"{type(exc).__name__}: {exc}"
                ),
            )
            self.store.save(record)
            return

        record["published_files"] = published
        record["workspace_root"] = ""
        self._set_status(record, "completed", accepted=True, error="")
        self.store.save(record)
        self.workspace.cleanup_project_copy()

    def _goal_exists(self, goal_id: str) -> bool:
        try:
            self.store.load(goal_id)
        except (ValueError, OSError, json.JSONDecodeError):
            return False
        return True

    def _runtime_preflight(self) -> tuple[str, ...]:
        """Provision verifiers before spending model calls on a Goal."""
        if not isinstance(self.verifier, WorkspaceVerifier):
            return ()
        from gigaforge.runtime_dependencies import (
            ensure_browser_runtime,
            ensure_goal_support_packages,
        )

        issues = list(ensure_goal_support_packages(auto_install=True, timeout_s=60))
        if self.verifier.require_browser:
            status = ensure_browser_runtime(auto_install=True, timeout_s=180)
            if not status.available:
                issues.append("browser runtime: " + status.detail)
        return tuple(issues)

    @staticmethod
    def _set_status(
        record: dict[str, Any],
        status: str,
        *,
        accepted: Optional[bool] = None,
        error: Optional[str] = None,
    ) -> None:
        record["status"] = status
        record["updated_at"] = _now()
        if accepted is not None:
            record["accepted"] = bool(accepted)
        if error is not None:
            record["error"] = str(error)[:1200]

    @staticmethod
    def _last_feedback(record: dict[str, Any]) -> str:
        cycles = record.get("cycles") or []
        if not cycles:
            return ""
        latest = cycles[-1]
        verification = latest.get("verification") or {}
        checks = verification.get("checks") or []
        failures = [
            f"- {check.get('name')}: {check.get('status')} — {check.get('summary', '')}"
            for check in checks
            if check.get("required") and check.get("status") == "failed"
        ]
        if failures:
            return "\n".join(failures)[:3500]
        return str(latest.get("error") or "")[:1800]

    @staticmethod
    def _cycle_prompt(goal: str, feedback: str) -> str:
        prompt = goal.strip()
        if feedback:
            prompt += (
                "\n\nLONG-LIVED GOAL MODE — CONTINUE THE EXISTING TASK. "
                "The previous cycle did not complete. Inspect the current workspace "
                "and fix the reported pipeline, patch, or execution failure before "
                "continuing. Do not repeat a rejected change unchanged. Preserve the "
                "original goal.\nPrevious cycle feedback:\n"
                f"{feedback[:3500]}"
            )
        return prompt

    def _cheap_verification(self, changed: Sequence[str], goal: str) -> str:
        """The deterministic part of verification, for a cycle that was rejected.

        Only checks that are a pure function of the file: Python syntax and the
        HTML structure audit.  No test suite (slow, and a red suite is the
        pipeline's own gate) and no browser (needs a binary that may be absent).

        Returns a short, actionable block, or ``""`` when there is nothing to
        say — an empty string is the honest answer for a rejected cycle whose
        files are structurally fine, and inventing a complaint would send the
        next cycle chasing a phantom.
        """
        try:
            from gigaforge.core.goal import WorkspaceVerifier

            verifier = WorkspaceVerifier(require_browser=False)
            root = Path(self.repo_root).resolve()
            paths = verifier._changed_paths(root, changed)
            lines: list[str] = []
            for path in paths:
                suffix = path.suffix.lower()
                if suffix == ".py" and path.is_file():
                    check = verifier._command_check(
                        "Python syntax",
                        [sys.executable, "-m", "py_compile", str(path)],
                        root,
                    )
                    if check.status == "failed":
                        lines.append(f"- {path.name}: {check.summary}")
                elif suffix in {".html", ".htm"} and path.is_file():
                    check = verifier._audit_html(root, path)
                    if check.status == "failed":
                        lines.append(f"- {path.name}: {check.summary}")
            if not lines:
                return ""
            return "Structural problems found in the rejected result:\n" + "\n".join(lines)
        except Exception as exc:  # noqa: BLE001 - a diagnostic must not break the loop
            return f"(cheap verification unavailable: {str(exc)[:160]})"

    @staticmethod
    def _pipeline_feedback(result: Any) -> str:
        if getattr(result, "error", ""):
            return str(result.error)[:1500]
        nodes = getattr(result, "nodes", []) or []
        for node in reversed(nodes):
            detail = node.get("reason") or node.get("error")
            if detail:
                return str(detail)[:1500]
        return "the pipeline did not produce an accepted change"

    @staticmethod
    def _result(record: dict[str, Any]) -> GoalResult:
        cycles = [GoalCycle(**cycle) for cycle in record.get("cycles", [])]
        return GoalResult(
            goal_id=str(record.get("goal_id") or ""),
            goal=str(record.get("goal") or ""),
            status=str(record.get("status") or "failed"),
            accepted=bool(record.get("accepted")),
            cycles=cycles,
            tokens=int(record.get("tokens", 0) or 0),
            budget_tokens=int(record.get("budget_tokens", 0) or 0),
            calls=int(record.get("calls", 0) or 0),
            started_at=str(record.get("started_at") or ""),
            updated_at=str(record.get("updated_at") or ""),
            error=str(record.get("error") or ""),
            workspace_root=str(record.get("workspace_root") or ""),
            published_files=list(record.get("published_files") or []),
        )


def _is_transient_provider_error(error: str) -> bool:
    """True when the failure is the provider throttling us, not the work.

    A 429 (or a 5xx from the gateway) says nothing about the artifact.  Treating
    it as a failed cycle made a goal burn its whole cycle budget in under a
    minute and then report "cycle limit reached before verification passed" —
    a verdict on the work that was really a verdict on the quota.
    """
    low = (error or "").lower()
    return any(
        marker in low
        for marker in (
            "gf-rate-429",
            "429",
            "too many requests",
            "rate limit",
            "ratelimit",
            "gf-upstream-5",
            "502",
            "503",
            "504",
            "service unavailable",
            "temporarily unavailable",
        )
    )


def _fingerprint_changed_files(root: Path, changed_files: Sequence[str]) -> str:
    """Hash the current contents of the changed paths without following escapes.

    Goal cycles share one workspace.  Comparing only the model summary or diff
    text misses cases where different diffs produce the same tree; hashing the
    resulting files gives the no-progress guard a stable, content-based signal.
    """
    root = Path(root).resolve()
    digest = hashlib.sha256()
    paths = sorted(
        {str(raw).strip().replace("\\", "/") for raw in changed_files if str(raw).strip()}
    )
    for rel in paths:
        digest.update(rel.encode("utf-8", errors="replace"))
        digest.update(b"\0")
        relative = Path(rel)
        if relative.is_absolute() or ".." in relative.parts:
            digest.update(b"<unsafe-path>\0")
            continue
        candidate = root / relative
        try:
            resolved = candidate.resolve()
            resolved.relative_to(root)
        except (OSError, ValueError):
            digest.update(b"<outside-workspace>\0")
            continue
        if candidate.is_symlink():
            digest.update(b"<symlink>\0")
            continue
        if not resolved.is_file():
            digest.update(b"<missing>\0")
            continue
        try:
            with resolved.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
        except OSError:
            digest.update(b"<unreadable>\0")
        digest.update(b"\0")
    return digest.hexdigest()


def _remaining_goal_budget(budget_tokens: int, tokens_used: int) -> int:
    """Return this goal's remaining allowance, keeping zero as "unlimited"."""
    budget_tokens = max(0, int(budget_tokens))
    if budget_tokens == 0:
        return 0
    return max(0, budget_tokens - max(0, int(tokens_used)))


def _repeated_failed_goal_output(
    current: Mapping[str, Any], previous: Mapping[str, Any]
) -> bool:
    """Whether two adjacent cycles produced the same failed workspace result.

    An unavailable verifier is not a failed result: callers should block or
    resume after provisioning it, rather than treating the missing tool as
    evidence that another model cycle cannot help.
    """
    def failed(cycle: Mapping[str, Any]) -> bool:
        if not bool(cycle.get("pipeline_accepted")):
            return True
        verification = cycle.get("verification")
        return isinstance(verification, dict) and verification.get("passed") is False

    fingerprint = str(current.get("output_fingerprint") or "")
    return bool(
        failed(current)
        and failed(previous)
        and fingerprint
        and fingerprint == previous.get("output_fingerprint")
        and bool(current.get("pipeline_accepted"))
        == bool(previous.get("pipeline_accepted"))
        and current.get("verification") == previous.get("verification")
        and str(current.get("error") or "") == str(previous.get("error") or "")
    )


def _wait_or_cancel(cancel: Any, delay: float) -> bool:
    """Sleep for *delay* seconds, returning True if the run was cancelled.

    ``CancellationToken`` has no ``wait``; polling it in short slices keeps a
    cancel responsive without adding a method to the token.
    """
    remaining = max(0.0, float(delay))
    while remaining > 0:
        if getattr(cancel, "cancelled", False):
            return True
        step = min(0.5, remaining)
        time.sleep(step)
        remaining -= step
    return bool(getattr(cancel, "cancelled", False))


def _merge_conversation_context(
    existing: str,
    conversation_context: str = "",
    additional_context: str = "",
    *,
    limit: int = 8000,
) -> str:
    """Keep earlier context and explicit clarifications bounded across resumes."""
    parts = [str(existing or "").strip()]
    for label, value in (
        ("Earlier conversation", conversation_context),
        ("User clarification", additional_context),
    ):
        text = str(value or "").strip()
        if text and not any(text == part or text in part for part in parts if part):
            parts.append(f"{label}:\n{text}")
    combined = "\n\n".join(part for part in parts if part)
    limit = max(0, int(limit))
    return combined[-limit:] if limit else ""


def _safe_excerpt(value: str, *, limit: int = 900) -> str:
    text = str(value or "").replace("\x00", " ").strip()
    text = _SENSITIVE_OUTPUT.sub(r"\1=<redacted>", text)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return " | ".join(lines[-8:])[:limit]


def _unique(values: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(value.strip() for value in values if value.strip()))


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
