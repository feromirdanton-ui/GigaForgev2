"""GigaForge - orchestration harness for the GigaChat API (Sber)."""
import os
import sys
from pathlib import Path

# Ensure Python Scripts dirs are in PATH (Windows user scripts often omitted from PATH)
_scripts_dirs = [
    Path(sys.executable).parent / "Scripts",
    Path.home() / "AppData" / "Roaming" / "Python" / f"Python{sys.version_info.major}{sys.version_info.minor}" / "Scripts",
]
for _sd in _scripts_dirs:
    if _sd.exists() and str(_sd) not in os.environ.get("PATH", ""):
        os.environ["PATH"] = str(_sd) + os.pathsep + os.environ.get("PATH", "")

# Legacy Windows code pages (cp1251/cp866/cp437) cannot encode the status glyphs
# this harness prints, so a rich table would raise UnicodeEncodeError and kill the
# command. Force UTF-8 with errors="replace" once, at import.
from gigaforge.compat import harden_streams as _harden_streams  # noqa: E402

_harden_streams()

# Load .env and translate the legacy GF_CLIENT__*/GF_CORE__* names onto the
# GIGAFORGE_* settings names. Without this a correctly filled .env was invisible
# and every front end reported "no credential configured".
from gigaforge.env import load_env as _load_env  # noqa: E402

_load_env()

__version__ = "6.0.0"
