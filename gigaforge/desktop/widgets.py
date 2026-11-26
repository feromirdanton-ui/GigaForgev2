"""Reusable widgets for the desktop window.

Tk's standard widget set is competent but plain; these are the pieces that make
the difference between a form and a control room:

* :class:`Card` — the bordered panel everything else sits in;
* :class:`StatTile` — one number, its label and an optional trend line;
* :class:`Badge` — a status pill whose colours come from
  :meth:`gigaforge.desktop.theme.Palette.status`, so the same word is always the
  same colour;
* :class:`ScrollFrame` — a vertically scrollable container that survives content
  being rebuilt (the dashboard refreshes into it);
* :class:`CodeView` — read-only monospace text with diff and log colouring;
* :class:`Sparkline` — a canvas trend line for the token/cost series;
* :class:`MeterBar` — a thin horizontal proportion bar for cost-by-phase.

Every widget takes the shared :class:`~gigaforge.desktop.theme.Fonts` so the type
scale cannot drift between views.
"""
from __future__ import annotations

import math
import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk
from typing import Any, Callable, Iterable, Optional, Sequence

from .theme import Fonts, Palette, mix

__all__ = [
    "Card",
    "SoftButton",
    "SegmentedControl",
    "SmartIcon",
    "StatTile",
    "Badge",
    "ScrollFrame",
    "CodeView",
    "Sparkline",
    "MeterBar",
    "NavButton",
    "key_value_grid",
    "dark_scrollbar",
    "hairline",
]


class Card(tk.Frame):
    """A softly rounded glass-like panel with an optional title row."""

    def __init__(
        self,
        master: tk.Misc,
        *,
        title: str = "",
        fonts: Fonts,
        palette: Palette = Palette,
        subtitle: str = "",
        pad: int = 14,
        bg: Optional[str] = None,
        actions: Optional[Sequence[tuple[str, Callable[[], None]]]] = None,
    ) -> None:
        try:
            parent_bg = str(master.cget("bg") or palette.bg)
        except tk.TclError:
            parent_bg = palette.bg
        super().__init__(master, bg=parent_bg, bd=0, highlightthickness=0)
        self.palette = palette
        self.fonts = fonts
        self._pad = pad
        self._surface = bg or palette.panel
        self._drop_target = False
        self._surface_canvas = tk.Canvas(
            self, bg=parent_bg, bd=0, highlightthickness=0, takefocus=False
        )
        self._surface_canvas.place(x=0, y=0, relwidth=1, relheight=1)
        self._surface_canvas.bind("<Configure>", self._draw_surface)
        self.body = tk.Frame(self, bg=self._surface, bd=0, highlightthickness=0)
        self.body.pack(fill="both", expand=True, padx=10, pady=10)

        self.header: Optional[tk.Frame] = None
        if title:
            self.header = tk.Frame(self.body, bg=self._surface)
            inner_pad = max(4, pad - 10)
            self.header.pack(fill="x", padx=inner_pad, pady=(inner_pad, 4))
            tk.Label(
                self.header, text=title.upper(), bg=self._surface,
                fg=palette.text_muted, font=fonts.small_bold, anchor="w",
            ).pack(side="left")
            if subtitle:
                tk.Label(
                    self.header, text=subtitle, bg=self._surface,
                    fg=palette.text_faint, font=fonts.tiny, anchor="w",
                ).pack(side="left", padx=(8, 0))
            for label, command in reversed(list(actions or ())):
                SoftButton(
                    self.header, label, command, fonts=fonts, palette=palette,
                ).pack(side="right", padx=(6, 0))

        self.content = tk.Frame(self.body, bg=self._surface)
        inner_pad = max(4, pad - 10)
        self.content.pack(fill="both", expand=True, padx=inner_pad, pady=(4, inner_pad))

    def _draw_surface(self, event: Any = None) -> None:
        canvas = self._surface_canvas
        canvas.delete("all")
        width = max(2, int(getattr(event, "width", self.winfo_width())))
        height = max(2, int(getattr(event, "height", self.winfo_height())))
        radius = min(24, max(12, min(width, height) // 6))
        canvas.create_polygon(
            *_rounded_rect_points(2, 3, width - 3, height - 5, radius),
            fill=self.palette.shadow, outline="", smooth=True, splinesteps=20,
        )
        canvas.create_polygon(
            *_rounded_rect_points(1, 0, width - 3, height - 4, radius),
            fill=self._surface,
            outline=self.palette.accent if self._drop_target else self.palette.border,
            width=2 if self._drop_target else 1,
            smooth=True, splinesteps=20,
        )
        if height > 42:
            canvas.create_line(radius + 8, 1, width - radius - 8, 1,
                               fill=mix(self._surface, "#ffffff", 0.72), width=1)

    def set_drop_target(self, active: bool) -> None:
        if self._drop_target == bool(active):
            return
        self._drop_target = bool(active)
        self._draw_surface()


def _rounded_rect_points(x: float, y: float, width: float, height: float,
                         radius: float) -> list[float]:
    radius = max(0.0, min(radius, width / 2, height / 2))
    corners = (
        (x + width - radius, y + radius, -90),
        (x + width - radius, y + height - radius, 0),
        (x + radius, y + height - radius, 90),
        (x + radius, y + radius, 180),
    )
    points: list[float] = []
    for cx, cy, start in corners:
        for step in range(7):
            angle = (start + step * 15) * 3.141592653589793 / 180
            points.extend((cx + radius * math.cos(angle), cy + radius * math.sin(angle)))
    return points


class SoftButton(tk.Canvas):
    """A keyboard accessible, softly rounded button drawn with the shared palette."""

    def __init__(self, master: tk.Misc, text: str, command: Callable[[], None], *,
                 fonts: Fonts, primary: bool = False, palette: Palette = Palette,
                 height: int = 36, font: Any = None) -> None:
        try:
            parent_bg = str(master.cget("bg") or palette.panel)
        except tk.TclError:
            parent_bg = palette.panel
        self._font = font or fonts.small_bold
        self._measure = tkfont.Font(root=master.winfo_toplevel(), font=self._font)
        width = max(64, self._measure.measure(text) + 24)
        height = max(24, int(height))
        super().__init__(master, width=width, height=height, bg=parent_bg,
                         bd=0, highlightthickness=0, cursor="hand2", takefocus=True)
        self.palette = palette
        self.text = text
        self.command = command
        self.primary = primary
        self._button_state = "normal"
        self._hovered = False
        self._pressed = False
        self.bind("<Configure>", self._draw)
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Return>", self._on_key)
        self.bind("<space>", self._on_key)
        self._draw()

    def configure(self, cnf: Any = None, **kwargs: Any) -> Any:
        state = kwargs.pop("state", None)
        if isinstance(cnf, dict) and "state" in cnf:
            options = dict(cnf)
            state = options.pop("state")
            cnf = options
        if state is not None:
            self._button_state = str(state)
        result = super().configure(cnf, **kwargs)
        if hasattr(self, "palette"):
            self._draw()
        return result

    config = configure

    def set_primary(self, primary: bool) -> None:
        self.primary = bool(primary)
        self._draw()

    def set_text(self, text: str) -> None:
        self.text = text
        self.configure(width=max(64, self._measure.measure(text) + 24))
        self._draw()

    def _draw(self, _event: Any = None) -> None:
        if not hasattr(self, "palette"):
            return
        width, height = max(2, self.winfo_width()), max(2, self.winfo_height())
        disabled = self._button_state == "disabled"
        pressed = self._pressed and not disabled
        if disabled:
            fill, foreground = self.palette.panel_alt, self.palette.text_faint
        elif self.primary:
            fill = self.palette.accent_dim if pressed else self.palette.accent
            foreground = "#ffffff"
        else:
            fill = self.palette.raised if self._hovered or pressed else self.palette.panel
            foreground = self.palette.text
        self.delete("all")
        if not disabled and not pressed:
            self.create_polygon(
                *_rounded_rect_points(1, 3, width - 3, height - 5, height / 2),
                fill=self.palette.shadow, outline="", smooth=True, splinesteps=16,
            )
        self.create_polygon(
            *_rounded_rect_points(1, 0, width - 3, height - 5, height / 2),
            fill=fill, outline=self.palette.border_strong if not self.primary else "",
            width=1, smooth=True, splinesteps=16,
        )
        self.create_text(width / 2, (height - 4) / 2, text=self.text,
                         fill=foreground, font=self._font)

    def _on_enter(self, _event: Any = None) -> None:
        self._hovered = True
        self._draw()

    def _on_leave(self, _event: Any = None) -> None:
        self._hovered = False
        self._pressed = False
        self._draw()

    def _on_press(self, _event: Any = None) -> None:
        if self._button_state == "disabled":
            return
        self._pressed = True
        self.focus_set()
        self._draw()

    def _on_release(self, event: Any) -> None:
        should_invoke = (
            self._pressed and self._button_state != "disabled"
            and 0 <= event.x <= self.winfo_width()
            and 0 <= event.y <= self.winfo_height()
        )
        self._pressed = False
        self._draw()
        if should_invoke:
            self.command()

    def _on_key(self, _event: Any = None) -> str:
        if self._button_state != "disabled":
            self.command()
        return "break"


class SegmentedControl(tk.Canvas):
    """A compact, icon-led mode switch with one continuous glass surface."""

    _TONES = (Palette.accent, Palette.violet, Palette.ok, Palette.blue)

    def __init__(
        self,
        master: tk.Misc,
        options: Sequence[tuple[str, str, str]],
        command: Callable[[str], None],
        *,
        fonts: Fonts,
        selected: str,
        palette: Palette = Palette,
        height: int = 38,
    ) -> None:
        try:
            parent_bg = str(master.cget("bg") or palette.panel)
        except tk.TclError:
            parent_bg = palette.panel
        self.palette = palette
        self.fonts = fonts
        self.options = tuple(options)
        self.command = command
        self.selected = selected
        self.height = max(32, int(height))
        self._font = fonts.small_bold
        self._measure = tkfont.Font(root=master.winfo_toplevel(), font=self._font)
        self._widths = [
            max(76, self._measure.measure(label) + 44)
            for _key, label, _glyph in self.options
        ]
        width = sum(self._widths) + 8
        super().__init__(master, width=width, height=self.height,
                         bg=parent_bg, bd=0, highlightthickness=0,
                         takefocus=True, cursor="hand2")
        self._hover_index: int | None = None
        self.bind("<Configure>", self._draw)
        self.bind("<Button-1>", self._on_click)
        self.bind("<Motion>", self._on_motion)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Left>", lambda _event: self._step(-1))
        self.bind("<Right>", lambda _event: self._step(1))
        self.bind("<space>", self._on_activate)
        self.bind("<Return>", self._on_activate)
        self._draw()

    def set_selected(self, selected: str) -> None:
        if selected not in {item[0] for item in self.options}:
            return
        self.selected = selected
        self._draw()

    def _segment_at(self, x: int) -> int | None:
        offset = 4
        for index, width in enumerate(self._widths):
            if offset <= x < offset + width:
                return index
            offset += width
        return None

    def _select_index(self, index: int) -> None:
        if not self.options:
            return
        key = self.options[index % len(self.options)][0]
        if key != self.selected:
            self.selected = key
            self._draw()
            self.command(key)

    def _on_click(self, event: Any) -> str:
        index = self._segment_at(int(event.x))
        if index is not None:
            self.focus_set()
            self._select_index(index)
        return "break"

    def _on_motion(self, event: Any) -> None:
        index = self._segment_at(int(event.x))
        if index != self._hover_index:
            self._hover_index = index
            self._draw()

    def _on_leave(self, _event: Any = None) -> None:
        self._hover_index = None
        self._draw()

    def _step(self, direction: int) -> str:
        index = next((i for i, item in enumerate(self.options)
                      if item[0] == self.selected), 0)
        self._select_index(index + direction)
        return "break"

    def _on_activate(self, _event: Any = None) -> str:
        index = next((i for i, item in enumerate(self.options)
                      if item[0] == self.selected), 0)
        self._select_index(index)
        return "break"

    def _draw(self, _event: Any = None) -> None:
        if not hasattr(self, "palette"):
            return
        width = max(sum(self._widths) + 8, self.winfo_width())
        height = max(self.height, self.winfo_height())
        self.delete("all")
        self.create_polygon(
            *_rounded_rect_points(1, 1, width - 2, height - 3, height / 2),
            fill=self.palette.panel_alt, outline=self.palette.border,
            width=1, smooth=True, splinesteps=18,
        )
        offset = 4
        for index, ((key, label, glyph), segment_width) in enumerate(
            zip(self.options, self._widths, strict=False)
        ):
            is_selected = key == self.selected
            hovered = index == self._hover_index and not is_selected
            if is_selected:
                self.create_polygon(
                    *_rounded_rect_points(offset, 4, segment_width, height - 7,
                                         max(12, height / 2 - 3)),
                    fill=self.palette.shadow, outline="", smooth=True,
                    splinesteps=16,
                )
                self.create_polygon(
                    *_rounded_rect_points(offset, 2, segment_width, height - 8,
                                         max(12, height / 2 - 4)),
                    fill=self.palette.surface,
                    outline=self.palette.border, width=1,
                    smooth=True, splinesteps=16,
                )
            elif hovered:
                self.create_polygon(
                    *_rounded_rect_points(offset + 1, 3, segment_width - 2,
                                         height - 9, max(10, height / 2 - 5)),
                    fill=self.palette.raised, outline="", smooth=True,
                    splinesteps=16,
                )
            tone = self._TONES[index % len(self._TONES)]
            circle_x = offset + 18
            circle_y = height / 2
            circle_fill = mix(self.palette.surface, tone, 0.14 if is_selected else 0.08)
            self.create_oval(circle_x - 9, circle_y - 9, circle_x + 9, circle_y + 9,
                             fill=circle_fill, outline="")
            self.create_text(circle_x, circle_y, text=glyph, fill=tone,
                             font=(self.fonts.ui, 10, "bold"))
            foreground = self.palette.text if is_selected else self.palette.text_muted
            self.create_text(offset + 34, circle_y, text=label, anchor="w",
                             fill=foreground, font=self._font)
            offset += segment_width


class SmartIcon(tk.Canvas):
    """A compact circular icon token with a soft ring and a legible glyph."""

    def __init__(self, master: tk.Misc, glyph: str, *, size: int = 36,
                 tone: str = Palette.accent, fill: str = Palette.accent_soft,
                 font: Any = None, palette: Palette = Palette) -> None:
        try:
            parent_bg = str(master.cget("bg") or palette.panel)
        except tk.TclError:
            parent_bg = palette.panel
        super().__init__(master, width=size, height=size, bg=parent_bg,
                         bd=0, highlightthickness=0, takefocus=False)
        self.size = size
        self.glyph = glyph
        self.tone = tone
        self.fill = fill
        self.palette = palette
        self.glyph_font = font or ("Segoe UI Symbol", max(10, size // 2), "bold")
        self.bind("<Configure>", self._draw)

    def set_tone(self, tone: str, fill: str) -> None:
        self.tone, self.fill = tone, fill
        self._draw()

    def _draw(self, _event: Any = None) -> None:
        size = min(self.winfo_width() or self.size, self.winfo_height() or self.size)
        inset = max(2, size * 0.08)
        self.delete("all")
        self.create_oval(inset, inset + 1, size - inset, size - inset + 1,
                         fill=self.palette.shadow, outline="")
        self.create_oval(inset, inset, size - inset, size - inset,
                         fill=self.fill, outline=mix(self.fill, self.tone, 0.22), width=1)
        self.create_text(size / 2, size / 2, text=self.glyph,
                         fill=self.tone, font=self.glyph_font)


class Badge(tk.Canvas):
    """A rounded semantic status pill whose tint is tied to runtime state."""

    def __init__(
        self,
        master: tk.Misc,
        text: str = "",
        *,
        fonts: Fonts,
        status: str = "idle",
        palette: Palette = Palette,
        padx: int = 9,
        pady: int = 3,
    ) -> None:
        try:
            parent_bg = str(master.cget("bg") or palette.surface)
        except tk.TclError:
            parent_bg = palette.surface
        self.palette = palette
        self.fonts = fonts
        self._padx = padx
        self._pady = pady
        self._text = text.upper()
        self._status = status
        self._measure = tkfont.Font(root=master.winfo_toplevel(), font=fonts.tiny)
        width = self._measure.measure(self._text) + 2 * padx
        height = max(20, int(self._measure.metrics("linespace") + 2 * pady))
        super().__init__(master, width=width, height=height, bg=parent_bg,
                         bd=0, highlightthickness=0, takefocus=False)
        self.bind("<Configure>", self._draw)
        self._draw()

    def set(self, text: str, status: str = "idle", palette: Palette = Palette) -> None:
        self._text = text.upper()
        self._status = status
        self.palette = palette
        self.configure(width=self._measure.measure(self._text) + 2 * self._padx)
        self._draw()

    def _draw(self, _event: Any = None) -> None:
        width, height = max(2, self.winfo_width()), max(2, self.winfo_height())
        foreground, background = self.palette.status(self._status)
        self.delete("all")
        self.create_polygon(
            *_rounded_rect_points(1, 1, width - 2, height - 2, height / 2),
            fill=background, outline="", smooth=True, splinesteps=16,
        )
        self.create_text(width / 2, height / 2, text=self._text,
                         fill=foreground, font=self.fonts.tiny)


class StatTile(tk.Frame):
    """A KPI: label, large value, small unit — plus an optional delta line."""

    def __init__(
        self,
        master: tk.Misc,
        *,
        label: str,
        value: str = "—",
        unit: str = "",
        hint: str = "",
        fonts: Fonts,
        palette: Palette = Palette,
        accent: Optional[str] = None,
    ) -> None:
        super().__init__(master, bg=palette.panel)
        self.palette = palette
        self.fonts = fonts
        self._accent = accent or palette.accent

        tk.Label(self, text=label.upper(), bg=palette.panel, fg=palette.text_muted,
                 font=fonts.tiny, anchor="w").pack(fill="x")
        row = tk.Frame(self, bg=palette.panel)
        row.pack(fill="x", pady=(2, 0))
        self._value = tk.Label(row, text=value, bg=palette.panel, fg=self._accent,
                               font=fonts.tile, anchor="w")
        self._value.pack(side="left")
        self._unit = tk.Label(row, text=unit, bg=palette.panel, fg=palette.text_muted,
                              font=fonts.tile_unit, anchor="w")
        self._unit.pack(side="left", padx=(4, 0), pady=(8, 0))
        self._hint = tk.Label(self, text=hint, bg=palette.panel, fg=palette.text_faint,
                              font=fonts.tiny, anchor="w")
        self._hint.pack(fill="x")

    def set(self, value: str, *, unit: Optional[str] = None, hint: Optional[str] = None,
            colour: Optional[str] = None) -> None:
        self._value.configure(text=value, fg=colour or self._accent)
        if unit is not None:
            self._unit.configure(text=unit)
        if hint is not None:
            self._hint.configure(text=hint)


class ScrollFrame(tk.Frame):
    """A frame whose content scrolls vertically.

    ``.inner`` is the frame to add children to.  The canvas width is bound to the
    frame width so content never sits in a narrow column with dead space beside
    it — the usual failure mode of a canvas-based scroller.
    """

    def __init__(self, master: tk.Misc, *, bg: str = Palette.bg, palette: Palette = Palette):
        super().__init__(master, bg=bg, bd=0, highlightthickness=0)
        self.canvas = tk.Canvas(self, bg=bg, bd=0, highlightthickness=0)
        self.scroll = dark_scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.inner = tk.Frame(self.canvas, bg=bg, bd=0, highlightthickness=0)
        self._window = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=self.scroll.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.scroll.pack(side="right", fill="y")

        self.inner.bind("<Configure>", self._on_inner)
        self.canvas.bind("<Configure>", self._on_canvas)
        self.canvas.bind("<Enter>", self._bind_wheel)
        self.canvas.bind("<Leave>", self._unbind_wheel)

    def _on_inner(self, _event: Any) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas(self, event: Any) -> None:
        self.canvas.itemconfigure(self._window, width=event.width)

    def _bind_wheel(self, _event: Any) -> None:
        self.canvas.bind_all("<MouseWheel>", self._wheel)

    def _unbind_wheel(self, _event: Any) -> None:
        self.canvas.unbind_all("<MouseWheel>")

    def _wheel(self, event: Any) -> None:
        delta = -1 * int(event.delta / 120) if event.delta else 0
        if delta:
            self.canvas.yview_scroll(delta, "units")

    def clear(self) -> None:
        for child in self.inner.winfo_children():
            child.destroy()

    def to_top(self) -> None:
        self.canvas.yview_moveto(0.0)

    def to_bottom(self) -> None:
        self.canvas.yview_moveto(1.0)


class CodeView(tk.Frame):
    """Read-only monospace viewer with diff/log colouring."""

    def __init__(
        self,
        master: tk.Misc,
        *,
        fonts: Fonts,
        palette: Palette = Palette,
        height: int = 12,
        wrap: bool = False,
    ) -> None:
        super().__init__(master, bg=palette.border, bd=0, highlightthickness=0)
        self.palette = palette
        self.fonts = fonts
        self.text = tk.Text(
            self, bg=palette.surface, fg=palette.text_dim, bd=0, highlightthickness=0,
            font=fonts.code, wrap="word" if wrap else "none", height=height,
            insertbackground=palette.accent, padx=10, pady=8,
            selectbackground=palette.select_bg, selectforeground=palette.text,
        )
        bar = dark_scrollbar(self, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=bar.set)
        self.text.pack(side="left", fill="both", expand=True, padx=1, pady=1)
        bar.pack(side="right", fill="y")

        self.text.tag_configure("add", foreground=palette.diff_add_fg,
                                background=palette.diff_add_bg)
        self.text.tag_configure("del", foreground=palette.diff_del_fg,
                                background=palette.diff_del_bg)
        self.text.tag_configure("hunk", foreground=palette.diff_hunk)
        self.text.tag_configure("meta", foreground=palette.diff_meta)
        self.text.tag_configure("text", foreground=palette.text_dim)
        self.text.tag_configure("strong", foreground=palette.text)
        self.text.tag_configure("ok", foreground=palette.ok)
        self.text.tag_configure("err", foreground=palette.err)
        self.text.tag_configure("warn", foreground=palette.warn)
        self.text.tag_configure("muted", foreground=palette.text_faint)
        self.text.configure(state="disabled")

    # ── content ───────────────────────────────────────────────────────────
    def set_diff(self, diff: str, *, empty: str = "no diff for this run") -> None:
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        if not diff.strip():
            self._insert(empty + "\n", "muted")
        else:
            for line in diff.splitlines():
                tag = "text"
                if line.startswith(("+++", "---", "diff ", "index ")):
                    tag = "meta"
                elif line.startswith(("@@",)):
                    tag = "hunk"
                elif line.startswith("+"):
                    tag = "add"
                elif line.startswith("-"):
                    tag = "del"
                self._insert(line + "\n", tag)
        self.text.configure(state="disabled")
        self.text.yview_moveto(0.0)

    def set_lines(self, lines: Iterable[tuple[str, str]], *, empty: str = "nothing to show") -> None:
        """``lines`` is an iterable of ``(text, tag)``."""
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        wrote = False
        for text, tag in lines:
            wrote = True
            self._insert(text + "\n", tag or "text")
        if not wrote:
            self._insert(empty + "\n", "muted")
        self.text.configure(state="disabled")
        self.text.yview_moveto(0.0)

    def _insert(self, text: str, tag: str) -> None:
        self.text.insert("end", text, tag)

    def append(self, text: str, tag: str = "text") -> None:
        self.text.configure(state="normal")
        self._insert(text + "\n", tag)
        self.text.configure(state="disabled")
        self.text.see("end")


class Sparkline(tk.Canvas):
    """A minimal trend line — enough to see shape, no chart library."""

    def __init__(
        self,
        master: tk.Misc,
        *,
        palette: Palette = Palette,
        height: int = 56,
        colour: Optional[str] = None,
    ) -> None:
        super().__init__(master, bg=palette.surface, bd=0, highlightthickness=0,
                         height=height)
        self.palette = palette
        self.colour = colour or palette.accent
        self._values: list[float] = []
        self.bind("<Configure>", lambda _e: self._redraw())

    def set_values(self, values: Sequence[float]) -> None:
        self._values = [float(v) for v in values][-160:]
        self._redraw()

    def _redraw(self) -> None:
        self.delete("all")
        width = max(1, self.winfo_width())
        height = max(1, self.winfo_height())
        if len(self._values) < 2:
            self.create_text(
                width // 2, height // 2, text="no series yet",
                fill=self.palette.text_faint, font=("Segoe UI", 8),
            )
            return
        lo, hi = min(self._values), max(self._values)
        if hi - lo < 1e-9:
            hi = lo + 1.0
        pad = 4
        points: list[float] = []
        step = (width - 2 * pad) / (len(self._values) - 1)
        for i, value in enumerate(self._values):
            x = pad + i * step
            y = height - pad - (value - lo) / (hi - lo) * (height - 2 * pad)
            points.extend((x, y))
        # filled area under the line, then the line itself
        area = points + [width - pad, height - pad, pad, height - pad]
        self.create_polygon(area, fill=mix(self.palette.surface, self.colour, 0.14),
                            outline="")
        self.create_line(*points, fill=self.colour, width=2, smooth=True)
        self.create_text(
            6, 6, anchor="nw", text=f"{int(max(self._values)):,}",
            fill=self.palette.text_muted, font=("Segoe UI", 8),
        )


class MeterBar(tk.Canvas):
    """A thin proportion bar with a label and value at each end."""

    def __init__(self, master: tk.Misc, *, palette: Palette = Palette, height: int = 18,
                 colour: Optional[str] = None) -> None:
        super().__init__(master, bg=palette.panel, bd=0, highlightthickness=0, height=height)
        self.palette = palette
        self.colour = colour or palette.accent
        self._fraction = 0.0
        self.bind("<Configure>", lambda _e: self._redraw())

    def set_fraction(self, fraction: float, colour: Optional[str] = None) -> None:
        self._fraction = max(0.0, min(1.0, float(fraction)))
        if colour:
            self.colour = colour
        self._redraw()

    def _redraw(self) -> None:
        self.delete("all")
        width = max(1, self.winfo_width())
        height = max(1, self.winfo_height())
        radius = height - 6
        self.create_rectangle(0, 3, width, 3 + radius, fill=self.palette.panel_alt, outline="")
        filled = max(0, int(width * self._fraction))
        if filled:
            self.create_rectangle(0, 3, filled, 3 + radius, fill=self.colour, outline="")


class NavButton(tk.Frame):
    """A softly highlighted navigation row with a circular smart icon."""

    def __init__(
        self,
        master: tk.Misc,
        *,
        glyph: str,
        label: str,
        command: Callable[[], None],
        fonts: Fonts,
        palette: Palette = Palette,
        hint: str = "",
    ) -> None:
        super().__init__(master, bg=palette.surface, cursor="hand2")
        self.palette = palette
        self.fonts = fonts
        self.command = command
        self._active = False
        self._hovered = False
        self._background = tk.Canvas(self, bg=palette.surface, bd=0,
                                     highlightthickness=0, takefocus=False)
        self._background.place(x=0, y=0, relwidth=1, relheight=1)
        self._background.bind("<Configure>", self._draw_background)
        self._content = tk.Frame(self, bg=palette.surface)
        self._content.pack(fill="both", expand=True, padx=10, pady=3)
        self._glyph = SmartIcon(
            self._content, glyph, size=30, tone=palette.text_muted,
            fill=palette.panel_alt, font=(fonts.mono[0], 12), palette=palette,
        )
        self._glyph.pack(side="left", padx=(2, 10), pady=3)
        text_col = tk.Frame(self._content, bg=palette.surface)
        text_col.pack(side="left", fill="x", expand=True, pady=8)
        self._label = tk.Label(text_col, text=label, bg=palette.surface, fg=palette.text_dim,
                               font=fonts.body, anchor="w")
        self._label.pack(fill="x")
        if hint:
            self._hint = tk.Label(text_col, text=hint, bg=palette.surface,
                                  fg=palette.text_faint, font=fonts.tiny, anchor="w")
            self._hint.pack(fill="x")

        for widget in (self, self._background, self._content, self._glyph, self._label, text_col):
            widget.bind("<Button-1>", self._click)
            widget.bind("<Enter>", self._enter)
            widget.bind("<Leave>", self._leave)
        self.set_active(False)

    def _click(self, _event: Any) -> None:
        self.command()

    def _enter(self, _event: Any) -> None:
        self._hovered = True
        self._update_style()

    def _leave(self, _event: Any) -> None:
        self._hovered = False
        self._update_style()

    def set_active(self, active: bool) -> None:
        self._active = bool(active)
        self._update_style()

    def _update_style(self) -> None:
        active = self._active
        bg = self.palette.accent_soft if active else (
            self.palette.panel_alt if self._hovered else self.palette.surface
        )
        self.configure(bg=self.palette.surface)
        self._content.configure(bg=bg)
        self._label.configure(bg=bg, fg=self.palette.text if active else self.palette.text_dim,
                              font=self.fonts.body_bold if active else self.fonts.body)
        self._glyph.configure(bg=bg)
        self._glyph.set_tone(self.palette.accent_ink if active else self.palette.text_muted,
                             self.palette.accent_soft if active else self.palette.panel_alt)
        for widget in self._content.winfo_children():
            if isinstance(widget, tk.Frame):
                widget.configure(bg=bg)
                for child in widget.winfo_children():
                    if isinstance(child, tk.Label):
                        child.configure(bg=bg)
        self._draw_background()

    def _draw_background(self, _event: Any = None) -> None:
        canvas = self._background
        canvas.delete("all")
        if not (self._active or self._hovered):
            return
        width, height = max(2, self.winfo_width()), max(2, self.winfo_height())
        fill = self.palette.accent_soft if self._active else self.palette.panel_alt
        canvas.create_polygon(
            *_rounded_rect_points(2, 1, width - 4, height - 2, min(16, height / 2)),
            fill=fill, outline="", smooth=True, splinesteps=16,
        )


def key_value_grid(
    master: tk.Misc,
    rows: Sequence[tuple[str, str]],
    *,
    fonts: Fonts,
    palette: Palette = Palette,
    bg: str = Palette.panel,
    value_colours: Optional[dict[str, str]] = None,
    value_wrap: int = 460,
) -> tk.Frame:
    """A two-column key/value block — the compact way to show configuration."""
    frame = tk.Frame(master, bg=bg)
    for index, (key, value) in enumerate(rows):
        tk.Label(frame, text=key, bg=bg, fg=palette.text_muted, font=fonts.small,
                 anchor="w").grid(row=index, column=0, sticky="w", padx=(0, 14), pady=2)
        colour = (value_colours or {}).get(key, palette.text)
        tk.Label(frame, text=value, bg=bg, fg=colour, font=fonts.small_bold,
                 anchor="w", justify="left", wraplength=value_wrap).grid(
            row=index, column=1, sticky="w", pady=2
        )
    frame.grid_columnconfigure(1, weight=1)
    return frame


def dark_scrollbar(
    master: tk.Misc,
    *,
    orient: str = "vertical",
    command: Any = None,
    palette: Palette = Palette,
) -> ttk.Scrollbar:
    """A scrollbar that survives a dark theme.

    ``tk.Scrollbar`` is rendered by the native Windows element and ignores both
    ``bg`` and ``troughcolor`` — it appears as a bright white bar in a dark window
    (verified by rendering both side by side).  The ttk scrollbar under ``clam``
    does honour colours, so the theme defines ``Dark.Vertical.TScrollbar`` and
    every scrolling surface asks for it through this helper.
    """
    name = "Vertical" if orient == "vertical" else "Horizontal"
    return ttk.Scrollbar(master, orient=orient, command=command,
                         style=f"Dark.{name}.TScrollbar")


def hairline(master: tk.Misc, *, palette: Palette = Palette) -> tk.Frame:
    line = tk.Frame(master, bg=palette.border, height=1)
    line.pack(fill="x")
    return line
