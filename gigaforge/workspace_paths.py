"""Shared policy for protecting local credentials inside project workspaces."""
from __future__ import annotations

from pathlib import PurePosixPath

SENSITIVE_WORKSPACE_DIRECTORIES = frozenset({
    ".aws", ".azure", ".credentials", ".env.d", ".gnupg", ".ssh",
    ".secrets", ".tokens", "credentials", "secrets", "tokens",
})
SENSITIVE_WORKSPACE_NAMES = frozenset({
    ".env", ".envrc", ".netrc", "_netrc", ".npmrc", ".pypirc", ".git-credentials",
    "id_rsa", "id_ed25519", "id_ecdsa", "id_dsa", "credentials",
    "credentials.json", "credentials.yaml", "credentials.yml", "credentials.toml",
    "service-account.json", "service_account.json",
    "token.json", "auth.json", "api-key.json", "api_key.json", "apikey.json",
})
SENSITIVE_WORKSPACE_SUFFIXES = (
    ".pem", ".key", ".p8", ".p12", ".pfx", ".jks", ".keystore", ".secret",
)


def is_sensitive_workspace_path(relative_path: str) -> bool:
    """Return whether a project-relative path points at local secrets or keys.

    ``.env.example`` is an intentionally shareable template. All other dotenv
    variants, credential stores, private-key formats, and known secret folders
    are protected consistently by editors, agent context selection, and diff
    application.
    """
    parts = tuple(part.casefold() for part in PurePosixPath(
        str(relative_path or "").replace("\\", "/")
    ).parts if part not in {"", "."})
    if not parts:
        return False
    name = parts[-1]
    if any(part in SENSITIVE_WORKSPACE_DIRECTORIES for part in parts):
        return True
    if name in SENSITIVE_WORKSPACE_NAMES or name.startswith("credentials"):
        return True
    if name.startswith(".env.") and name != ".env.example":
        return True
    if name.endswith(".env") or name.endswith(SENSITIVE_WORKSPACE_SUFFIXES):
        return True
    if name.endswith(".json") and name.startswith(("service-account", "service_account")):
        return True
    return False
