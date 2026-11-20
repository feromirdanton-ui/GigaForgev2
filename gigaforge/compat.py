"""Console/encoding compatibility shims.

Windows still hands many Python processes a legacy code page (cp1251, cp866,
cp437).  Everything in this harness prints box-drawing characters and status
glyphs, so the first ``✗`` or ``×`` in a rich table raises
``UnicodeEncodeError: 'charmap' codec can't encode character`` and takes the
command down — which is exactly what ``gigaforge doctor`` did on a cp1251 console.

The fix is one place, applied at package import: force the standard streams onto
UTF-8 with ``errors="replace"``.  ``replace`` rather than ``strict`` because a
console that cannot represent a glyph should cost one character, never the run.
"""
from __future__ import annotations

import sys
from typing import Any

__all__ = ["harden_streams", "safe_print"]

_DONE = False


def harden_streams() -> None:
    """Reconfigure stdout/stderr to UTF-8 once, tolerating odd consoles."""
    global _DONE
    if _DONE:
        return
    _DONE = True
    for name in ("stdout", "stderr"):
        stream: Any = getattr(sys, name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        try:
            encoding = (getattr(stream, "encoding", "") or "").lower()
            if encoding.replace("-", "").replace("_", "") in {"utf8", "utf8mb4"}:
                continue
            reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - a frozen/piped stream is not fatal
            continue


def safe_print(text: str = "", *, stream: Any = None) -> None:
    """``print`` that degrades to ``?`` instead of raising on an odd console."""
    target = stream if stream is not None else sys.stdout
    try:
        print(text, file=target)
    except UnicodeEncodeError:
        encoding = getattr(target, "encoding", "ascii") or "ascii"
        print(text.encode(encoding, "replace").decode(encoding, "replace"), file=target)
