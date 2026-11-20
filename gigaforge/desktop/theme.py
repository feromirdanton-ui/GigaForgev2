"""V6.fresh design tokens and ttk styling for the GigaForge desktop window.

One palette, one font ladder, one place to change them. The V6.fresh surfaces use
cool whites and pale blue panels, with a stronger blue accent and semantic status
colours reserved for actual runtime state.

Tk has no CSS or per-widget transparency. The shared canvas primitives in
:mod:`gigaforge.desktop.widgets` draw rounded surfaces, soft shadows and circular
icons while keeping the native app responsive and keyboard-operable.
"""
from __future__ import annotations

import sys
import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

__all__ = ["Palette", "Fonts", "apply_theme", "enable_hidpi", "mix"]


class Palette:
    """Light, glassy surfaces with a restrained cyan/blue forge accent."""

    # surfaces
    bg = "#f0f8f6"
    surface = "#fbfefe"
    panel = "#f8fcfb"
    panel_alt = "#edf6f4"
    raised = "#e5f2ef"
    border = "#d9e9e5"
    border_strong = "#c7ded9"
    shadow = "#deede9"
    wash_mint = "#d3f3e8"
    wash_lime = "#eaf3c8"
    wash_blue = "#d8eafa"

    # text
    text = "#183b66"
    text_dim = "#4d6b91"
    text_muted = "#718bad"
    text_faint = "#96abc5"

    # accents
    accent = "#4b9fea"
    accent_dim = "#3687c9"
    accent_soft = "#e4f3fb"
    violet = "#817ce6"
    violet_soft = "#f1f0ff"
    blue = "#48b7c7"

    # status
    ok = "#1da96a"
    ok_soft = "#e3f8ed"
    warn = "#bd7b11"
    warn_soft = "#fff4df"
    err = "#d64d63"
    err_soft = "#ffedf0"
    info = "#287ed2"
    info_soft = "#e8f3ff"

    # diff
    diff_add_bg = "#e8f8ef"
    diff_add_fg = "#157b4d"
    diff_del_bg = "#fff0f1"
    diff_del_fg = "#b83d51"
    diff_hunk = "#456fd1"
    diff_meta = "#718bad"

    # selection
    select_bg = "#dceeff"
    highlight = "#2aadd4"

    @classmethod
    def status(cls, name: str) -> tuple[str, str]:
        """``(foreground, soft background)`` for a status token."""
        return {
            "ok": (cls.ok, cls.ok_soft),
            "succeeded": (cls.ok, cls.ok_soft),
            "accepted": (cls.ok, cls.ok_soft),
            "pass": (cls.ok, cls.ok_soft),
            "running": (cls.info, cls.info_soft),
            "warn": (cls.warn, cls.warn_soft),
            "warning": (cls.warn, cls.warn_soft),
            "reject": (cls.err, cls.err_soft),
            "rejected": (cls.err, cls.err_soft),
            "failed": (cls.err, cls.err_soft),
            "fail": (cls.err, cls.err_soft),
            "error": (cls.err, cls.err_soft),
            "cached": (cls.violet, cls.violet_soft),
            "idle": (cls.text_muted, cls.panel_alt),
            "skipped": (cls.text_muted, cls.panel_alt),
        }.get(name.lower(), (cls.text_dim, cls.panel_alt))


class Fonts:
    """The font ladder, resolved against what this machine actually has."""

    def __init__(self) -> None:
        families = set(tkfont.families())
        ui = next(
            (f for f in ("Segoe UI Variable Text", "Segoe UI", "Inter", "Helvetica Neue",
                         "Helvetica", "DejaVu Sans", "Arial") if f in families),
            "TkDefaultFont",
        )
        mono = next(
            (f for f in ("Cascadia Mono", "Cascadia Code", "JetBrains Mono", "Consolas",
                         "DejaVu Sans Mono", "Courier New") if f in families),
            "TkFixedFont",
        )
        self.ui = ui
        self.mono = mono
        self.h1 = (ui, 24, "bold")
        self.h2 = (ui, 17, "bold")
        self.h3 = (ui, 12, "bold")
        self.body = (ui, 10)
        self.body_bold = (ui, 10, "bold")
        self.small = (ui, 9)
        self.small_bold = (ui, 9, "bold")
        self.tiny = (ui, 8)
        self.tile = (ui, 24, "bold")
        self.tile_unit = (ui, 10)
        self.code = (mono, 9)
        self.code_small = (mono, 8)
        self.logo = (ui, 13, "bold")


def mix(color_a: str, color_b: str, ratio: float) -> str:
    """Blend two ``#rrggbb`` colours; ``ratio=0`` → a, ``ratio=1`` → b."""
    ratio = max(0.0, min(1.0, ratio))
    a = _rgb(color_a)
    b = _rgb(color_b)
    out = tuple(int(round(a[i] + (b[i] - a[i]) * ratio)) for i in range(3))
    return "#%02x%02x%02x" % out


def _rgb(color: str) -> tuple[int, int, int]:
    color = color.lstrip("#")
    if len(color) == 3:
        color = "".join(ch * 2 for ch in color)
    return int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16)


def enable_hidpi(root: tk.Misc) -> None:
    """Ask Windows for a per-monitor DPI aware process, then scale Tk to match.

    Without this the whole window renders blurry on a scaled display, which is
    the single most visible difference between "a script with buttons" and "an
    application".
    """
    if sys.platform != "win32":
        return
    try:  # pragma: no cover - platform specific
        import ctypes

        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor v2
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
        scale = ctypes.windll.shcore.GetScaleFactorForDevice(0) / 100.0
        if 0.5 < scale < 4.0:
            root.tk.call("tk", "scaling", max(1.0, 1.333 * scale))
    except Exception:
        pass


def apply_theme(root: tk.Misc) -> tuple[Palette, Fonts]:
    """Paint the whole application with the V6.fresh light workspace theme."""
    palette = Palette()
    fonts = Fonts()

    root.configure(bg=palette.bg)
    style = ttk.Style(root)
    try:
        style.theme_use("clam")  # the only built-in theme that honours colours
    except tk.TclError:  # pragma: no cover
        pass

    style.configure(".", background=palette.bg, foreground=palette.text,
                    fieldbackground=palette.panel, borderwidth=0, focuscolor=palette.accent)
    style.configure("TFrame", background=palette.bg)
    style.configure("Panel.TFrame", background=palette.panel)
    style.configure("Surface.TFrame", background=palette.surface)
    style.configure("Raised.TFrame", background=palette.raised)

    style.configure("TLabel", background=palette.bg, foreground=palette.text, font=fonts.body)
    style.configure("Dim.TLabel", background=palette.bg, foreground=palette.text_dim,
                    font=fonts.small)
    style.configure("Panel.TLabel", background=palette.panel, foreground=palette.text,
                    font=fonts.body)
    style.configure("PanelDim.TLabel", background=palette.panel, foreground=palette.text_muted,
                    font=fonts.small)
    style.configure("H1.TLabel", background=palette.bg, foreground=palette.text, font=fonts.h1)
    style.configure("H2.TLabel", background=palette.bg, foreground=palette.text, font=fonts.h2)
    style.configure("H3.TLabel", background=palette.bg, foreground=palette.text, font=fonts.h3)
    style.configure("Accent.TLabel", background=palette.bg, foreground=palette.accent,
                    font=fonts.body_bold)

    # ── buttons ──────────────────────────────────────────────────────────
    style.configure(
        "TButton",
        background=palette.raised,
        foreground=palette.text,
        borderwidth=0,
        focusthickness=0,
        focuscolor=palette.raised,
        padding=(12, 7),
        font=fonts.body,
    )
    style.map(
        "TButton",
        background=[("active", palette.border_strong), ("pressed", palette.border),
                    ("disabled", palette.panel)],
        foreground=[("disabled", palette.text_faint)],
    )
    style.configure("Accent.TButton", background=palette.accent, foreground="#ffffff",
                    font=fonts.body_bold, padding=(14, 8))
    style.map("Accent.TButton",
              background=[("active", "#48a2fa"), ("pressed", palette.accent_dim),
                          ("disabled", palette.panel_alt)],
              foreground=[("disabled", palette.text_faint)])
    style.configure("Ghost.TButton", background=palette.panel, foreground=palette.text_dim,
                    padding=(10, 6), font=fonts.small)
    style.map("Ghost.TButton",
              background=[("active", palette.panel_alt)],
              foreground=[("active", palette.text)])
    style.configure("Danger.TButton", background=palette.err_soft, foreground=palette.err,
                    font=fonts.small_bold, padding=(10, 6))
    style.map("Danger.TButton", background=[("active", "#4d1622")])

    # ── inputs ───────────────────────────────────────────────────────────
    style.configure(
        "TEntry",
        fieldbackground=palette.panel_alt,
        foreground=palette.text,
        insertcolor=palette.accent,
        bordercolor=palette.border,
        lightcolor=palette.border,
        darkcolor=palette.border,
        borderwidth=1,
        padding=7,
        font=fonts.body,
    )
    style.map("TEntry", bordercolor=[("focus", palette.accent_dim)])
    style.configure("TCombobox", fieldbackground=palette.panel_alt, background=palette.raised,
                    foreground=palette.text, arrowcolor=palette.text_dim, borderwidth=1,
                    padding=6, selectbackground=palette.panel_alt,
                    selectforeground=palette.text)
    style.map(
        "TCombobox",
        fieldbackground=[("readonly", palette.panel_alt), ("disabled", palette.panel)],
        foreground=[("readonly", palette.text), ("disabled", palette.text_faint)],
        selectbackground=[("readonly", palette.panel_alt)],
        selectforeground=[("readonly", palette.text)],
        background=[("active", palette.raised)],
        arrowcolor=[("active", palette.accent)],
    )
    style.configure("TCheckbutton", background=palette.bg, foreground=palette.text_dim,
                    font=fonts.small, focuscolor=palette.bg)
    style.map("TCheckbutton", background=[("active", palette.bg)],
              foreground=[("active", palette.text)])
    style.configure("Panel.TCheckbutton", background=palette.panel,
                    foreground=palette.text_dim, font=fonts.small)
    style.map("Panel.TCheckbutton", background=[("active", palette.panel)])

    # ── scrollbars ───────────────────────────────────────────────────────
    # The classic ``tk.Scrollbar`` is drawn by the native Windows element and
    # IGNORES bg/troughcolor — it shows up as a white bar in a dark window.  The
    # ttk scrollbar under the ``clam`` theme does honour colours, so it is styled
    # here once and used everywhere (see widgets.dark_scrollbar).
    for orient in ("Vertical", "Horizontal"):
        style.configure(
            f"Dark.{orient}.TScrollbar",
            background=palette.panel_alt,
            troughcolor=palette.surface,
            bordercolor=palette.surface,
            lightcolor=palette.surface,
            darkcolor=palette.surface,
            arrowcolor=palette.text_faint,
            gripcount=0,
            arrowsize=9,
            borderwidth=0,
            width=10,
        )
        style.map(
            f"Dark.{orient}.TScrollbar",
            background=[("active", palette.border_strong), ("pressed", palette.accent_dim)],
            arrowcolor=[("active", palette.accent)],
        )

    # ── separator / progress ─────────────────────────────────────────────
    style.configure("TSeparator", background=palette.border)
    style.configure("TProgressbar", background=palette.accent, troughcolor=palette.panel_alt,
                    borderwidth=0, thickness=4)

    # ── tree (tables) ────────────────────────────────────────────────────
    style.configure(
        "Treeview",
        background=palette.panel,
        fieldbackground=palette.panel,
        foreground=palette.text,
        borderwidth=0,
        rowheight=26,
        font=fonts.body,
    )
    style.configure("Treeview.Heading", background=palette.panel_alt,
                    foreground=palette.text_muted, font=fonts.small_bold,
                    borderwidth=0, padding=(6, 6))
    style.map("Treeview.Heading", background=[("active", palette.raised)])
    style.map("Treeview", background=[("selected", palette.select_bg)],
              foreground=[("selected", palette.text)])

    # ── notebook ─────────────────────────────────────────────────────────
    style.configure("TNotebook", background=palette.bg, borderwidth=0, tabmargins=(0, 0, 0, 0))
    style.configure("TNotebook.Tab", background=palette.surface, foreground=palette.text_muted,
                    padding=(14, 7), font=fonts.small, borderwidth=0)
    style.map("TNotebook.Tab",
              background=[("selected", palette.panel)],
              foreground=[("selected", palette.accent)])

    return palette, fonts
