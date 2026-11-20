"""GigaForge command-line interface.

``main`` is the console-script entry point declared in ``pyproject.toml``.
Keeping the typer application in :mod:`gigaforge.cli.app` lets tests import it
without triggering the console-script shim.
"""

from __future__ import annotations

from .app import app, main_callback


def main() -> None:
    """Console-script entry point (``gigaforge ...``)."""
    app()


__all__ = ["app", "main", "main_callback"]
