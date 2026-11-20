"""Environment loading and credential normalisation.

Three problems this module exists to solve, all of them observed on a real
checkout:

1. **`.env` was never read.** The repository ships a `.env`, the docs tell you to
   fill it in, and nothing loaded it — so a correct credential file produced
   "no credential configured". Loading happens here, once, at package import.

2. **Two naming schemes.** The older layers use `GF_CLIENT__*`, `GF_L0__*`,
   `GF_FIREWALL__*`, `GF_CORE__*`; the newer settings object reads
   `GIGAFORGE_*`. A `.env` written for one scheme was invisible to the other, so
   the loader *translates* the legacy names onto the modern ones (never the
   reverse: the modern names win).

3. **The value carried a scheme prefix.** `GF_CLIENT__AUTH_KEY=Basic MDE5…`
   makes the client send `Authorization: Basic Basic MDE5…`, and Sber answers
   `Can't decode 'Authorization' header`. :func:`normalise_auth_key` strips a
   leading `Basic `/`Bearer `, quotes and whitespace, and rejects a value that is
   not `base64(uuid:uuid)` with a message that says what to fix.

Nothing here logs or returns a secret in readable form.
"""
from __future__ import annotations

import base64
import logging
import os
import string
import tempfile
from pathlib import Path
from typing import Iterable, Mapping

__all__ = [
    "load_env",
    "update_env_file",
    "normalise_auth_key",
    "looks_like_auth_key",
    "masked",
    "ENV_FILES",
]

log = logging.getLogger("gigaforge.env")

#: Where a `.env` is looked for, in priority order (first hit wins per key).
#: The package root comes first: the window and the CLI are routinely launched
#: from another directory (a shortcut's working dir, or a repository being worked
#: on), and a credential file sitting next to the installed package must still be
#: found.  This is why a live run initially reported "no credential" — the loader
#: only looked at the current directory.
def _default_env_files() -> tuple[str, ...]:
    package_root = Path(__file__).resolve().parent.parent
    return (
        str(package_root / ".env"),
        str(package_root / ".gigaforge.env"),
        ".gigaforge.env",
        ".env",
        "~/.gigaforge/.env",
    )


ENV_FILES: tuple[str, ...] = _default_env_files()

_PREFIXES = ("Basic ", "basic ", "BASIC ", "Bearer ", "bearer ")

#: Legacy env name → modern env name. The modern name always wins when both are set.
_ALIASES: dict[str, str] = {
    "GF_CLIENT__AUTH_KEY": "GIGAFORGE_AUTH_KEY",
    "GF_CLIENT__SCOPE": "GIGAFORGE_SCOPE",
    "GF_CLIENT__BASE_URL": "GIGAFORGE_BASE_URL",
    "GF_CLIENT__OAUTH_URL": "GIGAFORGE_AUTH_URL",
    "GF_CLIENT__MODEL": "GIGAFORGE_DEFAULT_MODEL",
    "GF_CLIENT__CA_BUNDLE": "GIGAFORGE_CA_CERT",
}


def load_env(
    paths: Iterable[str | Path] | None = None,
    *,
    override: bool = False,
    env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Load ``.env`` files into :data:`os.environ` and apply the legacy aliases.

    Existing environment variables win unless *override* is true — a shell export
    must never be silently replaced by a file. Returns the mapping that was
    applied (values redacted for keys that look like secrets).
    """
    target = os.environ if env is None else env  # type: ignore[assignment]
    files = [Path(p).expanduser() for p in (paths or ENV_FILES)]

    applied: dict[str, str] = {}
    for path in files:
        try:
            if not path.is_file():
                continue
            text = path.read_bytes().decode("utf-8", "replace")
        except OSError:
            continue
        for key, value in _parse(text).items():
            if not override and key in target and str(target[key]).strip():
                continue
            target[key] = value
            applied[key] = value

    # Translate the legacy scheme onto the modern one.
    for legacy, modern in _ALIASES.items():
        value = str(target.get(legacy) or "").strip()
        if not value:
            continue
        if override or not str(target.get(modern) or "").strip():
            target[modern] = value
            applied[modern] = value

    # ``VERIFY_SSL`` is the inverse of the modern ``TLS_VERIFY_OFF`` switch.
    verify = str(
        target.get("GF_CLIENT__TLS_VERIFY")
        or target.get("GF_CLIENT__VERIFY_SSL")
        or ""
    ).strip().lower()
    if verify in {"0", "false", "no", "off", "1", "true", "yes", "on"}:
        if override or not str(target.get("GIGAFORGE_TLS_VERIFY_OFF") or "").strip():
            disabled = verify in {"0", "false", "no", "off"}
            value = "1" if disabled else "0"
            target["GIGAFORGE_TLS_VERIFY_OFF"] = value
            applied["GIGAFORGE_TLS_VERIFY_OFF"] = value

    if "GIGAFORGE_AUTH_KEY" in applied:
        key = normalise_auth_key(applied["GIGAFORGE_AUTH_KEY"])
        if key:
            target["GIGAFORGE_AUTH_KEY"] = key
            applied["GIGAFORGE_AUTH_KEY"] = key

    if applied:
        log.debug("env applied from %s: %s", [str(p) for p in files],
                  {k: ("<secret>" if "KEY" in k else v) for k, v in applied.items()})
    return applied


def _parse(text: str) -> dict[str, str]:
    """Minimal ``KEY=VALUE`` parser: comments, quotes, `export`, CRLF."""
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if value and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        else:
            # strip an inline comment only when it is clearly separated
            if "  #" in value:
                value = value.split("  #", 1)[0].rstrip()
        out[key] = value
    return out


def update_env_file(values: Mapping[str, str], path: str | Path | None = None) -> Path:
    """Atomically upsert environment values while preserving unrelated lines.

    The desktop settings page and provider forms share this writer so a save
    cannot silently lose comments or unrelated settings. Duplicate definitions
    for an updated key are collapsed; the loader's last-value-wins behavior can
    therefore not restore a stale credential on the next launch.
    """
    env_path = Path(path) if path is not None else Path(__file__).resolve().parent.parent / ".env"
    updates: dict[str, str] = {}
    for raw_name, raw_value in values.items():
        name = str(raw_name).strip()
        if not name or name[0] not in string.ascii_letters + "_" or not all(
            char in string.ascii_letters + string.digits + "_" for char in name
        ):
            raise ValueError(f"invalid environment variable name: {name!r}")
        value = str(raw_value)
        if any(char in value for char in "\0\r\n"):
            raise ValueError(f"environment value for {name} cannot contain a line break or NUL")
        updates[name] = value
    if not updates:
        return env_path

    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        lines = []

    def serialized(value: str) -> str:
        if value != value.strip() or '"' in value or "'" in value or "  #" in value:
            return f'"{value}"'
        return value

    output: list[str] = []
    written: set[str] = set()
    for line in lines:
        candidate = line.strip()
        if candidate.startswith("export "):
            candidate = candidate[len("export "):].lstrip()
        current, separator, _ = candidate.partition("=")
        if separator and current.strip() in updates:
            name = current.strip()
            if name not in written:
                output.append(f"{name}={serialized(updates[name])}")
                written.add(name)
            continue
        output.append(line)
    for name, value in updates.items():
        if name not in written:
            output.append(f"{name}={serialized(value)}")

    env_path.parent.mkdir(parents=True, exist_ok=True)
    content = "\n".join(output).rstrip("\n") + "\n"
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{env_path.name}.", suffix=".tmp", dir=str(env_path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, env_path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise
    return env_path


def normalise_auth_key(raw: str) -> str:
    """Return a bare ``base64(client_id:client_secret)`` credential.

    Strips an accidental ``Basic ``/``Bearer `` prefix (the classic cause of
    ``Can't decode 'Authorization' header``), quotes and whitespace.
    """
    value = str(raw or "").strip().strip('"').strip("'").strip()
    for prefix in _PREFIXES:
        if value.startswith(prefix):
            value = value[len(prefix):].strip()
    # A URL-encoded or line-wrapped key is still a key.
    value = value.replace("\n", "").replace("\r", "").replace(" ", "").replace("\\", "")
    return value


def looks_like_auth_key(value: str) -> bool:
    """True when the value decodes to ``uuid:uuid`` — the only shape Sber accepts."""
    key = normalise_auth_key(value)
    if not key:
        return False
    try:
        decoded = base64.b64decode(key + "==", validate=False).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 - a malformed key is simply not a key
        return False
    client_id, _, secret = decoded.partition(":")
    return _is_uuid(client_id) and _is_uuid(secret)


def _is_uuid(text: str) -> bool:
    parts = text.strip().split("-")
    return len(parts) == 5 and [len(p) for p in parts] == [8, 4, 4, 4, 12]


def masked(value: str) -> str:
    """A safe rendering of a secret for logs and diagnostics."""
    key = normalise_auth_key(value)
    if not key:
        return "<empty>"
    return f"<{len(key)} chars, …{key[-4:]}>"
