"""The GigaForge desktop application — a standalone window, not a browser tab.

Run it with any of::

    gigaforge desktop                 # through the CLI
    gigaforge-desktop                 # installed console script
    python -m gigaforge.desktop       # module entry point
    start.bat                         # Windows launcher

Qt Quick is the primary surface of the harness. It opens on the V6.fresh
dashboard, Chat conversations, Deep Research and the project workspace. The
existing Tk window remains available through ``--ui tk`` while more tools move
to the new surface.
The browser control room remains an optional monitoring feature.
"""
from __future__ import annotations

APP_NAME = "GigaForge"
VERSION = "6.0.0"

__all__ = ["APP_NAME", "VERSION", "GigaForgeWindow", "run", "main"]


def __getattr__(name: str):
    """Load the legacy Tk shell only when a caller actually requests it.

    This keeps the QML entry point usable on Python distributions that do not
    ship Tk and avoids importing an entire second UI toolkit at QML startup.
    """
    if name in {"GigaForgeWindow", "run"}:
        from . import app

        return getattr(app, name)
    raise AttributeError(name)


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point for the desktop window."""
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        prog="gigaforge-desktop",
        description="GigaForge desktop control room (standalone window).",
    )
    parser.add_argument("--repo", "-r", default=None,
                        help="Repository root the harness should operate on.")
    parser.add_argument("--offline", action="store_true",
                        help="Use the scripted chat backend; never call a model.")
    parser.add_argument("--require-provider", action="store_true",
                        help="Exit instead of opening in read-only mode when no credential is set.")
    parser.add_argument("--version", action="version", version=f"GigaForge desktop {VERSION}")
    parser.add_argument("--view", default=None,
                        help="Open a page: dashboard, forge, files, agents, scenarios, library, "
                             "integrations, monitoring (chat/workspace alias forge).")
    parser.add_argument("--ui", choices=("tk", "qml"), default="qml",
                        help="Desktop surface (Qt Quick is primary; --ui tk opens the fallback).")
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])
    try:
        if args.ui == "qml":
            from .qt_app import run_qml

            return run_qml(repo_root=args.repo, offline=args.offline,
                           require_provider=args.require_provider, view=args.view)
        from .app import run

        return run(repo_root=args.repo, offline=args.offline,
                   require_provider=args.require_provider, view=args.view)
    except KeyboardInterrupt:  # pragma: no cover - interactive
        return 130
