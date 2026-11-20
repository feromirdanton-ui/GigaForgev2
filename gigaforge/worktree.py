"""Git worktree helpers — isolate pipeline tasks in their own checkout.

The pipeline gives each task its own git worktree under ``<repo>/.gigaforge/worktrees/``
so concurrent tasks (or fix-loop retries) never trip over each other. A worktree
shares the parent repo's object database but has its own HEAD + working files,
which is exactly what we want: cheap to create, isolated to operate in, and
``git commit`` inside the worktree is atomic per logical change.

All git invocations go through ``subprocess.run`` with ``shell=False``
(never the shell-enabled form). The caller's git identity is forced via
``-c user.email=... -c user.name=...`` so the helper works even when the
sandbox env has no global git identity.
"""
from __future__ import annotations

import logging
import os
import secrets
import subprocess
import time
from pathlib import Path

log = logging.getLogger(__name__)

__all__ = [
    "make_worktree",
    "remove_worktree",
    "git_sha",
    "commit_all",
    "current_branch",
    "is_clean",
    "GitError",
]

# Forced identity so the helper works in sandbox envs without global git config.
# Pass via ``-c key=value`` to ``git`` itself — never relies on a configured
# global identity. LOW-01: a single flat tuple literal (no grouping parens).
_GIT_IDENTITY = (
    "-c", "user.email=gigaforge@local",
    "-c", "user.name=gigaforge-bot",
)

# Force C locale so git's stderr/stdout is ALWAYS English. MED-07: the retry
# loop in make_worktree keys off substrings ("already exists", "used by
# worktree") which would silently break under ru_RU/de_DE/etc. locales.
_GIT_ENV = {**os.environ, "LC_ALL": "C", "LANG": "C"}


class GitError(RuntimeError):
    """Raised when a git invocation returns non-zero."""


# --- internals ---------------------------------------------------------------


def _run(
    args: list[str],
    *,
    cwd: Path,
    check: bool = True,
    capture: bool = True,
    timeout: float = 60.0,
) -> subprocess.CompletedProcess[str]:
    """Invoke ``git <args>`` in ``cwd`` with forced identity.

    Always uses ``shell=False``. Never raises on non-zero rc when
    ``check=False``; instead the caller inspects ``CompletedProcess.returncode``.
    """
    cmd = ["git", *_GIT_IDENTITY, *args]
    log.debug("git cwd=%s args=%s", cwd, args)
    started = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            env=_GIT_ENV,
            capture_output=capture,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise GitError("git executable not found on PATH") from exc
    elapsed = time.monotonic() - started
    if check and proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        raise GitError(
            f"git {' '.join(args)!r} failed (rc={proc.returncode}) "
            f"in {cwd}: {stderr[:300]}"
        )
    log.debug(
        "git %s rc=%d elapsed=%.2fs",
        args[0] if args else "?",
        proc.returncode,
        elapsed,
    )
    return proc


def _ensure_repo(repo_root: Path) -> None:
    """Verify ``repo_root`` is a git repo (and git is available)."""
    p = repo_root / ".git"
    if not p.exists():
        raise GitError(f"not a git repo (no .git in {repo_root})")


# --- public API --------------------------------------------------------------


def make_worktree(repo_root: Path, branch_name: str | None = None) -> Path:
    """Create a new git worktree under ``<repo_root>/.gigaforge/worktrees/``.

    Returns the absolute path to the new worktree. The worktree is checked out
    on a fresh branch (or detached HEAD if ``branch_name`` is None) starting
    from the repo's current HEAD.

    The parent dir ``<repo_root>/.gigaforge/worktrees/`` is created if missing.
    """
    repo_root = Path(repo_root).resolve()
    _ensure_repo(repo_root)
    wt_root = repo_root / ".gigaforge" / "worktrees"
    wt_root.mkdir(parents=True, exist_ok=True)

    if branch_name is None:
        branch_name = f"gigaforge-wt-{secrets.token_hex(4)}-{int(time.time())}"

    # Sanitize branch name (git rejects slashes/spaces/etc.).
    safe_branch = branch_name.replace("/", "-").replace(" ", "-")
    wt_path = wt_root / safe_branch

    # Use `git worktree add -b <branch> <path>` to create a NEW branch from HEAD.
    # If the branch name collides, append a short random suffix.
    attempt = 0
    while True:
        try:
            _run(
                ["worktree", "add", "-b", safe_branch, str(wt_path)],
                cwd=repo_root,
            )
            break
        except GitError as exc:
            if "already exists" not in str(exc) and "used by worktree" not in str(exc):
                raise
            attempt += 1
            if attempt > 20:
                raise
            safe_branch = f"{branch_name}-{secrets.token_hex(2)}"
            wt_path = wt_root / safe_branch

    # git worktree add prints the path + commit to stdout; the path we asked
    # for is what we return.
    return wt_path.resolve()


def remove_worktree(worktree: Path) -> None:
    """Remove a worktree + prune its entry from the parent repo.

    Best-effort: if the parent repo / worktree no longer exists, do nothing.
    Safe to call multiple times.

    Idempotency contract: after this call returns, the worktree directory
    MUST be gone (otherwise HIGH-06 test fails — `.gigaforge/worktrees/`
    accumulates entries across runs). We belt-and-braces this by also
    rmtree'ing the directory if git left it behind. Windows in particular
    often leaves the directory alive even after ``git worktree remove
    --force`` succeeds; the rmtree fallback is the canonical fix.
    """
    worktree = Path(worktree)
    if not worktree.exists() and not worktree.is_symlink():
        return
    # Find the parent repo (walk up for .git).
    repo_root = _find_repo_root(worktree)
    if repo_root is None:
        # Not in a repo — nothing to remove from git's POV. Best-effort rmtree.
        log.warning("worktree %s not in a git repo; rmtree anyway", worktree)
        _rmtree_best_effort(worktree)
        return
    try:
        _run(
            ["worktree", "remove", "--force", str(worktree)],
            cwd=repo_root,
            check=False,
        )
    except GitError as exc:  # pragma: no cover - defensive
        log.warning("git worktree remove failed: %s", exc)
    # Always prune afterwards (cleans up stale admin files).
    try:
        _run(["worktree", "prune"], cwd=repo_root, check=False, timeout=15.0)
    except (GitError, subprocess.TimeoutExpired):  # pragma: no cover - defensive
        pass
    # HIGH-06 belt-and-braces: even on ``rc=0`` the directory may linger
    # (Windows file-handle race, named pipe from a still-running shell, etc.).
    # rmtree it if it still exists. Idempotent — second call is a no-op.
    if worktree.exists():
        _rmtree_best_effort(worktree)


def _rmtree_best_effort(path: Path) -> None:
    """Delete *path* (and all contents) — never raises. Used by
    :func:`remove_worktree` to guarantee the dir is gone even when
    Windows leaves it behind after ``git worktree remove --force``."""
    import shutil
    try:
        shutil.rmtree(path, ignore_errors=True)
    except Exception as exc:  # noqa: BLE001 — last-resort best-effort
        log.warning("rmtree failed for %s: %s", path, exc)


def git_sha(repo_root: Path) -> str:
    """Return the short SHA of HEAD in ``repo_root`` (or raise GitError)."""
    repo_root = Path(repo_root).resolve()
    _ensure_repo(repo_root)
    proc = _run(["rev-parse", "--short", "HEAD"], cwd=repo_root)
    return (proc.stdout or "").strip()


def current_branch(repo_root: Path) -> str | None:
    """Return the current branch name, or None if HEAD is detached."""
    repo_root = Path(repo_root).resolve()
    _ensure_repo(repo_root)
    proc = _run(
        ["rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_root, check=False
    )
    name = (proc.stdout or "").strip()
    if name == "HEAD" or not name:
        return None
    return name


def is_clean(repo_root: Path) -> bool:
    """True iff ``git status --porcelain`` produces no output."""
    repo_root = Path(repo_root).resolve()
    _ensure_repo(repo_root)
    proc = _run(
        ["status", "--porcelain"], cwd=repo_root, check=False
    )
    return (proc.stdout or "").strip() == ""


def commit_all(worktree: Path, message: str) -> str:
    """Stage everything in ``worktree`` and commit. Returns the new short SHA.

    Idempotent: if there is nothing to commit (the working tree is clean),
    returns the current HEAD sha without raising. The commit is made with
    forced gigaforge identity, so the sandbox env's lack of global git config
    never breaks the commit.
    """
    worktree = Path(worktree).resolve()
    _ensure_repo(worktree)

    # `git add -A` — stage everything (created, modified, deleted).
    _run(["add", "-A"], cwd=worktree)

    # Detect "nothing to commit" via `git diff --cached --quiet`.
    # rc=0 means no staged changes (clean); rc=1 means there are staged changes.
    diff_proc = _run(
        ["diff", "--cached", "--quiet"], cwd=worktree, check=False
    )
    if diff_proc.returncode == 0:
        # Nothing staged -> no-op. Return current HEAD sha.
        return git_sha(worktree)

    # Commit with the given message.
    _run(
        ["commit", "-m", message],
        cwd=worktree,
        timeout=30.0,
    )
    return git_sha(worktree)


# --- helpers -----------------------------------------------------------------


def _find_repo_root(start: Path) -> Path | None:
    """Walk up from ``start`` to find a dir containing ``.git``."""
    p = Path(start).resolve()
    while True:
        if (p / ".git").exists():
            return p
        if p.parent == p:
            return None
        p = p.parent
