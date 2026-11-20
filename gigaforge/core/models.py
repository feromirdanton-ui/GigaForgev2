"""Model catalog resolution — pick the models this account actually has.

The router is configured with role names (``cheap`` / ``strong`` / ``default``) and
per-phase routes.  Those names used to be hardcoded to a catalogue that a given
account may not own: a live run failed with ``{"status":404,"message":"No such
model"}`` because the plan phase asked for ``GigaChat-3-Lightning`` while the
endpoint served ``GigaChat-2``, ``GigaChat-2-Pro``, ``GigaChat-Max`` and a handful
of others.

Hardcoding a catalogue is a permanent bet that the provider never renames
anything.  This module makes the opposite bet: ask the endpoint, rank what came
back by role, and *verify* that the configured names exist.  The result is cached
briefly so a run does not pay for a ``/models`` call every time, and a probe
failure degrades to the configured names rather than blocking the run.

Ranking is deliberately lexical and tiny — a model id is the only signal available
without spending tokens, and the ordering (Max > Pro > base, Lightning/Lite for the
cheap role) matches every GigaChat catalogue published so far.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable, Optional

__all__ = [
    "ModelCatalog",
    "RoleModels",
    "rank_models",
    "resolve_roles",
    "apply_roles",
    "catalog_path",
    "load_catalog",
]

log = logging.getLogger("gigaforge.models")

CACHE_TTL_S = 6 * 3600.0


def catalog_path(cfg: Any = None) -> Path:
    """Where the probe result is cached (next to the core database)."""
    db = getattr(cfg, "db_path", None) or ".gigaforge/core.sqlite3"
    return Path(db).parent / "models.json"


@dataclass
class RoleModels:
    """The three role slots the router understands."""

    cheap: str = ""
    strong: str = ""
    default: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"cheap": self.cheap, "strong": self.strong, "default": self.default}


@dataclass
class ModelCatalog:
    """Live model list plus the resolved role mapping."""

    models: list[str]
    roles: RoleModels
    fetched_at: float = 0.0
    source: str = "live"  # live | cache | configured
    error: str = ""

    @property
    def available(self) -> bool:
        return bool(self.models)

    def has(self, name: str) -> bool:
        if not name:
            return False
        lowered = name.lower()
        return any(m.lower() == lowered for m in self.models)

    def missing(self, names: Iterable[str]) -> list[str]:
        return [n for n in names if n and not self.has(n)]

    def to_dict(self) -> dict[str, Any]:
        return {
            "models": list(self.models),
            "roles": self.roles.to_dict(),
            "fetched_at": self.fetched_at,
            "source": self.source,
        }

    # -- persistence -------------------------------------------------------
    def save(self, path: Path) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
            tmp.replace(path)
        except OSError:  # pragma: no cover - read-only checkout
            log.debug("could not cache the model catalog", exc_info=True)


def load_catalog(path: Path, *, ttl_s: float = CACHE_TTL_S) -> Optional[ModelCatalog]:
    """Read a cached catalog if it is still fresh."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    fetched_at = float(payload.get("fetched_at") or 0.0)
    if time.time() - fetched_at > ttl_s:
        return None
    roles = payload.get("roles") or {}
    return ModelCatalog(
        models=[str(m) for m in payload.get("models") or []],
        roles=RoleModels(
            cheap=str(roles.get("cheap") or ""),
            strong=str(roles.get("strong") or ""),
            default=str(roles.get("default") or ""),
        ),
        fetched_at=fetched_at,
        source="cache",
    )


# ══════════════════════════════════════════════════════════════════════════
# ranking
# ══════════════════════════════════════════════════════════════════════════
_STRONG_ORDER = (
    ("max", 100),
    ("ultra", 95),
    ("pro", 80),
    ("plus", 60),
    ("2", 40),
    ("1", 20),
)
_CHEAP_ORDER = (
    ("lightning", 100),
    ("lite", 95),
    ("light", 90),
    ("flash", 85),
    ("mini", 80),
    ("plus", 40),
    ("2", 20),
)


def _score(name: str, table: tuple[tuple[str, int], ...]) -> int:
    low = name.lower()
    best = 0
    for token, weight in table:
        if token in low:
            best = max(best, weight)
    # A preview/experimental model is worse than the same model in GA.
    if "preview" in low or "beta" in low:
        best -= 15
    return best


def rank_models(models: Iterable[str]) -> dict[str, list[str]]:
    """Split the catalogue into ``strong`` and ``cheap`` rankings (best first)."""
    names = [str(m) for m in models if str(m).strip()]
    strong = sorted(names, key=lambda n: (-_score(n, _STRONG_ORDER), n.lower()))
    cheap = sorted(names, key=lambda n: (-_score(n, _CHEAP_ORDER), len(n), n.lower()))
    return {"strong": strong, "cheap": cheap}


def resolve_roles(
    models: Iterable[str],
    *,
    configured_cheap: str = "",
    configured_strong: str = "",
    configured_default: str = "",
) -> RoleModels:
    """Pick the three role models from what the endpoint actually serves.

    A configured name that exists wins; otherwise the ranked best is taken.  The
    default role prefers the unversioned base model (``GigaChat``), which every
    catalogue has carried so far, before falling back to the strongest.
    """
    names = [str(m) for m in models if str(m).strip()]
    if not names:
        return RoleModels(configured_cheap, configured_strong, configured_default)

    ranked = rank_models(names)
    by_lower = {m.lower(): m for m in names}

    def pick(configured: str, ordered: list[str]) -> str:
        if configured and configured.lower() in by_lower:
            return by_lower[configured.lower()]
        return ordered[0] if ordered else ""

    default = ""
    for candidate in ("gigachat", "gigachat-2", "gigachat-2-pro", "gigachat-pro"):
        if candidate in by_lower:
            default = by_lower[candidate]
            break
    if not default:
        default = ranked["strong"][0] if ranked["strong"] else names[0]

    cheap = pick(configured_cheap, ranked["cheap"])
    strong = pick(configured_strong, ranked["strong"])
    if not cheap:
        cheap = default
    if not strong:
        strong = default
    wanted_default = configured_default if configured_default.lower() in by_lower else default
    return RoleModels(cheap=cheap, strong=strong, default=wanted_default)


# ══════════════════════════════════════════════════════════════════════════
# application
# ══════════════════════════════════════════════════════════════════════════
def apply_roles(cfg: Any, roles: RoleModels) -> Any:
    """Return a :class:`CoreConfig` whose routes use the resolved models.

    Every phase keeps its temperature and rationale; only the model names and the
    fallback chains change.  A fallback chain that contains the primary model is
    pointless, so duplicates are dropped and the default model is appended as the
    last resort.
    """
    from .config import default_routes

    routes = dict(getattr(cfg, "routes", None) or default_routes(cfg))
    fallback_chain = tuple(dict.fromkeys(
        [roles.strong, roles.default, roles.cheap]
    ))
    for phase, route in list(routes.items()):
        primary = roles.cheap if phase in {"retrieve", "classify", "summarize"} else roles.strong
        if not primary:
            primary = route.model
        fallbacks = tuple(m for m in fallback_chain if m and m != primary)
        routes[phase] = replace(route, model=primary, fallbacks=fallbacks)
    return replace(
        cfg,
        cheap_model=roles.cheap or cfg.cheap_model,
        strong_model=roles.strong or cfg.strong_model,
        default_model=roles.default or cfg.default_model,
        routes=routes,
    )


def fetch_models(client: Any, *, path: Optional[Path] = None,
                 ttl_s: float = CACHE_TTL_S, force: bool = False) -> ModelCatalog:
    """Ask the provider for its catalogue, using the on-disk cache when fresh."""
    cache = Path(path) if path else Path(".gigaforge/models.json")
    if not force:
        cached = load_catalog(cache, ttl_s=ttl_s)
        if cached is not None and cached.available:
            return cached
    models: list[str] = []
    try:
        payload = client.list_models()
        for item in payload or []:
            if isinstance(item, dict):
                name = item.get("id") or item.get("name") or item.get("model")
                if name:
                    models.append(str(name))
            elif isinstance(item, str):
                models.append(item)
    except Exception as exc:  # noqa: BLE001 - a probe failure must not block a run
        log.warning("model probe failed: %s", exc)
        error = f"{type(exc).__name__}: {exc}"
        cached = load_catalog(cache, ttl_s=1e9)
        if cached is not None:
            cached.error = error
            return cached
        return ModelCatalog(models=[], roles=RoleModels(), source="configured", error=error)

    catalog = ModelCatalog(models=sorted(set(models)), roles=resolve_roles(models),
                           fetched_at=time.time(), source="live")
    catalog.save(cache)
    return catalog


def env_skip_probe() -> bool:
    return (os.environ.get("GIGAFORGE_SKIP_MODEL_PROBE") or "").strip().lower() in {
        "1", "true", "yes", "on"
    }


def looks_like_model_error(message: str) -> bool:
    """True when a provider error means "this model name does not exist"."""
    low = (message or "").lower()
    return any(token in low for token in ("no such model", "model_not_found",
                                          "unknown model", "does not exist"))
