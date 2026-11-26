"""Run the CLI with .env loaded by python-dotenv (handles CRLF and hyphen keys)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path("/workspace/repo")
for key, value in dotenv_values(ROOT / ".env").items():
    if value is None:
        continue
    # Old catalogue names -> the names the account actually serves.
    key = key.replace("GF_CORE__PRICES__GigaChat-Pro__", "GF_CORE__PRICES__GigaChat-2-Pro__")
    key = key.replace("GF_CORE__PRICES__GigaChat__", "GF_CORE__PRICES__GigaChat-2__")
    os.environ[key] = value
os.environ.setdefault("GF_CLIENT__MODEL", "GigaChat-2")

sys.path.insert(0, str(ROOT))
from gigaforge.cli.app import app  # noqa: E402

if __name__ == "__main__":
    sys.argv = ["gigaforge", *sys.argv[1:]]
    app()
