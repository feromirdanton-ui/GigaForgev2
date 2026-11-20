"""Measured GigaChat context windows — the single source of truth.

GigaForge used to hard-code ``max_context = 8192`` (GigaChat-2-Pro's *published*
figure from an old model card).  The value measured against the production API
is **261 120** tokens for every public chat model, i.e. the harness was
under-using the model by a factor of ~32: budget pre-flight refused work the
model could easily hold, and the firewall sliced context far more aggressively
than necessary.

This module carries the measured table (taken from
``ai-forever/deepagents-gigachat``'s ``GIGACHAT_CONTEXT_WINDOWS``, measured
2026-08-03), the model-ID normalisation that makes the table actually match
what callers pass in, and the environment override.

Resolution order (first hit wins)
---------------------------------
1. an explicit ``override`` argument (per-provider / per-call);
2. ``GIGAFORGE_GIGA_MAX_CONTEXT`` (GigaForge's own knob);
3. ``GIGACHAT_CONTEXT_WINDOW`` (the name the official ecosystem uses);
4. the normalised model ID in :data:`GIGACHAT_CONTEXT_WINDOWS`;
5. :data:`DEFAULT_CONTEXT_WINDOW` (261 120) for an unknown model.

Step 5 is deliberate: an unknown model is far more likely to be a new
GigaChat-3 variant with the same 261 120 window than a 8 192-token model, and
guessing low is the expensive mistake (it silently degrades quality).
"""
from __future__ import annotations

import os
from typing import Any, Mapping, Optional

__all__ = [
    "GIGACHAT_CONTEXT_WINDOWS",
    "DEFAULT_CONTEXT_WINDOW",
    "CONTEXT_WINDOW_ENV_VARS",
    "normalize_model_id",
    "context_window_for",
    "context_window_table",
    "describe_context_windows",
]

#: Measured input-token limit per public chat model (2026-08-03).
GIGACHAT_CONTEXT_WINDOWS: dict[str, int] = {
    "GigaChat": 261_120,
    "GigaChat-Pro": 261_120,
    "GigaChat-Max": 261_120,
    "GigaChat-2": 261_120,
    "GigaChat-2-Pro": 261_120,
    "GigaChat-2-Max": 261_120,
    "GigaChat-2-Reasoning": 261_120,
    "GigaChat-3-Lightning": 261_120,
    "GigaChat-3-Pro": 261_120,
    "GigaChat-3-Ultra": 261_120,
}

#: Fallback for a model the table does not know (see module docstring, step 5).
DEFAULT_CONTEXT_WINDOW: int = 261_120

#: Environment variables consulted, in order, when no explicit override is given.
CONTEXT_WINDOW_ENV_VARS: tuple[str, ...] = (
    "GIGAFORGE_GIGA_MAX_CONTEXT",
    "GIGACHAT_CONTEXT_WINDOW",
)

_NORMALIZED: dict[str, int] = {
    name.casefold(): window for name, window in GIGACHAT_CONTEXT_WINDOWS.items()
}

# Provider prefixes the official SDK / LangChain integrations prepend.
_PROVIDER_PREFIXES = ("gigachat:", "giga:", "gigachat/", "giga/")


def normalize_model_id(model: Any) -> str:
    """Reduce a model identifier to the key used by the table.

    Handles every shape the ecosystem actually produces::

        "GigaChat-2-Pro"                  -> "GigaChat-2-Pro"
        "GigaChat-2-Pro:latest"           -> "GigaChat-2-Pro"
        "gigachat:GigaChat-3-Ultra"       -> "GigaChat-3-Ultra"
        "GigaChat-3-Pro-preview"          -> "GigaChat-3-Pro"
        "  gigachat/GigaChat-2-Max:v1  "  -> "GigaChat-2-Max"

    An empty / ``None`` identifier normalises to ``"GigaChat"`` — the base
    model, which is what the API defaults to.
    """
    if model is None:
        return "GigaChat"
    text = str(model).strip()
    if not text:
        return "GigaChat"
    lowered = text.casefold()
    for prefix in _PROVIDER_PREFIXES:
        if lowered.startswith(prefix):
            text = text[len(prefix):].strip()
            lowered = text.casefold()
            break
    # A version suffix (":latest", ":v1") is not part of the model family.
    if ":" in text:
        text = text.split(":", 1)[0].strip()
    if text.casefold().endswith("-preview"):
        text = text[: -len("-preview")].strip()
    return text or "GigaChat"


def _env_override(env: Optional[Mapping[str, str]] = None) -> Optional[int]:
    source: Mapping[str, str] = os.environ if env is None else env
    for name in CONTEXT_WINDOW_ENV_VARS:
        raw = source.get(name)
        if raw is None or str(raw).strip() == "":
            continue
        try:
            value = int(str(raw).strip())
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return None


def context_window_for(
    model: Any = None,
    *,
    override: Optional[int] = None,
    env: Optional[Mapping[str, str]] = None,
) -> int:
    """Resolve the context window for *model*.

    ``override`` wins over the environment, which wins over the table, which
    falls back to :data:`DEFAULT_CONTEXT_WINDOW`.  A non-positive override is
    ignored rather than propagated (a zero window would make every pre-flight
    check fail).
    """
    if override is not None:
        try:
            value = int(override)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            return value
    from_env = _env_override(env)
    if from_env is not None:
        return from_env
    key = normalize_model_id(model)
    return _NORMALIZED.get(key.casefold(), DEFAULT_CONTEXT_WINDOW)


def context_window_table() -> dict[str, int]:
    """A copy of the measured table (safe to mutate)."""
    return dict(GIGACHAT_CONTEXT_WINDOWS)


def describe_context_windows() -> list[dict[str, Any]]:
    """Table rows for the UI / ``gigaforge doctor`` output."""
    return [
        {"model": name, "max_context": window}
        for name, window in sorted(GIGACHAT_CONTEXT_WINDOWS.items())
    ]
