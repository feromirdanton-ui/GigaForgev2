"""Cache mutations — deliberately outside the read path.

Every read command in the UI layer opens the database read-only and can therefore
run concurrently with the pipeline.  Deletion is the one operation that cannot
live there, so it is quarantined in this module and reachable only through an
explicit ``gigaforge cache purge`` call with ``--yes``.

* ``purge``      — drop entries by namespace / age, optionally dry-run
* ``invalidate`` — drop one key
* ``vacuum``     — reclaim the file space afterwards

A dry run counts what *would* be deleted and touches nothing: reporting a number
while still mutating would make "dry run" a lie.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional


def _connect_rw(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    if not path.is_file():
        raise FileNotFoundError(f"no database at {path}")
    conn = sqlite3.connect(str(path), timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def _cutoff_iso(older_than: Optional[str]) -> Optional[str]:
    """Turn ``7d`` / ``12h`` / ``30m`` into an ISO timestamp, or ``None``."""
    if not older_than:
        return None
    text = str(older_than).strip().lower()
    units = {"d": "days", "h": "hours", "m": "minutes", "s": "seconds"}
    unit = text[-1:]
    if unit not in units:
        raise ValueError(f"unrecognised age {older_than!r}: use e.g. 7d, 12h, 30m")
    try:
        amount = int(text[:-1])
    except ValueError as exc:
        raise ValueError(f"unrecognised age {older_than!r}: use e.g. 7d, 12h, 30m") from exc
    moment = datetime.now(timezone.utc) - timedelta(**{units[unit]: amount})
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def purge(
    db_path: str | Path,
    *,
    namespace: Optional[str] = None,
    older_than: Optional[str] = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Delete cache entries, optionally filtered; return what happened."""
    conn = _connect_rw(db_path)
    try:
        where: list[str] = []
        params: list[Any] = []
        if namespace:
            where.append("namespace = ?")
            params.append(namespace)
        cutoff = _cutoff_iso(older_than)
        if cutoff:
            where.append("created_at < ?")
            params.append(cutoff)
        clause = f" WHERE {' AND '.join(where)}" if where else ""

        matched = conn.execute(f"SELECT COUNT(*) AS n FROM cache_entries{clause}", params).fetchone()
        count = int(matched["n"] if matched else 0)
        freed = conn.execute(
            f"SELECT COALESCE(SUM(bytes), 0) AS b FROM cache_entries{clause}", params
        ).fetchone()
        freed_bytes = int((freed or {})["b"] if hasattr(freed, "keys") else 0) if freed else 0

        if not dry_run and count:
            conn.execute(f"DELETE FROM cache_entries{clause}", params)
            conn.commit()
        return {
            "matched": count,
            "deleted": 0 if dry_run else count,
            "freed_bytes": freed_bytes,
            "dry_run": bool(dry_run),
            "namespace": namespace or "",
            "older_than": older_than or "",
        }
    finally:
        conn.close()


def invalidate(db_path: str | Path, cache_key: str) -> dict[str, Any]:
    """Delete one cache entry by key."""
    conn = _connect_rw(db_path)
    try:
        cur = conn.execute("DELETE FROM cache_entries WHERE cache_key = ?", (cache_key,))
        conn.commit()
        return {"key": cache_key, "deleted": int(cur.rowcount or 0)}
    finally:
        conn.close()


def vacuum(db_path: str | Path) -> dict[str, Any]:
    """Reclaim space after a purge."""
    path = Path(db_path)
    before = path.stat().st_size if path.is_file() else 0
    conn = _connect_rw(db_path)
    try:
        conn.execute("VACUUM")
        conn.commit()
    finally:
        conn.close()
    after = path.stat().st_size if path.is_file() else 0
    return {"bytes_before": before, "bytes_after": after, "reclaimed": max(0, before - after)}


__all__ = ["invalidate", "purge", "vacuum"]
