"""Single-page mini-site generator for GigaForge Landing.

The site is a self-contained static landing page rendered as a single HTML
string. Content is supplied as plain Python data structures (no templating
engine — we want this to be readable, deterministic, and easy to feed to
a model as a Context Slice).

Layout (top -> bottom):
  - <header> with brand + tagline
  - <section id="hero"> with title, subtitle, primary CTA
  - <section id="features"> with 3..N feature cards
  - <section id="pricing"> with up to 3 plans
  - <footer> with copyright + minimal links

Each section is a stub that builds its own <section>...</section> block;
the orchestrator (`build_page`) stitches them in order. Tests cover the
happy path: passing valid content produces the expected scaffold.
"""

from __future__ import annotations

from html import escape
from typing import Any


# ---- CSS (intentionally minimal — the orchestrator can add dark-mode later)
_CSS = """
:root { color-scheme: light dark; font-family: system-ui, sans-serif; }
* { box-sizing: border-box; }
body { margin: 0; line-height: 1.55; }
header, section, footer { padding: 24px; max-width: 960px; margin: auto; }
.card { border: 1px solid #ccc3; border-radius: 8px; padding: 12px 16px; }
.btn { display: inline-block; padding: 10px 18px; border-radius: 6px;
       background: #2d6cdf; color: white; text-decoration: none; }
"""


# ---------- section builders ----------


def _section(title: str, body_html: str, *, section_id: str = "") -> str:
    """Return a single <section>...</section> block."""
    id_attr = f' id="{escape(section_id)}"' if section_id else ""
    return f'<section{id_attr}>\n<h2>{escape(title)}</h2>\n{body_html}\n</section>'


def build_header(brand: str, tagline: str) -> str:
    """Return the <header>...</header> block."""
    return (
        f"<header>\n"
        f"  <strong>{escape(brand)}</strong>\n"
        f"  <span> — {escape(tagline)}</span>\n"
        f"</header>"
    )


def build_hero(title: str, subtitle: str, cta_label: str, cta_url: str) -> str:
    """Return the hero section: title + subtitle + primary CTA button."""
    body = (
        f'<h1>{escape(title)}</h1>\n'
        f'<p>{escape(subtitle)}</p>\n'
        f'<a class="btn" href="{escape(cta_url)}">{escape(cta_label)}</a>'
    )
    return _section("Hero", body, section_id="hero")


def build_features(features: list[dict[str, str]]) -> str:
    """Return the features section: one card per feature dict.

    Each feature dict has keys ``title`` and ``body``.
    """
    if not features:
        return _section("Features", "<p>(no features yet)</p>", section_id="features")
    cards = []
    for f in features:
        cards.append(
            f'<div class="card">\n'
            f'  <h3>{escape(f.get("title", ""))}</h3>\n'
            f'  <p>{escape(f.get("body", ""))}</p>\n'
            f'</div>'
        )
    body = "\n".join(cards)
    return _section("Features", body, section_id="features")


def build_pricing(plans: list[dict[str, str]]) -> str:
    """Return the pricing section: one card per plan dict.

    Each plan dict has keys ``name``, ``price``, ``details``.
    """
    if not plans:
        return _section("Pricing", "<p>(no plans yet)</p>", section_id="pricing")
    cards = []
    for p in plans:
        cards.append(
            f'<div class="card">\n'
            f'  <h3>{escape(p.get("name", ""))} — {escape(p.get("price", ""))}</h3>\n'
            f'  <p>{escape(p.get("details", ""))}</p>\n'
            f'</div>'
        )
    body = "\n".join(cards)
    return _section("Pricing", body, section_id="pricing")


def build_footer(copyright: str, links: list[dict[str, str]]) -> str:
    """Return the <footer>...</footer> block with copyright + minimal links.

    Each link dict has keys ``label`` and ``href``.
    """
    link_html = " &middot; ".join(
        f'<a href="{escape(l["href"])}">{escape(l["label"])}</a>' for l in links
    )
    return f"<footer>\n  <small>© {escape(copyright)}</small>\n  {link_html}\n</footer>"


# ---------- orchestrator ----------


def build_page(content: dict[str, Any]) -> str:
    """Render the full single-page landing as one HTML string.

    ``content`` MUST contain:
      - ``brand``     : str
      - ``tagline``   : str
      - ``title``     : str  (hero title)
      - ``subtitle``  : str  (hero subtitle)
      - ``cta_label`` : str  (hero CTA button text)
      - ``cta_url``   : str  (hero CTA button href)
      - ``features``  : list[dict]  (each: title, body) — optional, may be empty
      - ``plans``     : list[dict]  (each: name, price, details) — optional
      - ``copyright`` : str  (footer)
      - ``links``     : list[dict] (each: label, href) — optional
    """
    header = build_header(content["brand"], content["tagline"])
    hero = build_hero(
        content["title"], content["subtitle"],
        content["cta_label"], content["cta_url"],
    )
    features = build_features(content.get("features", []))
    pricing = build_pricing(content.get("plans", []))
    footer = build_footer(content["copyright"], content.get("links", []))

    body = "\n".join([header, hero, features, pricing, footer])
    return (
        "<!doctype html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '  <meta charset="utf-8">\n'
        f"  <title>{escape(content['brand'])} — {escape(content['tagline'])}</title>\n"
        f"  <style>{_CSS}</style>\n"
        "</head>\n"
        "<body>\n"
        f"{body}\n"
        "</body>\n"
        "</html>\n"
    )


# ---------- content preset used by the CLI demo entry-point ----------


DEFAULT_CONTENT: dict[str, Any] = {
    "brand": "ГигаФорге",
    "tagline": "Orchestration harness for codegen",
    "title": "Stop fighting context. Ship the diff.",
    "subtitle": (
        "GigaForge plans → codes → tests → reviews → commits, with a hard token "
        "budget and a deterministic cache. One tool, one tight loop."
    ),
    "cta_label": "Read the spec",
    "cta_url": "https://github.com/MiniMax-AI/gigaforge",
    "features": [
        {"title": "L0 indexing", "body": "Tree-sitter + SQLite, zero LLM calls. One repo, one map."},
        {"title": "Firewall-compressed stderr", "body": "10×–80× smaller. LLM never reads a megabyte of pytest noise."},
        {"title": "Atomic worktree per task", "body": "Each task in its own branch. Merged on success, pruned on exit."},
    ],
    "plans": [
        {"name": "Dev",       "price": "free",   "details": "Local repos, 1 task at a time."},
        {"name": "Team",      "price": "$49/mo", "details": "Up to 5 parallel tasks, shared cache."},
        {"name": "Enterprise", "price": "talk",   "details": "SSO, on-prem, custom model routing."},
    ],
    "copyright": "2026 GigaForge",
    "links": [
        {"label": "Docs",   "href": "#"},
        {"label": "Status", "href": "#"},
        {"label": "GitHub", "href": "https://github.com"},
    ],
}


def cli() -> None:
    """Tiny CLI: ``python build.py > site.html`` writes the default landing."""
    import sys
    sys.stdout.write(build_page(DEFAULT_CONTENT))


if __name__ == "__main__":
    cli()
