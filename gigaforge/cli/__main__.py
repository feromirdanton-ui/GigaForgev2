"""``python -m gigaforge.cli`` — the CLI as a runnable module.

Without this file ``python -m gigaforge.cli`` imports the package and exits 0
having done nothing, which is the most confusing possible outcome: no usage, no
error, success status.  The package's ``__init__`` already re-exports
:data:`gigaforge.cli.app`, so this module only has to invoke it.
"""

from __future__ import annotations

from .app import app


if __name__ == "__main__":
    app()
