"""Language registry: extension → tree-sitter grammar + node-kind tables.

The L0 layer is deliberately grammar-driven.  Every language contributes a
:class:`LanguageSpec` describing which tree-sitter node kinds denote a
definition, a call, an import, and how to pull a name / signature / docstring
out of them.  Adding a language is a data change, not a code change.

Grammars are loaded lazily and defensively: a missing optional grammar
package degrades that language to "unsupported" instead of breaking import.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from .errors import UnsupportedLanguageError


@dataclass(frozen=True)
class LanguageSpec:
    """Everything the parser needs to know about one language."""

    name: str
    extensions: tuple[str, ...]
    module: str  # importable tree-sitter grammar package
    #: node kinds that introduce a named symbol
    definition_kinds: Mapping[str, str] = field(default_factory=dict)
    #: node kinds that denote a call site
    call_kinds: tuple[str, ...] = ()
    #: node kinds that denote an import / use
    import_kinds: tuple[str, ...] = ()
    #: node kinds that denote a container (class / module / impl)
    container_kinds: tuple[str, ...] = ()
    #: node kinds whose text is a docstring / comment block
    docstring_kinds: tuple[str, ...] = ()
    #: field name holding the symbol's identifier
    name_field: str = "name"
    #: field name holding the parameter list
    params_field: str = "parameters"
    #: field name holding the body
    body_field: str = "body"
    #: field name holding the return type
    return_field: str = "return_type"
    #: node kinds that carry a type annotation worth recording
    type_kinds: tuple[str, ...] = ()
    #: how to render a signature: "text" (verbatim slice) or "python"
    signature_style: str = "text"
    #: comment prefix used when synthesising docstrings
    comment_prefix: str = "#"


PYTHON = LanguageSpec(
    name="python",
    extensions=(".py", ".pyi"),
    module="tree_sitter_python",
    definition_kinds={
        "function_definition": "function",
        "class_definition": "class",
        "decorated_definition": "decorated",
    },
    call_kinds=("call",),
    import_kinds=("import_statement", "import_from_statement"),
    container_kinds=("class_definition", "module"),
    docstring_kinds=("string", "expression_statement"),
    name_field="name",
    params_field="parameters",
    body_field="body",
    return_field="return_type",
    type_kinds=("type", "identifier", "subscript", "attribute"),
    signature_style="python",
    comment_prefix="#",
)

JAVASCRIPT = LanguageSpec(
    name="javascript",
    extensions=(".js", ".jsx", ".mjs", ".cjs"),
    module="tree_sitter_javascript",
    definition_kinds={
        "function_declaration": "function",
        "function_expression": "function",
        "arrow_function": "function",
        "method_definition": "method",
        "class_declaration": "class",
        "lexical_declaration": "variable",
        "variable_declaration": "variable",
    },
    call_kinds=("call_expression", "new_expression"),
    import_kinds=("import_statement",),
    container_kinds=("class_declaration", "class_body", "program"),
    docstring_kinds=("comment",),
    name_field="name",
    params_field="parameters",
    body_field="body",
    return_field="return_type",
    type_kinds=("type_identifier", "identifier"),
    signature_style="text",
    comment_prefix="//",
)

TYPESCRIPT = LanguageSpec(
    name="typescript",
    extensions=(".ts", ".tsx"),
    module="tree_sitter_typescript",
    definition_kinds={
        "function_declaration": "function",
        "function_signature": "function",
        "method_definition": "method",
        "method_signature": "method",
        "class_declaration": "class",
        "interface_declaration": "interface",
        "type_alias_declaration": "type",
        "enum_declaration": "enum",
        "lexical_declaration": "variable",
        "variable_declaration": "variable",
        "abstract_class_declaration": "class",
    },
    call_kinds=("call_expression", "new_expression"),
    import_kinds=("import_statement",),
    container_kinds=("class_declaration", "interface_declaration", "program"),
    docstring_kinds=("comment",),
    name_field="name",
    params_field="parameters",
    body_field="body",
    return_field="return_type",
    type_kinds=("type_identifier", "predefined_type", "generic_type"),
    signature_style="text",
    comment_prefix="//",
)

GO = LanguageSpec(
    name="go",
    extensions=(".go",),
    module="tree_sitter_go",
    definition_kinds={
        "function_declaration": "function",
        "method_declaration": "method",
        "type_declaration": "type",
        "type_spec": "type",
        "const_declaration": "const",
        "var_declaration": "variable",
    },
    call_kinds=("call_expression",),
    import_kinds=("import_declaration",),
    container_kinds=("type_declaration", "source_file"),
    docstring_kinds=("comment",),
    name_field="name",
    params_field="parameters",
    body_field="body",
    return_field="result",
    type_kinds=("type_identifier", "qualified_type", "pointer_type"),
    signature_style="text",
    comment_prefix="//",
)

RUST = LanguageSpec(
    name="rust",
    extensions=(".rs",),
    module="tree_sitter_rust",
    definition_kinds={
        "function_item": "function",
        "struct_item": "struct",
        "enum_item": "enum",
        "trait_item": "trait",
        "impl_item": "impl",
        "mod_item": "module",
        "const_item": "const",
        "static_item": "static",
        "type_item": "type",
        "macro_definition": "macro",
    },
    call_kinds=("call_expression", "macro_invocation"),
    import_kinds=("use_declaration",),
    container_kinds=("impl_item", "trait_item", "mod_item", "source_file"),
    docstring_kinds=("line_comment", "block_comment"),
    name_field="name",
    params_field="parameters",
    body_field="body",
    return_field="return_type",
    type_kinds=("type_identifier", "generic_type", "reference_type"),
    signature_style="text",
    comment_prefix="//",
)

JAVA = LanguageSpec(
    name="java",
    extensions=(".java",),
    module="tree_sitter_java",
    definition_kinds={
        "method_declaration": "method",
        "constructor_declaration": "constructor",
        "class_declaration": "class",
        "interface_declaration": "interface",
        "enum_declaration": "enum",
        "record_declaration": "record",
        "field_declaration": "field",
    },
    call_kinds=("method_invocation", "object_creation_expression"),
    import_kinds=("import_declaration",),
    container_kinds=("class_declaration", "interface_declaration", "program"),
    docstring_kinds=("block_comment", "line_comment"),
    name_field="name",
    params_field="parameters",
    body_field="body",
    return_field="type",
    type_kinds=("type_identifier", "generic_type"),
    signature_style="text",
    comment_prefix="//",
)

SPECS: tuple[LanguageSpec, ...] = (PYTHON, JAVASCRIPT, TYPESCRIPT, GO, RUST, JAVA)

_BY_NAME: dict[str, LanguageSpec] = {spec.name: spec for spec in SPECS}
_BY_EXT: dict[str, LanguageSpec] = {}
for _spec in SPECS:
    for _ext in _spec.extensions:
        _BY_EXT[_ext] = _spec

#: Aliases accepted by :func:`get_spec`.
_ALIASES = {
    "py": "python",
    "python3": "python",
    "js": "javascript",
    "jsx": "javascript",
    "mjs": "javascript",
    "cjs": "javascript",
    "ts": "typescript",
    "tsx": "typescript",
    "golang": "go",
    "rs": "rust",
}


def get_spec(language: str) -> LanguageSpec:
    """Resolve a language name (or alias) to its :class:`LanguageSpec`."""
    key = language.strip().lower()
    key = _ALIASES.get(key, key)
    spec = _BY_NAME.get(key)
    if spec is None:
        raise UnsupportedLanguageError(
            f"unsupported language: {language!r}",
            detail={"language": language, "supported": sorted(_BY_NAME)},
        )
    return spec


def spec_for_path(path: str | Path) -> Optional[LanguageSpec]:
    """Return the spec for a file path, or ``None`` if the extension is unknown."""
    return _BY_EXT.get(Path(path).suffix.lower())


def supported_extensions() -> tuple[str, ...]:
    return tuple(sorted(_BY_EXT))


def supported_languages() -> tuple[str, ...]:
    return tuple(sorted(_BY_NAME))


# ── grammar loading ──────────────────────────────────────────────────────
_GRAMMAR_CACHE: dict[str, Any] = {}


def load_language(spec: LanguageSpec) -> Any:
    """Import the tree-sitter grammar for ``spec`` and return a ``Language``.

    Raises :class:`UnsupportedLanguageError` when the optional grammar
    package is not installed, so callers can skip the file gracefully.
    """
    if spec.name in _GRAMMAR_CACHE:
        return _GRAMMAR_CACHE[spec.name]

    try:
        from tree_sitter import Language  # type: ignore
    except Exception as exc:  # pragma: no cover - tree_sitter is a hard dep
        raise UnsupportedLanguageError(
            "tree_sitter is not installed", detail={"error": str(exc)}
        ) from exc

    try:
        module = importlib.import_module(spec.module)
    except Exception as exc:
        raise UnsupportedLanguageError(
            f"grammar package {spec.module!r} is not installed",
            detail={"language": spec.name, "module": spec.module, "error": str(exc)},
        ) from exc

    # tree-sitter >= 0.22 exposes ``language()``; older builds expose
    # ``language`` as a module-level capsule.
    raw = getattr(module, "language", None)
    if callable(raw):
        raw = raw()
    if raw is None:  # pragma: no cover - defensive
        raise UnsupportedLanguageError(
            f"grammar package {spec.module!r} exposes no language()",
            detail={"language": spec.name},
        )

    language = Language(raw)
    _GRAMMAR_CACHE[spec.name] = language
    return language


def make_parser(spec: LanguageSpec) -> Any:
    """Return a configured ``tree_sitter.Parser`` for ``spec``."""
    from tree_sitter import Parser  # type: ignore

    return Parser(load_language(spec))


def available_languages() -> tuple[str, ...]:
    """Languages whose grammar package is actually importable right now."""
    out: list[str] = []
    for spec in SPECS:
        try:
            load_language(spec)
        except UnsupportedLanguageError:
            continue
        out.append(spec.name)
    return tuple(sorted(out))


__all__ = [
    "LanguageSpec",
    "SPECS",
    "get_spec",
    "spec_for_path",
    "supported_extensions",
    "supported_languages",
    "available_languages",
    "load_language",
    "make_parser",
    "PYTHON",
    "JAVASCRIPT",
    "TYPESCRIPT",
    "GO",
    "RUST",
    "JAVA",
]
