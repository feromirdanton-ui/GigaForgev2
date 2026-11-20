"""Isolated project copies and guarded publication for long-lived goals.

Goal work happens in a persistent private copy of the selected project. The copy
is verified there, then only the reported files are published back with an
optimistic-concurrency check and atomic replacement. A failed or blocked goal
leaves its workspace intact so it can be resumed or inspected.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Optional, Sequence

__all__ = ["GoalWorkspace", "WorkspaceConflict", "WorkspacePublishError"]

_EXCLUDED = {
    ".git", ".hg", ".svn", ".gigaforge", ".venv", "venv", "node_modules",
    "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache", ".hypothesis",
    ".tox", ".nox", "build", "dist", "htmlcov", ".idea", ".vscode",
}
_MANIFEST_VERSION = 1


class WorkspacePublishError(RuntimeError):
    """A verified workspace could not be safely published."""


class WorkspaceConflict(WorkspacePublishError):
    """A destination file changed after the isolated workspace was created."""


class GoalWorkspace:
    """A resumable isolated workspace backed by one selected destination."""

    def __init__(self, destination: Path, goal_id: str) -> None:
        self.destination = Path(destination).expanduser().resolve()
        if not self.destination.is_dir():
            raise ValueError(f"project folder does not exist: {self.destination}")
        if (
            not goal_id
            or len(goal_id) > 80
            or not goal_id[0].isalnum()
            or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for char in goal_id)
        ):
            raise ValueError("goal_id contains unsupported path characters")
        self.state_dir = self.destination / ".gigaforge" / "workspaces" / goal_id
        self.root = self.state_dir / "project"
        self.manifest_path = self.state_dir / "manifest.json"
        self._manifest: dict[str, Any] = {}

    def prepare(self, *, resume: bool = False) -> Path:
        """Create the private copy, or reopen it for a resumed goal."""
        if resume and self.manifest_path.is_file():
            manifest = self._read_manifest()
            if manifest.get("destination") != str(self.destination):
                raise WorkspacePublishError("saved goal workspace belongs to another project")
            if not self.root.is_dir():
                if not manifest.get("published_at"):
                    raise WorkspacePublishError(
                        "saved isolated workspace is missing; start a new goal to create another"
                    )
                # Recover the narrow crash window after publication but before the
                # GoalStore marked completion. Rebuild from the now-published
                # destination so verification can be replayed safely.
                shutil.copytree(
                    self.destination,
                    self.root,
                    symlinks=True,
                    ignore=self._ignore_project_data,
                )
                manifest["base_files"] = self._snapshot(self.destination)
                manifest["workspace"] = str(self.root)
                self._manifest = manifest
                self._write_manifest()
                return self.root
            self._manifest = manifest
            return self.root

        if self.state_dir.exists():
            raise WorkspacePublishError(
                f"workspace state already exists for {self.manifest_path}; resume that goal"
            )
        self.state_dir.mkdir(parents=True, exist_ok=False)
        try:
            shutil.copytree(
                self.destination,
                self.root,
                symlinks=True,
                ignore=self._ignore_project_data,
            )
            self._manifest = {
                "schema_version": _MANIFEST_VERSION,
                "goal_id": self.state_dir.name,
                "destination": str(self.destination),
                "workspace": str(self.root),
                "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "base_files": self._snapshot(self.destination),
                "published_files": [],
            }
            self._write_manifest()
        except Exception:
            shutil.rmtree(self.state_dir, ignore_errors=True)
            raise
        return self.root

    def publish(self, changed_files: Sequence[str]) -> list[str]:
        """Publish verified files with conflict checks and atomic file replacement."""
        if not self._manifest:
            self._manifest = self._read_manifest()
        base_files = self._manifest.get("base_files")
        if not isinstance(base_files, dict):
            raise WorkspacePublishError("workspace manifest has no valid destination snapshot")

        rel_paths = self._normalize_changed_paths(changed_files)
        writes: list[tuple[str, Path, Path, Optional[bytes], int]] = []
        conflicts: list[str] = []
        for rel in rel_paths:
            source = self._safe_target(self.root, rel)
            target = self._safe_target(self.destination, rel)
            staged_bytes: Optional[bytes]
            staged_mode = 0o644
            if source.exists():
                if not source.is_file() or source.is_symlink():
                    raise WorkspacePublishError(f"workspace output is not a regular file: {rel}")
                staged_bytes = source.read_bytes()
                staged_mode = source.stat().st_mode & 0o777
            else:
                staged_bytes = None

            baseline = base_files.get(rel)
            current = _path_digest(target)
            if current != baseline:
                conflicts.append(rel)
                continue
            if staged_bytes is not None and _bytes_digest(staged_bytes) == baseline:
                continue
            if staged_bytes is None and baseline is None:
                continue
            writes.append((rel, target, source, staged_bytes, staged_mode))

        if conflicts:
            display = ", ".join(conflicts[:12])
            if len(conflicts) > 12:
                display += f", and {len(conflicts) - 12} more"
            raise WorkspaceConflict(
                "destination changed while the goal was running; resolve these files "
                f"before publishing: {display}"
            )

        backups: dict[Path, tuple[bool, bytes, int]] = {}
        created_dirs: list[Path] = []
        try:
            for _rel, target, _source, _content, _mode in writes:
                backups[target] = _capture_target(target)
            for _rel, target, _source, content, mode in writes:
                missing = []
                parent = target.parent
                while not parent.exists() and parent != self.destination:
                    missing.append(parent)
                    parent = parent.parent
                target.parent.mkdir(parents=True, exist_ok=True)
                created_dirs.extend(reversed(missing))
                if content is None:
                    target.unlink(missing_ok=True)
                else:
                    _atomic_write(target, content, mode)
        except Exception as exc:
            rollback_errors = _rollback(backups)
            for directory in reversed(created_dirs):
                try:
                    directory.rmdir()
                except OSError:
                    pass
            detail = f"{type(exc).__name__}: {exc}"
            if rollback_errors:
                detail += "; rollback errors: " + "; ".join(rollback_errors[:4])
            raise WorkspacePublishError(f"publishing verified files failed: {detail}") from exc

        published = [rel for rel, *_ in writes]
        self._manifest["published_files"] = published
        self._manifest["published_at"] = datetime.now(UTC).isoformat(timespec="seconds")
        try:
            self._write_manifest()
        except Exception as exc:
            rollback_errors = _rollback(backups)
            detail = f"{type(exc).__name__}: {exc}"
            if rollback_errors:
                detail += "; rollback errors: " + "; ".join(rollback_errors[:4])
            raise WorkspacePublishError(f"could not record publication: {detail}") from exc
        return published

    def cleanup_project_copy(self) -> None:
        """Release the full project copy after successful publication."""
        shutil.rmtree(self.root, ignore_errors=True)

    def _read_manifest(self) -> dict[str, Any]:
        try:
            manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise WorkspacePublishError(f"cannot read saved workspace manifest: {exc}") from exc
        if not isinstance(manifest, dict) or manifest.get("schema_version") != _MANIFEST_VERSION:
            raise WorkspacePublishError("saved workspace manifest has an unsupported format")
        return manifest

    def _write_manifest(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.manifest_path.with_name(self.manifest_path.name + ".tmp")
        try:
            temporary.write_text(
                json.dumps(self._manifest, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            os.replace(temporary, self.manifest_path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _ignore_project_data(directory: str, names: list[str]) -> set[str]:
        del directory
        ignored = set(names) & _EXCLUDED
        return ignored | {name for name in names if _is_secret_env_file(name)}

    @staticmethod
    def _snapshot(root: Path) -> dict[str, Optional[str]]:
        files: dict[str, Optional[str]] = {}
        for current, dirs, names in os.walk(root, topdown=True, followlinks=False):
            dirs[:] = sorted(name for name in dirs if name not in _EXCLUDED)
            base = Path(current)
            for name in names:
                if _is_secret_env_file(name):
                    continue
                path = base / name
                rel = path.relative_to(root).as_posix()
                files[rel] = _path_digest(path)
        return files

    @staticmethod
    def _normalize_changed_paths(paths: Sequence[str]) -> list[str]:
        normalized: dict[str, str] = {}
        for raw in paths:
            value = str(raw or "").strip().replace("\\", "/")
            candidate = PurePosixPath(value)
            if not value or candidate.is_absolute() or any(part in {"", ".", ".."} for part in candidate.parts):
                raise WorkspacePublishError(f"unsafe changed file path: {raw!r}")
            if candidate.parts and ":" in candidate.parts[0]:
                raise WorkspacePublishError(f"unsafe changed file path: {raw!r}")
            rel = candidate.as_posix()
            if any(part in _EXCLUDED for part in candidate.parts) or any(
                _is_secret_env_file(part) for part in candidate.parts
            ):
                raise WorkspacePublishError(f"refusing to publish protected project data: {rel}")
            normalized.setdefault(rel.casefold(), rel)
        return list(normalized.values())

    @staticmethod
    def _safe_target(root: Path, relative: str) -> Path:
        candidate = root.joinpath(*PurePosixPath(relative).parts)
        resolved_root = root.resolve()
        resolved = candidate.resolve(strict=False)
        try:
            resolved.relative_to(resolved_root)
        except ValueError as exc:
            raise WorkspacePublishError(f"file path escapes project root: {relative}") from exc
        current = root
        for part in PurePosixPath(relative).parts:
            current = current / part
            if current.is_symlink():
                raise WorkspacePublishError(f"refusing to publish through a symlink: {relative}")
        return candidate


def _bytes_digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _is_secret_env_file(name: str) -> bool:
    lowered = name.lower()
    return lowered == ".env" or (lowered.startswith(".env.") and lowered != ".env.example")


def _path_digest(path: Path) -> Optional[str]:
    if path.is_symlink():
        return "symlink:" + hashlib.sha256(os.readlink(path).encode("utf-8", errors="replace")).hexdigest()
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _capture_target(path: Path) -> tuple[bool, bytes, int]:
    if not path.exists():
        return False, b"", 0o644
    if path.is_symlink() or not path.is_file():
        raise WorkspacePublishError(f"destination is not a regular file: {path}")
    return True, path.read_bytes(), path.stat().st_mode & 0o777


def _atomic_write(target: Path, content: bytes, mode: int) -> None:
    fd, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _rollback(backups: dict[Path, tuple[bool, bytes, int]]) -> list[str]:
    errors: list[str] = []
    for path, (existed, content, mode) in reversed(list(backups.items())):
        try:
            if existed:
                _atomic_write(path, content, mode)
            else:
                path.unlink(missing_ok=True)
        except OSError as exc:
            errors.append(f"{path}: {exc}")
    return errors
