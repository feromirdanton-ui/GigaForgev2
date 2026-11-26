"""Print the canonical contrast table for every surface, from the code.

The report carried three numbers that did not reproduce: ``10.48 -> 9.85``,
``4.81 -> 4.52`` and ``5.23 -> 5.29``.  None of them was a before/after pair.
Each was **one token measured on two different surfaces** -- ``text`` is 10.48
on ``bg`` and 9.85 on ``raised``; ``diff_meta`` is 4.81 on ``bg`` and 4.52 on
``raised``; ``accent-ink`` is 5.29 on the page and 5.44 on the diff background
-- written into a "before -> after" column.  A reader comparing the two columns
concludes the fix made things worse, which is the opposite of what happened.

The cause is that the numbers were typed by hand from a one-off measurement.
This script is the fix: it reads the palettes and prints the table, so the
report can quote output instead of memory.  ``--markdown`` emits the block that
goes into ``qa/REPORT.md``.

    python scripts/contrast_table.py --markdown
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gigaforge.desktop.qml_tokens import THEMES, audit, tokens  # noqa: E402
from gigaforge.desktop.theme import Palette  # noqa: E402
from gigaforge.harness.wcag import contrast_ratio  # noqa: E402

#: The web panel's tokens, read from the CSS the product ships.
WEB = {
    "fg": "#183b66",
    "fg-dim": "#55708f",
    "accent-ink": "#1766c2",
    "accent (fill)": "#328df2",
    "ok-ink": "#147a4c",
    "warn-ink": "#8a5a08",
    "bad-ink": "#b02a41",
    "mag-ink": "#6d28d9",
}
WEB_SURFACES = {"page": "#f3f8fd", "diff": "#f7fbff", "card": "#ffffff"}

DESKTOP_TOKENS = (
    "text", "text_dim", "text_muted", "text_faint",
    "accent_ink", "violet_ink", "blue_ink",
    "ok", "warn", "err", "info",
    "diff_hunk", "diff_meta", "diff_add_fg", "diff_del_fg",
)
DESKTOP_SURFACES = ("bg", "surface", "panel", "panel_alt", "raised")


def _rgb(hex_colour: str) -> str:
    value = hex_colour.lstrip("#")
    return "rgb(%d,%d,%d)" % (
        int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)
    )


def _cr(fg: str, bg: str) -> float:
    return contrast_ratio(_rgb(fg), _rgb(bg))


def web_rows() -> list[tuple[str, dict[str, float]]]:
    return [
        (name, {surface: _cr(colour, bg) for surface, bg in WEB_SURFACES.items()})
        for name, colour in WEB.items()
    ]


def desktop_rows() -> list[tuple[str, dict[str, float]]]:
    rows = []
    for name in DESKTOP_TOKENS:
        colour = getattr(Palette, name)
        rows.append(
            (name, {s: _cr(colour, getattr(Palette, s)) for s in DESKTOP_SURFACES})
        )
    return rows


def qml_rows() -> list[tuple[str, str, dict[str, float]]]:
    rows = []
    for theme in THEMES:
        resolved = tokens(theme)
        for name in ("ink", "ink2", "ink3", "ink4", "primaryInk", "aquaInk", "mintInk"):
            colour = resolved.colours.get(name)
            if colour is None:
                continue
            rows.append(
                (theme, name, {s: _cr(colour, resolved.surfaces[s])
                               for s in ("field", "surface3")})
            )
    return rows


def _table(header: list[str], rows: list[list[str]]) -> str:
    out = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    for row in rows:
        out.append("| " + " | ".join(row) + " |")
    return "\n".join(out)


def markdown() -> str:
    parts: list[str] = []

    parts.append("### Web panel (control room)\n")
    parts.append(
        _table(
            ["token", *WEB_SURFACES],
            [[n, *[f"{v:.2f}" for v in vals.values()]] for n, vals in web_rows()],
        )
    )

    parts.append("\n### Desktop palette (Tk/Qt)\n")
    parts.append(
        _table(
            ["token", *DESKTOP_SURFACES],
            [[n, *[f"{v:.2f}" for v in vals.values()]] for n, vals in desktop_rows()],
        )
    )

    parts.append("\n### QML (primary surface)\n")
    parts.append(
        _table(
            ["theme", "token", "field", "surface3"],
            [[t, n, f"{v['field']:.2f}", f"{v['surface3']:.2f}"] for t, n, v in qml_rows()],
        )
    )

    parts.append("\n### Gates\n")
    gate_rows = []
    for theme in THEMES:
        failures = audit(theme)["failures"]
        gate_rows.append([f"QML {theme}", f"{len(failures)} failing pairs"])
    parts.append(_table(["gate", "result"], gate_rows))
    return "\n".join(parts)


def main() -> int:
    if "--markdown" in sys.argv:
        print(markdown())
        return 0
    for name, vals in web_rows():
        print(f"{name:16} " + "  ".join(f"{k}:{v:.2f}" for k, v in vals.items()))
    print()
    for name, vals in desktop_rows():
        print(f"{name:16} " + "  ".join(f"{k}:{v:.2f}" for k, v in vals.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
