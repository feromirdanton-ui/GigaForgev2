"""IGAFORGE L0 — local symbol index and AST tooling (zero LLM tokens).

The L0 layer answers "what is in this repository?" without ever calling a
model.  It is built from four pieces:

* :mod:`gigaforge.l0.parser` — tree-sitter symbol / import / call extraction;
* :mod:`gigaforge.l0.store` — SQLite persistence (symbols, edges, FTS5);
* :mod:`gigaforge.l0.indexer` — full and git-diff incremental indexing;
* :mod:`gigaforge.l0.repo_map` — ranked, token-budgeted repository outline;
* :mod:`gigaforge.l0.ast_tools` — structural edits gated by lint + re-parse.

Typical use::

    from gigaforge.l0 import L0Config, L0Store, Indexer, ASTTools, build_repo_map

    cfg = L0Config.from_env()
    with L0Store(config=cfg) as store:
        Indexer(store, config=cfg).index_full()
        print(build_repo_map(store, config=cfg).text)
        ASTTools(store, config=cfg).edit_function_body("app.py", "main", "return 0")
"""

from __future__ import annotations

from .ast_tools import ASTTools, EditResult
from .config import L0Config
from .db import SCHEMA_VERSION, connect, migrate
from .errors import (
    AmbiguousSymbolError,
    EditConflictError,
    EditRejectedError,
    GitError,
    IndexError_,
    L0Error,
    LintToolError,
    ParseError,
    RollbackError,
    SymbolNotFoundError,
    UnsupportedLanguageError,
)
from .indexer import Git, IndexResult, Indexer
from .languages import (
    LanguageSpec,
    available_languages,
    get_spec,
    spec_for_path,
    supported_extensions,
    supported_languages,
)
from .lint import Diagnostic, GateResult, Linter
from .parser import CallSite, Edge, Import, ParsedFile, Symbol, SymbolParser
from .repo_map import RankedSymbol, RepoMap, RepoMapBuilder, build_repo_map, pagerank
from .store import L0Store, StoreStats, file_id_for

__version__ = "0.2.0"

__all__ = [
    # config / db
    "L0Config",
    "SCHEMA_VERSION",
    "connect",
    "migrate",
    # errors
    "L0Error",
    "UnsupportedLanguageError",
    "ParseError",
    "SymbolNotFoundError",
    "AmbiguousSymbolError",
    "EditConflictError",
    "EditRejectedError",
    "RollbackError",
    "IndexError_",
    "GitError",
    "LintToolError",
    # languages
    "LanguageSpec",
    "get_spec",
    "spec_for_path",
    "supported_extensions",
    "supported_languages",
    "available_languages",
    # parser
    "SymbolParser",
    "Symbol",
    "Import",
    "CallSite",
    "Edge",
    "ParsedFile",
    # store
    "L0Store",
    "StoreStats",
    "file_id_for",
    # indexer
    "Indexer",
    "IndexResult",
    "Git",
    # repo map
    "RepoMap",
    "RankedSymbol",
    "RepoMapBuilder",
    "build_repo_map",
    "pagerank",
    # ast tools
    "ASTTools",
    "EditResult",
    # lint
    "Linter",
    "Diagnostic",
    "GateResult",
]
