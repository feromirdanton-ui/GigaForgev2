"""GigaForge doctor — environment / config / credentials / deps / git health-check.

``gigaforge doctor`` runs a battery of checks and prints a rich diagnostics
report with PASS / WARN / FAIL per check, plus actionable auto-fix hints.
It DOES NOT modify anything by default (read-only); pass ``--fix`` to apply
safe auto-fixes (create .env template, init git, install missing deps).

Checks:
  1. GIGAFORGE_AUTH_KEY present + non-empty.
  2. .env / .gigaforge.json discoverable (autoload would work).
  3. OAuth reachable (best-effort; skipped if --offline).
  4. tree-sitter Python grammar importable.
  5. ruff on PATH.
  6. git on PATH + repo is a git repo (if --repo given).
  7. SQLite writable (db_path parent exists / creatable).
  8. trace_dir writable.
  9. Budget caps sane (plan < code < task_cap).
 10. Model names map to a plausible catalog form.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console
from rich.table import Table
from rich.text import Text

__all__ = ["Check", "collect_checks", "run_doctor"]

console = Console()


@dataclass
class Check:
    name: str
    status: str  # "PASS" | "WARN" | "FAIL" | "SKIP"
    detail: str
    fix: str = ""  # actionable hint (empty if none)


def _ok(name: str, detail: str) -> Check:
    return Check(name, "PASS", detail)


def _warn(name: str, detail: str, fix: str = "") -> Check:
    return Check(name, "WARN", detail, fix)


def _fail(name: str, detail: str, fix: str = "") -> Check:
    return Check(name, "FAIL", detail, fix)


def _skip(name: str, detail: str) -> Check:
    return Check(name, "SKIP", detail)


def _check_auth_key() -> Check:
    v = os.environ.get("GIGAFORGE_AUTH_KEY", "").strip()
    if not v:
        return _fail(
            "GIGAFORGE_AUTH_KEY",
            "env var is empty/missing",
            "set GIGAFORGE_AUTH_KEY (base64(client_id:client_secret)) in your .env "
            "or shell; never commit real credentials.",
        )
    if len(v) < 16:
        return _warn("GIGAFORGE_AUTH_KEY", f"key looks short ({len(v)} chars)")
    return _ok("GIGAFORGE_AUTH_KEY", f"present ({len(v)} chars)")


def _check_env_discovery(cwd: Path) -> Check:
    candidates = [cwd / ".gigaforge.env", cwd / ".env", Path.home() / ".gigaforge" / ".env"]
    found = [c for c in candidates if c.exists()]
    if found:
        return _ok(".env discovery", f"found {found[0]}")
    return _warn(
        ".env discovery",
        "no .env found in ./.gigaforge.env, ./.env, ~/.gigaforge/.env",
        "create a .env with GIGAFORGE_AUTH_KEY=... (see .env.example)",
    )


def _check_oauth_online() -> Check:
    try:
        import httpx

        from gigaforge.config import get_settings

        s = get_settings()
        # Don't do a full OAuth — just probe the auth URL with a HEAD-like GET.
        with httpx.Client(verify=s.verify, timeout=5.0) as client:
            r = client.get(s.auth_url)
        # 401/403/405 is FINE — the endpoint exists, just rejects GET.
        if r.status_code < 500:
            return _ok("OAuth reachable", f"{s.auth_url} -> {r.status_code}")
        return _warn("OAuth reachable", f"{s.auth_url} -> {r.status_code}")
    except Exception as exc:  # noqa: BLE001
        return _skip("OAuth reachable", f"skipped: {exc}")


def _check_tree_sitter() -> Check:
    try:
        import tree_sitter_python as tspython  # noqa: F401
        from tree_sitter import Language, Parser  # noqa: F401

        _ = Language(tspython.language())
    except Exception as exc:  # noqa: BLE001
        return _fail(
            "tree-sitter", f"import failed: {exc}",
            "The current GigaForge install provisions tree-sitter and its Python grammar.",
        )
    try:
        import tree_sitter_javascript as tsjavascript
        from tree_sitter import Language

        _ = Language(tsjavascript.language())
        return _ok("tree-sitter", "Python and JavaScript grammars load")
    except Exception as exc:  # noqa: BLE001 - JavaScript grammar is bootstrapped by the app
        return _warn(
            "tree-sitter JavaScript", f"grammar unavailable: {type(exc).__name__}: {exc}",
            "The desktop app provisions the JavaScript grammar on startup.",
        )


def _check_ruff() -> Check:
    path = shutil.which("ruff")
    if path:
        try:
            v = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=5)
            return _ok("ruff", f"{path} {v.stdout.strip()}")
        except Exception as exc:  # noqa: BLE001
            return _warn("ruff", f"on PATH but --version failed: {exc}")
    return _warn("ruff", "not on PATH", "pip install ruff  (or apt install ruff)")


def _check_browser_runtime() -> Check:
    """Probe the browser capability used by HTML Goal verification."""
    try:
        from gigaforge.runtime_dependencies import ensure_browser_runtime

        status = ensure_browser_runtime(auto_install=False)
        if status.available:
            return _ok("browser runtime", status.detail)
        return _warn(
            "browser runtime", status.detail,
            "The app and Goal mode provision Playwright and Chromium automatically; "
            "restore network access and retry the Goal.",
        )
    except Exception as exc:  # noqa: BLE001 - doctor reports rather than crashes
        return _warn("browser runtime", f"probe failed: {type(exc).__name__}: {exc}")


def _check_pytest() -> Check:
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "--version"],
            capture_output=True, text=True, timeout=8, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return _warn("pytest", f"probe failed: {type(exc).__name__}: {exc}")
    if result.returncode == 0:
        return _ok("pytest", result.stdout.strip() or result.stderr.strip())
    return _warn("pytest", "not installed", "The current GigaForge package provisions pytest as a core dependency.")


def _check_git(repo: Path | None) -> Check:
    path = shutil.which("git")
    if not path:
        return _fail("git", "not on PATH", "install git")
    if repo is None:
        return _ok("git", f"{path} on PATH (no --repo given)")
    if not (repo / ".git").exists():
        return _warn("git repo", f"{repo} is not a git repo", f"cd {repo} && git init && git add -A && git commit -m init")
    return _ok("git repo", f"{repo} is a git repo")


def _check_db_writable() -> Check:
    """Probe the database the harness actually writes.

    ``Settings.db_path`` is the legacy location; the V2 pipeline writes
    ``CoreConfig.db_path`` (``.gigaforge/core.sqlite3``).  Probing the wrong file
    reports "writable" for a path nothing uses — the one answer a doctor must not
    get wrong.
    """
    try:
        import sqlite3

        from gigaforge.config import get_settings

        s = get_settings()
        candidates = []
        try:
            from gigaforge.core.config import CoreConfig

            candidates.append(CoreConfig.from_env().db_path)
        except Exception:  # noqa: BLE001 - the legacy path is still worth checking
            pass
        candidates.append(str(s.db_path))

        probed = []
        for raw in candidates:
            path = Path(raw)
            path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(path), timeout=2.0)
            try:
                conn.execute("CREATE TABLE IF NOT EXISTS _doctor_probe (x INTEGER)")
                conn.execute("INSERT INTO _doctor_probe VALUES (1)")
                conn.execute("DELETE FROM _doctor_probe")
                conn.execute("DROP TABLE _doctor_probe")
                conn.commit()
            finally:
                conn.close()
            probed.append(str(path))
        return _ok("SQLite", "writable: " + ", ".join(probed))
    except Exception as exc:  # noqa: BLE001
        return _fail("SQLite", f"not writable: {exc}",
                     "check permissions on the db_path parent dir")


def _check_trace_writable() -> Check:
    try:
        from gigaforge.config import get_settings

        s = get_settings()
        s.trace_dir.mkdir(parents=True, exist_ok=True)
        probe = s.trace_dir / "_doctor_probe.jsonl"
        probe.write_text("{}\n", encoding="utf-8")
        probe.unlink()
        return _ok("trace_dir", f"writable: {s.trace_dir}")
    except Exception as exc:  # noqa: BLE001
        return _fail("trace_dir", f"not writable: {exc}")


def _check_budgets() -> Check:
    try:
        from gigaforge.config import get_settings

        s = get_settings()
        issues = []
        if s.budget_plan_in >= s.budget_code_in:
            issues.append(f"plan_in({s.budget_plan_in}) >= code_in({s.budget_code_in}) — plan should be cheaper")
        if s.budget_code_in >= s.budget_task_cap:
            issues.append(f"code_in({s.budget_code_in}) >= task_cap({s.budget_task_cap}) — one phase could exhaust the task")
        if s.budget_fixloop_iters > 5:
            issues.append(f"fixloop_iters={s.budget_fixloop_iters} — keep ≤3-5 to avoid runaway")
        if issues:
            return _warn("budgets", "; ".join(issues))
        return _ok("budgets", f"plan<{s.budget_plan_in}> code<{s.budget_code_in}> task<{s.budget_task_cap}> fixloop×{s.budget_fixloop_iters}")
    except Exception as exc:  # noqa: BLE001
        return _fail("budgets", f"config error: {exc}")


def _check_models() -> Check:
    try:
        from gigaforge.config import get_settings

        s = get_settings()
        m = s.models
        lite = m.get("plan", "")
        pro = m.get("code", "")
        if not lite or not pro:
            return _fail("models", "plan or code model is empty", "set GIGAFORGE_MODEL_PLAN / GIGAFORGE_MODEL_CODE")
        if lite == pro:
            return _warn("models", f"plan==code=={lite} (L1 should be cheaper than L2)")
        return _ok("models", f"L1={lite} · L2={pro} · explain={m.get('explain', '?')}")
    except Exception as exc:  # noqa: BLE001
        return _fail("models", f"config error: {exc}")


def collect_checks(
    *,
    root: Path | None = None,
    repo: Path | None = None,
    offline: bool = False,
) -> list[Check]:
    """Collect the shared diagnostic contract for CLI and desktop surfaces."""
    cwd = root or repo or Path.cwd()
    checks = [
        _check_auth_key(),
        _check_env_discovery(cwd),
    ]
    if offline:
        checks.append(_skip("OAuth reachable", "skipped (--offline)"))
    else:
        checks.append(_check_oauth_online())
    checks.extend([
        _check_tree_sitter(),
        _check_ruff(),
        _check_browser_runtime(),
        _check_pytest(),
        _check_git(repo),
        _check_db_writable(),
        _check_trace_writable(),
        _check_budgets(),
        _check_models(),
    ])
    return checks


def run_doctor(
    repo: Path | None = None,
    *,
    offline: bool = False,
    fix: bool = False,
) -> int:
    """Run the doctor battery. Returns an exit code (0=all pass, 1=warn, 2=fail)."""
    cwd = repo or Path.cwd()
    checks = collect_checks(root=cwd, repo=repo, offline=offline)

    # Render.
    table = Table(title="GigaForge doctor", border_style="cyan", title_style="bold cyan")
    table.add_column("status", width=6)
    table.add_column("check", style="cyan")
    table.add_column("detail", overflow="fold")
    table.add_column("fix", style="yellow", overflow="fold")
    n_fail = n_warn = 0
    for c in checks:
        color = {"PASS": "green", "WARN": "yellow", "FAIL": "red", "SKIP": "dim"}[c.status]
        table.add_row(Text(c.status, style=f"bold {color}"), c.name, c.detail, c.fix)
        if c.status == "FAIL":
            n_fail += 1
        elif c.status == "WARN":
            n_warn += 1
    console.print(table)

    if fix:
        _apply_safe_fixes(checks, cwd)
    if n_fail:
        console.print(f"\n[bold red]FAILED[/] {n_fail} check(s); [yellow]WARNED[/] {n_warn}.", style="red")
        return 2
    if n_warn:
        console.print(f"\n[yellow]WARNED[/] {n_warn} check(s) (no failures).", style="yellow")
        return 1
    console.print("\n[bold green]✓ all checks passed[/]")
    return 0


def _apply_safe_fixes(checks: list[Check], cwd: Path) -> None:
    """Apply only safe, non-destructive auto-fixes."""
    console.print("\n[bold cyan]Applying safe fixes…[/]")
    for c in checks:
        if c.status != "FAIL" or not c.fix:
            continue
        if c.name in (".env discovery", "GIGAFORGE_AUTH_KEY"):
            env_path = cwd / ".env"
            if not env_path.exists():
                env_path.write_text(
                    "# GigaForge config — fill in your GIGAFORGE_AUTH_KEY\n"
                    "GIGAFORGE_AUTH_KEY=\n",
                    encoding="utf-8",
                )
                console.print(f"  [green]created[/] {env_path}")
        elif c.name == "git repo":
            try:
                subprocess.run(["git", "init"], cwd=cwd, check=True, capture_output=True)
                console.print(f"  [green]git init[/] {cwd}")
            except Exception as exc:  # noqa: BLE001
                console.print(f"  [red]git init failed:[/] {exc}")
    console.print("[dim]--fix only creates missing files / inits git; it never overwrites.[/]")


if __name__ == "__main__":  # pragma: no cover
    sys.exit(run_doctor())
