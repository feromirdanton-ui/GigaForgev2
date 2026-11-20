"""V6.fresh product pages: an operational dashboard and data-backed catalogs."""
from __future__ import annotations

import base64
import hashlib
import math
import os
import queue
import subprocess
import tempfile
import threading
import tkinter as tk
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any

from ...workspace_paths import (
    SENSITIVE_WORKSPACE_DIRECTORIES,
    is_sensitive_workspace_path,
)
from ..preferences import load_preferences, save_preferences
from ..theme import Palette, mix
from ..widgets import Badge, Card, ScrollFrame, SegmentedControl, SmartIcon, SoftButton, StatTile

__all__ = ["DashboardView", "FileWorkspacePage", "ProductPage", "SCENARIOS"]


SCENARIOS: tuple[dict[str, str], ...] = (
    {
        "title": "Аудит проекта",
        "category": "АНАЛИЗ",
        "description": "Найти узкие места, ошибки и приоритетные улучшения с привязкой к файлам.",
        "prompt": "Проведи аудит проекта: изучи архитектуру, найди конкретные риски и предложи улучшения с приоритетами.",
        "icon": "⌕",
    },
    {
        "title": "Создать веб-приложение",
        "category": "РАЗРАБОТКА",
        "description": "Спроектировать интерфейс, реализовать функции и пройти доступные проверки.",
        "prompt": "Изучи проект и создай готовое к запуску веб-приложение с продуманным адаптивным интерфейсом. Реализуй функциональность, проверь результат и исправь обнаруженные проблемы.",
        "icon": "✦",
    },
    {
        "title": "Написать тесты",
        "category": "КАЧЕСТВО",
        "description": "Покрыть важные пользовательские потоки и проверить критические случаи.",
        "prompt": "Изучи архитектуру проекта и добавь полезные тесты для ключевых сценариев и граничных случаев. Запусти подходящие проверки и устрани выявленные ошибки.",
        "icon": "✓",
    },
    {
        "title": "Документация проекта",
        "category": "ДОКУМЕНТАЦИЯ",
        "description": "Собрать актуальные инструкции, карту компонентов и сценарии запуска.",
        "prompt": "Изучи проект и обнови документацию: объясни архитектуру, установку, запуск, конфигурацию и основные рабочие сценарии.",
        "icon": "▤",
    },
)


def _safe_rows(value: Any) -> list[dict[str, Any]]:
    try:
        return [dict(item) for item in (value or []) if isinstance(item, Mapping)]
    except Exception:  # noqa: BLE001 - a catalog should never prevent the UI from opening
        return []


def _display(value: Any, fallback: str = "—") -> str:
    text = str(value or "").strip()
    return text if text else fallback


def _card(parent: tk.Misc, title: str = "", subtitle: str = "", *, pad: int = 14,
          bg: str | None = None) -> Card:
    return Card(parent, title=title, subtitle=subtitle, fonts=parent.winfo_toplevel().fonts,
                pad=pad, bg=bg)


def _action_button(
    parent: tk.Misc, label: str, command: Callable[[], None], *, primary: bool = False
) -> SoftButton:
    return SoftButton(
        parent, label, command, fonts=parent.winfo_toplevel().fonts,
        primary=primary,
    )


class DashboardView(tk.Frame):
    """The home page: real runtime state, task entry, agent catalog and run history."""

    def __init__(
        self,
        master: tk.Misc,
        app: Any,
        *,
        navigate: Callable[[str], None],
        start_task: Callable[[str, str], None],
    ) -> None:
        super().__init__(master, bg=Palette.bg, bd=0, highlightthickness=0)
        self.app = app
        self.ctx = app.ctx
        self.navigate = navigate
        self.start_task = start_task
        self._mode = "goal"
        default_order = ["hero", "actions", "workspace"]
        saved_order = load_preferences().get("dashboard_order", default_order)
        if not isinstance(saved_order, (list, tuple)):
            saved_order = []
        self._layout_order = list(dict.fromkeys(
            [key for key in saved_order if isinstance(key, str) and key in default_order]
            + default_order
        ))
        self._dragging_block: str | None = None
        self._drag_target: str | None = None
        self._drag_ghost: tk.Toplevel | None = None
        self._logo: tk.PhotoImage | None = None
        self._small_logo: tk.PhotoImage | None = None
        self._wash: tk.PhotoImage | None = None
        self._load_logo()

        self.composer_host = tk.Frame(self, bg=Palette.bg, height=156,
                                      bd=0, highlightthickness=0)
        self.composer_host.pack(side="bottom", fill="x")
        self.composer_host.pack_propagate(False)
        self._build_composer(self.composer_host)

        self.scroller = ScrollFrame(self, bg=Palette.bg)
        self.scroller.pack(fill="both", expand=True)
        self.body = self.scroller.inner
        self._build()

    def _load_logo(self) -> None:
        asset_dir = Path(__file__).resolve().parents[2] / "assets"
        wash_path = asset_dir / "watercolor-wash.png"
        logo_path = asset_dir / "gigaforge-mark.png"

        # Tk's Tcl image loader can fail on Windows absolute paths (including
        # normalized paths with forward slashes). Read the packaged PNG bytes
        # through Python and hand Tk a base64 payload instead.
        try:
            if wash_path.is_file():
                wash = tk.PhotoImage(
                    master=self, data=base64.b64encode(wash_path.read_bytes()).decode("ascii")
                )
                self._wash = wash
        except (tk.TclError, OSError):
            self._wash = None

        try:
            if logo_path.is_file():
                logo = tk.PhotoImage(
                    master=self, data=base64.b64encode(logo_path.read_bytes()).decode("ascii")
                )
                if logo.width() > 144:
                    logo = logo.subsample((logo.width() + 143) // 144)
                self._logo = logo
                self._small_logo = logo.subsample(max(1, (logo.width() + 55) // 56))
        except (tk.TclError, OSError):
            self._logo = self._small_logo = None

    def refresh(self) -> None:
        draft = ""
        mode = self._mode
        if hasattr(self, "entry"):
            candidate = self.entry.get("1.0", "end").strip()
            if candidate and candidate != getattr(self, "_placeholder", ""):
                draft = candidate
        for child in self.body.winfo_children():
            child.destroy()
        self._mode = mode
        self._build()
        self.set_mode(mode)
        if draft:
            self.entry.delete("1.0", "end")
            self.entry.insert("1.0", draft)
            self.entry.configure(fg=Palette.text)
        self.scroller.to_top()

    def _build(self) -> None:
        fonts = self.app.fonts
        root = self.body
        root.configure(padx=28, pady=14)
        root.grid_columnconfigure(0, weight=3, uniform="dashboard")
        root.grid_columnconfigure(1, weight=1, uniform="dashboard")
        root.grid_rowconfigure(1, weight=1)
        root.grid_rowconfigure(2, weight=1)
        backdrop = tk.Canvas(root, bg=Palette.bg, bd=0, highlightthickness=0,
                             takefocus=False)
        backdrop.place(x=0, y=0, relwidth=1, relheight=1)

        def draw_backdrop(event: Any = None) -> None:
            backdrop.delete("all")
            if self._wash is None:
                return
            width = int(getattr(event, "width", backdrop.winfo_width()) or 1)
            height = int(getattr(event, "height", backdrop.winfo_height()) or 1)
            backdrop.create_image(width / 2, height / 2, image=self._wash)

        backdrop.bind("<Configure>", draw_backdrop)

        header = tk.Frame(root, bg=Palette.bg, height=94)
        header.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 12))
        title_row = tk.Frame(header, bg=Palette.bg)
        title_row.pack(anchor="w", pady=(4, 3))
        tk.Label(title_row, text="Собирайте агентов.", bg=Palette.bg,
                 fg=Palette.text, font=fonts.h1).pack(side="left")
        tk.Label(title_row, text=" Создавайте результат.", bg=Palette.bg,
                 fg=Palette.accent_dim, font=fonts.h1).pack(side="left")
        tk.Label(header,
                 text="Одна рабочая среда для постановки задач, разработки и проверки результата.",
                 bg=Palette.bg, fg=Palette.text_dim, font=fonts.body).pack(anchor="w")

        hero = _card(root, pad=20, bg=Palette.panel)
        hero.content.grid_columnconfigure(0, weight=11, uniform="hero")
        hero.content.grid_columnconfigure(1, weight=9, uniform="hero")
        hero.content.grid_rowconfigure(0, weight=1)
        copy = tk.Frame(hero.content, bg=Palette.panel)
        copy.grid(row=0, column=0, sticky="nsew", padx=(0, 14))
        self._make_drag_handle(copy, "hero", Palette.panel).pack(anchor="e")
        tk.Label(copy, text="АГЕНТНАЯ РАБОЧАЯ СРЕДА", bg=Palette.panel,
                 fg=Palette.accent_dim, font=fonts.tiny).pack(anchor="w", pady=(6, 8))
        tk.Label(copy, text="От идеи — к проверенному\nрезультату", bg=Palette.panel,
                 fg=Palette.text, font=(fonts.ui, 25, "bold"), anchor="w", justify="left",
                 wraplength=540).pack(anchor="w")
        tk.Label(
            copy,
            text="GigaForge соединяет модель, специализированных агентов, файлы проекта и проверку результата в единый рабочий цикл.",
            bg=Palette.panel, fg=Palette.text_dim, font=fonts.body,
            anchor="w", justify="left", wraplength=520,
        ).pack(anchor="w", pady=(9, 0))
        _action_button(copy, "Перейти к задаче  →", lambda: self.focus_task(),
                       primary=True).pack(anchor="w", pady=(18, 0))
        self._draw_orbit(hero.content)

        actions_card = _card(root, "НАЧАТЬ СЦЕНАРИЙ", "Готовые цели запускаются в режиме Goal")
        if actions_card.header is not None:
            self._make_drag_handle(actions_card.header, "actions", Palette.panel).pack(side="right")
        for column in range(3):
            actions_card.content.grid_columnconfigure(column, weight=1, uniform="actions")
        tiles = (
            ("⌕", "Аудит проекта", "Архитектура и точки роста", SCENARIOS[0]["prompt"]),
            ("✦", "Собрать приложение", "Реализация и запуск", SCENARIOS[1]["prompt"]),
            ("✓", "Проверить качество", "Тесты и граничные случаи", SCENARIOS[2]["prompt"]),
        )
        for column, (icon, title, desc, prompt) in enumerate(tiles):
            tile = tk.Frame(actions_card.content, bg=Palette.panel,
                            cursor="hand2", padx=8, pady=8)
            tile.grid(row=0, column=column, sticky="ew",
                      padx=(0 if column == 0 else 10, 0))
            SmartIcon(tile, icon, size=34, font=(fonts.ui, 13, "bold")).pack(side="left",
                                                                            padx=(0, 10))
            copy_tile = tk.Frame(tile, bg=Palette.panel)
            copy_tile.pack(side="left", fill="x", expand=True)
            tk.Label(copy_tile, text=title, bg=Palette.panel, fg=Palette.text,
                     font=fonts.small_bold, anchor="w").pack(anchor="w")
            tk.Label(copy_tile, text=desc, bg=Palette.panel, fg=Palette.text_muted,
                     font=fonts.tiny, anchor="w").pack(anchor="w", pady=(2, 0))
            tk.Label(tile, text="→", bg=Palette.panel, fg=Palette.accent,
                     font=fonts.small_bold).pack(side="right", padx=(6, 0))
            command = lambda value=prompt: self._use_scenario(value)
            tile.bind("<Button-1>", lambda _e, fn=command: fn())
            pending = list(tile.winfo_children())
            while pending:
                child = pending.pop()
                if isinstance(child, tk.Misc):
                    child.bind("<Button-1>", lambda _e, fn=command: fn())
                    child.configure(cursor="hand2")
                pending.extend(child.winfo_children())

        activity_card = _card(root, "КОНТЕКСТ ЗАДАЧИ",
                              "Последний запуск и команда проекта", pad=13)
        if activity_card.header is not None:
            self._make_drag_handle(activity_card.header, "workspace", Palette.panel).pack(side="right")
        self._build_activity(activity_card.content)

        self._movable_blocks = {
            "hero": hero,
            "actions": actions_card,
            "workspace": activity_card,
        }
        if not getattr(self, "_dashboard_resize_bound", False):
            root.bind("<Configure>", self._dashboard_resized, add="+")
            self._dashboard_resize_bound = True
        self._dashboard_layout_mode: str | None = None
        self._apply_dashboard_order()

    def _dashboard_resized(self, event: Any = None) -> None:
        """Reflow the dashboard at a useful breakpoint instead of shrinking cards."""
        if event is not None and event.widget is not self.body:
            return
        self._apply_dashboard_order()

    def _make_drag_handle(self, parent: tk.Misc, key: str, bg: str) -> tk.Canvas:
        handle = tk.Canvas(parent, width=32, height=24, bg=bg, bd=0,
                           highlightthickness=0, cursor="fleur", takefocus=True)

        def draw(active: bool = False) -> None:
            handle.delete("all")
            fill = Palette.raised if active else Palette.panel_alt
            handle.create_oval(2, 2, 30, 22, fill=fill, outline="")
            for x in (13, 18):
                for y in (8, 12, 16):
                    handle.create_oval(x, y, x + 1.6, y + 1.6,
                                       fill=Palette.text_muted, outline="")

        draw()
        handle.bind("<Enter>", lambda _event: draw(True))
        handle.bind("<Leave>", lambda _event: draw(False))
        handle.bind("<ButtonPress-1>", lambda event, block=key: self._drag_start(block, event))
        handle.bind("<B1-Motion>", self._drag_motion)
        handle.bind("<ButtonRelease-1>", self._drag_end)
        return handle

    def _drag_start(self, key: str, event: Any) -> None:
        self._dragging_block = key
        self._drag_target = key
        ghost = tk.Toplevel(self)
        ghost.overrideredirect(True)
        try:
            ghost.attributes("-topmost", True)
            ghost.attributes("-alpha", 0.92)
        except tk.TclError:
            pass
        label = tk.Label(ghost, text="⠿  Панель перемещается", bg=Palette.panel,
                         fg=Palette.text, font=self.app.fonts.small_bold,
                         padx=14, pady=8, relief="flat", bd=0,
                         highlightthickness=1, highlightbackground=Palette.border_strong)
        label.pack()
        ghost.geometry(f"+{int(event.x_root) + 12}+{int(event.y_root) + 10}")
        self._drag_ghost = ghost
        for block, widget in self._movable_blocks.items():
            widget.set_drop_target(block == key)

    def _drag_motion(self, event: Any) -> None:
        if self._dragging_block is None:
            return
        if self._drag_ghost is not None:
            self._drag_ghost.geometry(f"+{int(event.x_root) + 12}+{int(event.y_root) + 10}")
        target = min(
            self._movable_blocks,
            key=lambda key: math.hypot(
                self._movable_blocks[key].winfo_rootx()
                + self._movable_blocks[key].winfo_width() / 2 - int(event.x_root),
                self._movable_blocks[key].winfo_rooty()
                + self._movable_blocks[key].winfo_height() / 2 - int(event.y_root),
            ),
        )
        if target != self._drag_target:
            self._drag_target = target
            for block, widget in self._movable_blocks.items():
                widget.set_drop_target(block == target)

    def _drag_end(self, event: Any) -> str:
        source = self._dragging_block
        target = self._drag_target
        self._dragging_block = None
        self._drag_target = None
        if self._drag_ghost is not None:
            self._drag_ghost.destroy()
            self._drag_ghost = None
        for widget in getattr(self, "_movable_blocks", {}).values():
            widget.set_drop_target(False)
        if source not in getattr(self, "_movable_blocks", {}):
            return "break"
        if target is None:
            x, y = int(event.x_root), int(event.y_root)
            target = min(self._movable_blocks, key=lambda key: math.hypot(
                self._movable_blocks[key].winfo_rootx()
                + self._movable_blocks[key].winfo_width() / 2 - x,
                self._movable_blocks[key].winfo_rooty()
                + self._movable_blocks[key].winfo_height() / 2 - y,
            ))
        if source == target:
            return "break"
        order = list(self._layout_order)
        source_index, target_index = order.index(source), order.index(target)
        order[source_index], order[target_index] = order[target_index], order[source_index]
        self._layout_order = order
        self._apply_dashboard_order()
        try:
            save_preferences(dashboard_order=order)
            self.app.toast("Порядок панелей сохранен", "ok")
        except (OSError, ValueError) as exc:
            self.app.toast(f"Не удалось сохранить раскладку: {exc}", "warn")
        return "break"

    def _apply_dashboard_order(self) -> None:
        if not hasattr(self, "_movable_blocks"):
            return
        root = self.body
        width = max(root.winfo_width(), self.winfo_width())
        mode = "wide" if width >= 1120 else "compact"
        if (mode == self._dashboard_layout_mode
                and list(self._layout_order) == getattr(self, "_dashboard_applied_order", None)):
            return
        self._dashboard_layout_mode = mode
        for widget in self._movable_blocks.values():
            widget.grid_forget()
        if mode == "wide":
            root.grid_columnconfigure(0, weight=3, uniform="dashboard")
            root.grid_columnconfigure(1, weight=1, uniform="dashboard")
            root.grid_rowconfigure(1, weight=1)
            root.grid_rowconfigure(2, weight=1)
            slots = (
                (1, 0, 1, 1),
                (2, 0, 1, 1),
                (1, 1, 2, 1),
            )
        else:
            root.grid_columnconfigure(0, weight=1, uniform="dashboard")
            root.grid_columnconfigure(1, weight=0, uniform="dashboard")
            root.grid_rowconfigure(1, weight=0)
            root.grid_rowconfigure(2, weight=0)
            slots = tuple((row, 0, 1, 2) for row in range(1, 4))
        for index, key in enumerate(self._layout_order):
            row, column, rowspan, colspan = slots[index]
            pady = (0, 16) if index < len(self._layout_order) - 1 else (0, 0)
            self._movable_blocks[key].grid(
                row=row, column=column, rowspan=rowspan, columnspan=colspan,
                sticky="nsew" if mode == "wide" and column == 1 else "ew",
                padx=(14, 0) if mode == "wide" and column == 1 else 0,
                pady=pady,
            )
        self._dashboard_applied_order = list(self._layout_order)
        self._position_composer()

    def _draw_orbit(self, parent: tk.Misc) -> None:
        fonts = self.app.fonts
        canvas = tk.Canvas(parent, height=264, bg=Palette.panel,
                           highlightthickness=0, bd=0, takefocus=False)
        canvas.grid(row=0, column=1, sticky="nsew")
        self._orbit_phase = 0
        self._orbit_nodes: list[tuple[float, float, str]] = []

        def activate(event: Any) -> str:
            nearest = min(self._orbit_nodes,
                          key=lambda item: math.hypot(item[0] - event.x, item[1] - event.y),
                          default=None)
            if nearest is not None and math.hypot(nearest[0] - event.x,
                                                  nearest[1] - event.y) < 38:
                self.navigate(nearest[2])
            else:
                self.focus_task()
            return "break"

        def hover(event: Any) -> None:
            near_node = any(math.hypot(x - event.x, y - event.y) < 38
                            for x, y, _page in self._orbit_nodes)
            canvas.configure(cursor="hand2" if near_node else "")

        canvas.bind("<Button-1>", activate)
        canvas.bind("<Motion>", hover)

        def draw(event: Any = None) -> None:
            canvas.delete("all")
            width = max(260, int(getattr(event, "width", canvas.winfo_width() or 420)))
            height = max(190, int(getattr(event, "height", canvas.winfo_height() or 250)))
            cx, cy = width / 2, height / 2
            breath = (math.sin(self._orbit_phase) + 1) / 2
            # A layered wash gives the creature a soft, organic aura. Drawing the
            # full rectangular wallpaper here created an obvious image tile.
            for layer in range(11, 0, -1):
                spread = layer / 11
                phase = self._orbit_phase * 0.35 + layer * 0.42
                offset_x = math.cos(phase) * layer * 0.72
                offset_y = math.sin(phase) * layer * 0.42
                tint = mix(Palette.wash_mint, Palette.wash_blue,
                           (math.sin(phase * 0.7) + 1) / 2)
                opacity = 0.075 + (1 - spread) * 0.075
                wash_color = mix(Palette.panel, tint, opacity)
                rx = 43 + layer * 4.2
                ry = 42 + layer * 3.1
                canvas.create_oval(cx - rx + offset_x, cy - ry + offset_y,
                                   cx + rx + offset_x, cy + ry + offset_y,
                                   fill=wash_color, outline="")
            orbit_w, orbit_h = min(width * 0.82, 470), min(height * 0.73, 196)
            for scale, color in ((1.0, "#cde9ec"), (0.74, "#cde4f5"),
                                 (0.48, "#d6eee7")):
                swell = breath * (2.0 * scale)
                canvas.create_oval(
                    cx - orbit_w * scale / 2 - swell,
                    cy - orbit_h * scale / 2 - swell / 2,
                    cx + orbit_w * scale / 2 + swell,
                    cy + orbit_h * scale / 2 + swell / 2,
                    outline=color, width=1,
                )
            if self._logo is not None:
                canvas.create_image(cx, cy, image=self._logo)
            else:
                canvas.create_oval(cx - 30, cy - 30, cx + 30, cy + 30,
                                   fill=Palette.accent_soft, outline=Palette.accent)
                canvas.create_text(cx, cy, text="GF", fill=Palette.accent,
                                   font=fonts.h3)
            nodes = ((cx - orbit_w * 0.43, cy - orbit_h * 0.27,
                      "PLAN", Palette.blue, "scenarios"),
                     (cx + orbit_w * 0.43, cy - orbit_h * 0.27,
                      "ACT", Palette.accent, "forge"),
                     (cx - orbit_w * 0.43, cy + orbit_h * 0.30,
                      "VERIFY", Palette.violet, "monitoring"),
                     (cx + orbit_w * 0.43, cy + orbit_h * 0.30,
                      "REVIEW", Palette.info, "agents"))
            self._orbit_nodes = [(px, py, target) for px, py, _label, _color, target in nodes]
            for px, py, label, color, _target in nodes:
                radius = 4 + breath * 1.5
                canvas.create_oval(px - radius, py - radius, px + radius, py + radius,
                                   fill=color, outline=Palette.panel, width=2)
                canvas.create_text(px, py + 14, text=label, fill=Palette.text_dim,
                                   font=fonts.tiny)

        canvas.bind("<Configure>", draw)

        def breathe() -> None:
            if not canvas.winfo_exists():
                return
            active = getattr(self.app, "_active_page", "") == "dashboard"
            if active:
                self._orbit_phase += 0.025
                draw()
            canvas.after(34 if active else 320, breathe)

        canvas.after(34, breathe)

    def _build_composer(self, parent: tk.Misc) -> None:
        fonts = self.app.fonts
        card = _card(parent, pad=12, bg=Palette.panel)
        self._task_composer = card
        card.place(x=0, y=5, width=760, height=138)
        parent.bind("<Configure>", self._position_composer, add="+")
        fonts = self.app.fonts
        headline = tk.Frame(card.content, bg=Palette.panel)
        headline.pack(fill="x", padx=10, pady=(4, 1))
        tk.Label(headline, text="Чем займёмся?", bg=Palette.panel,
                 fg=Palette.text, font=fonts.small_bold).pack(side="left")
        tk.Label(headline, text="Enter · отправить    Shift+Enter · новая строка",
                 bg=Palette.panel, fg=Palette.text_faint,
                 font=fonts.tiny).pack(side="right")
        self._placeholder = "Опишите задачу или задайте вопрос…"
        self.entry = tk.Text(card.content, height=2, wrap="word", undo=True,
                             bg=Palette.panel, fg=Palette.text_faint,
                             insertbackground=Palette.accent,
                             selectbackground=Palette.select_bg, relief="flat", bd=0,
                             highlightthickness=0, font=fonts.body, padx=10, pady=3)
        self.entry.pack(fill="both", expand=True, padx=10, pady=(0, 3))
        self.entry.insert("1.0", self._placeholder)
        self.entry.bind("<FocusIn>", self._clear_placeholder)
        self.entry.bind("<FocusOut>", self._restore_placeholder)
        self.entry.bind("<Return>", self._submit_event)

        controls = tk.Frame(card.content, bg=Palette.panel)
        controls.pack(fill="x", padx=10, pady=(0, 4))
        self._mode_switch = SegmentedControl(
            controls,
            (("chat", "Спросить", "✧"), ("agent", "Выполнить", "◈"),
             ("goal", "Цикл Goal", "⟳")),
            self.set_mode, fonts=fonts, selected=self._mode,
            palette=self.app.palette, height=34,
        )
        self._mode_switch.pack(side="left")
        self._mode_hint = tk.Label(card.content, text="Goal · выполнить, проверить, улучшить",
                                   bg=Palette.panel, fg=Palette.text_muted,
                                   font=fonts.tiny)
        self._mode_hint.pack(anchor="w", padx=12, pady=(0, 2))
        _action_button(controls, "Запустить  →", self._submit,
                       primary=True).pack(side="right")

    def _position_composer(self, event: Any = None) -> None:
        """Keep the task composer aligned with the main work column."""
        parent = self.composer_host
        if not hasattr(self, "_task_composer") or not parent.winfo_exists():
            return
        available = max(320, parent.winfo_width())
        dashboard_width = max(available, self.winfo_width())
        if dashboard_width >= 1120:
            width = min(1080, max(520, int((available - 56) * 0.75)))
            x = 28
        else:
            width = max(320, available - 36)
            x = 18
        self._task_composer.place(x=x, y=5, width=width, height=138)

    def _clear_placeholder(self, _event: Any = None) -> None:
        if self.entry.get("1.0", "end").strip() == self._placeholder:
            self.entry.delete("1.0", "end")
            self.entry.configure(fg=Palette.text)

    def _restore_placeholder(self, _event: Any = None) -> None:
        if not self.entry.get("1.0", "end").strip():
            self.entry.insert("1.0", self._placeholder)
            self.entry.configure(fg=Palette.text_faint)

    def _submit_event(self, event: Any = None) -> str:
        if event is not None and event.state & 0x0001:
            return ""
        self._submit()
        return "break"

    def _submit(self) -> None:
        text = self.entry.get("1.0", "end").strip()
        if not text or text == self._placeholder:
            self.entry.focus_set()
            return
        self.entry.delete("1.0", "end")
        self.start_task(text, self._mode)

    def set_mode(self, mode: str) -> None:
        if mode not in {"chat", "agent", "goal"}:
            return
        self._mode = mode
        self._mode_switch.set_selected(mode)
        hints = {
            "chat": "Спросить · только ответ модели, файлы останутся без изменений",
            "agent": "Выполнить · агент исследует проект и применит изменения",
            "goal": "Goal · выполнить задачу, проверить результат, продолжить при ошибках",
        }
        self._mode_hint.configure(text=hints[mode])

    def focus_task(self, prompt: str = "") -> None:
        if getattr(self.app, "_active_page", "") != "dashboard":
            self.navigate("dashboard")
        if prompt:
            self.entry.delete("1.0", "end")
            self.entry.insert("1.0", prompt)
            self.entry.configure(fg=Palette.text)
        self.entry.focus_set()

    def _use_scenario(self, prompt: str) -> None:
        self.set_mode("goal")
        self.focus_task(prompt)

    def _build_activity(self, parent: tk.Misc) -> None:
        fonts = self.app.fonts
        parent.grid_columnconfigure(0, weight=1)
        try:
            snapshot = self.ctx.snapshot()
        except Exception:  # noqa: BLE001 - an unavailable snapshot should not hide the dashboard
            snapshot = {}
        run = snapshot.get("run") if isinstance(snapshot, Mapping) else {}
        run = dict(run) if isinstance(run, Mapping) else {}
        progress = snapshot.get("progress") if isinstance(snapshot, Mapping) else {}
        progress = dict(progress) if isinstance(progress, Mapping) else {}
        status = str(run.get("status") or "unknown").strip().casefold()
        labels = {
            "accepted": "Проверено", "succeeded": "Завершено", "completed": "Завершено",
            "success": "Завершено", "running": "Выполняется", "in_progress": "Выполняется",
            "started": "Выполняется", "failed": "Ошибка", "rejected": "Отклонено",
            "cancelled": "Остановлено", "canceled": "Остановлено",
        }
        status_tone = (
            "ok" if status in {"accepted", "succeeded", "completed", "success"}
            else "running" if status in {"running", "in_progress", "started"}
            else "failed" if status in {"failed", "rejected", "error"}
            else "idle"
        )
        summary = tk.Frame(parent, bg=Palette.panel)
        summary.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        summary.grid_columnconfigure(0, weight=1)
        top = tk.Frame(summary, bg=Palette.panel)
        top.pack(fill="x")
        tk.Label(top, text="ПОСЛЕДНЯЯ ЗАДАЧА", bg=Palette.panel,
                 fg=Palette.text_muted, font=fonts.tiny).pack(side="left")
        Badge(top, labels.get(status, "Нет запусков"), fonts=fonts,
              status=status_tone).pack(side="right")
        goal = str(run.get("goal") or run.get("input") or "").strip()
        if goal:
            shown_goal = goal if len(goal) <= 140 else goal[:137].rstrip() + "…"
            tk.Label(summary, text=shown_goal, bg=Palette.panel, fg=Palette.text,
                     font=fonts.small_bold, anchor="w", justify="left",
                     wraplength=255).pack(fill="x", anchor="w", pady=(8, 3))
            try:
                percent = max(0, min(100, int(float(progress.get("percent") or 0))))
            except (TypeError, ValueError):
                percent = 0
            if progress.get("total"):
                meter = tk.Frame(summary, bg=Palette.panel_alt, height=5)
                meter.pack(fill="x", pady=(5, 2))
                meter.pack_propagate(False)
                tk.Frame(meter, bg=Palette.accent, width=1).place(
                    relx=0, rely=0, relwidth=percent / 100, relheight=1
                )
                tk.Label(summary, text=f"{percent}% этапов завершено",
                         bg=Palette.panel, fg=Palette.text_faint,
                         font=fonts.tiny).pack(anchor="w")
        else:
            tk.Label(summary, text="Задач пока нет. Опишите цель в поле внизу — команда начнёт работу здесь.",
                     bg=Palette.panel, fg=Palette.text_muted, font=fonts.small,
                     anchor="w", justify="left", wraplength=255).pack(
                         fill="x", anchor="w", pady=(8, 3)
                     )
        _action_button(summary, "История запусков →",
                       lambda: self.navigate("monitoring")).pack(anchor="w", pady=(5, 0))

        divider = tk.Frame(parent, bg=Palette.border, height=1)
        divider.grid(row=1, column=0, sticky="ew", pady=(2, 10))
        agent_section = tk.Frame(parent, bg=Palette.panel)
        agent_section.grid(row=2, column=0, sticky="ew")
        agent_head = tk.Frame(agent_section, bg=Palette.panel)
        agent_head.pack(fill="x", pady=(0, 6))
        agents = _safe_rows(self.ctx.agent_rows())
        tk.Label(agent_head, text=f"КОМАНДА  ·  {len(agents)} СПЕЦИАЛИСТОВ", bg=Palette.panel,
                 fg=Palette.text_muted, font=fonts.tiny).pack(side="left")
        _action_button(agent_head, "Вся команда →",
                       lambda: self.navigate("agents")).pack(side="right")
        for index, item in enumerate(agents[:5]):
            line = tk.Frame(agent_section, bg=Palette.panel, cursor="hand2")
            line.pack(fill="x", pady=3)
            tones = (Palette.accent, Palette.violet, Palette.blue, Palette.ok, Palette.info)
            SmartIcon(line, ("◈", "⌕", "✦", "▤", "⌘")[index % 5], size=28,
                      tone=tones[index % len(tones)], font=fonts.tiny).pack(
                          side="left", padx=(0, 8)
                      )
            tk.Label(line, text=_display(item.get("name")), bg=Palette.panel,
                     fg=Palette.text, font=fonts.small_bold,
                     anchor="w").pack(side="left", fill="x", expand=True)
            tk.Label(line, text=_display(item.get("family"), "agent").title(),
                     bg=Palette.panel, fg=Palette.text_faint,
                     font=fonts.tiny).pack(side="right")
            for widget in (line, *line.winfo_children()):
                widget.bind("<Button-1>", lambda _event: self.navigate("agents"))
                widget.configure(cursor="hand2")
        if not agents:
            tk.Label(agent_section, text="Каталог специалистов пока недоступен.",
                     bg=Palette.panel, fg=Palette.text_muted,
                     font=fonts.small).pack(anchor="w", pady=(4, 8))

        runs = _safe_rows(self.ctx.runs(limit=4))
        if len(runs) > 1:
            tk.Frame(parent, bg=Palette.border, height=1).grid(
                row=3, column=0, sticky="ew", pady=(10, 9)
            )
            history = tk.Frame(parent, bg=Palette.panel)
            history.grid(row=4, column=0, sticky="ew")
            tk.Label(history, text="ПРЕДЫДУЩИЕ ЗАПУСКИ", bg=Palette.panel,
                     fg=Palette.text_muted, font=fonts.tiny).pack(anchor="w", pady=(0, 6))
            for item in runs[1:3]:
                self._run_row(history, item)

    def _run_row(self, parent: tk.Misc, run: Mapping[str, Any]) -> None:
        fonts = self.app.fonts
        line = tk.Frame(parent, bg=Palette.panel, cursor="hand2")
        line.pack(fill="x", pady=(0, 8))
        status = str(run.get("status") or "unknown").casefold()
        tone = "ok" if status in {"accepted", "succeeded", "completed", "success"} else (
            "failed" if status in {"failed", "rejected", "error"} else "idle"
        )
        tk.Label(line, text="◈", bg=Palette.panel, fg=Palette.accent,
                 font=fonts.small).pack(side="left", anchor="n", padx=(0, 8))
        body = tk.Frame(line, bg=Palette.panel)
        body.pack(side="left", fill="x", expand=True)
        goal = _display(run.get("goal") or run.get("input"), "Запуск GigaForge")
        tk.Label(body, text=goal[:88], bg=Palette.panel, fg=Palette.text,
                 font=fonts.small_bold, anchor="w", justify="left",
                 wraplength=250).pack(anchor="w")
        Badge(body, status.replace("_", " "), fonts=fonts,
              status=tone).pack(anchor="w", pady=(3, 0))
        for widget in (line, *line.winfo_children(), *body.winfo_children()):
            widget.bind("<Button-1>", lambda _event: self.navigate("monitoring"))
            widget.configure(cursor="hand2")


class ProductPage(tk.Frame):
    """A consistent, scrollable view over live catalogs and project telemetry."""

    _META = {
        "agents": ("Агенты", "Специализированные роли, стратегии рассуждения и работа с контекстом."),
        "scenarios": ("Сценарии", "Запускайте проверенные рабочие потоки под задачи проекта."),
        "library": ("Библиотека", "Шаблоны задач, которые можно адаптировать и запустить как Goal."),
        "integrations": ("Интеграции", "Провайдеры и подключение модели в этой рабочей среде."),
        "monitoring": ("Мониторинг", "Состояние последнего запуска, метрики и последние события."),
    }

    def __init__(self, master: tk.Misc, app: Any, page: str,
                 start_task: Callable[[str, str], None]) -> None:
        super().__init__(master, bg=Palette.bg, bd=0, highlightthickness=0)
        self.app = app
        self.ctx = app.ctx
        self.page = page
        self.start_task = start_task
        self.scroller = ScrollFrame(self, bg=Palette.bg)
        self.scroller.pack(fill="both", expand=True)
        self.refresh()

    def refresh(self) -> None:
        body = self.scroller.inner
        for child in body.winfo_children():
            child.destroy()
        body.configure(padx=22, pady=20)
        title, subtitle = self._META[self.page]
        fonts = self.app.fonts
        tk.Label(body, text=f"GigaForge  /  {title}", bg=Palette.bg,
                 fg=Palette.text_muted, font=fonts.small).pack(anchor="w")
        tk.Label(body, text=title, bg=Palette.bg, fg=Palette.text,
                 font=fonts.h1).pack(anchor="w", pady=(4, 2))
        tk.Label(body, text=subtitle, bg=Palette.bg, fg=Palette.text_dim,
                 font=fonts.body, wraplength=1000, justify="left").pack(anchor="w", pady=(0, 16))
        {
            "agents": self._agents,
            "scenarios": self._scenarios,
            "library": self._library,
            "integrations": self._integrations,
            "monitoring": self._monitoring,
        }[self.page](body)

    def _agents(self, body: tk.Misc) -> None:
        rows = _safe_rows(self.ctx.agent_rows())
        groups: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            groups.setdefault(str(row.get("family") or "other"), []).append(row)
        labels = {"core": "ЯДРО", "reasoning": "РАССУЖДЕНИЕ", "context": "КОНТЕКСТ",
                  "delegation": "ДЕЛЕГИРОВАНИЕ"}
        for family, items in groups.items():
            card = _card(body, labels.get(family, family.upper()), f"{len(items)} роли")
            card.pack(fill="x", pady=(0, 12))
            for row in items:
                self._data_row(card.content, _display(row.get("name")),
                               _display(row.get("role")), _display(row.get("stage")),
                               icon="◈")
        _action_button(body, "Поставить задачу агентам →",
                       lambda: self.start_task("Изучи проект и выполни задачу с использованием подходящих специализированных агентов.", "goal"),
                       primary=True).pack(anchor="w", pady=(2, 12))

    def _scenarios(self, body: tk.Misc) -> None:
        self._template_grid(body, SCENARIOS)

    def _library(self, body: tk.Misc) -> None:
        playbooks = (
            SCENARIOS[0], SCENARIOS[1], SCENARIOS[2], SCENARIOS[3],
            {"title": "Безопасный рефакторинг", "category": "АРХИТЕКТУРА",
             "description": "Сохранить внешнее поведение и пройти нужные проверки.",
             "prompt": "Изучи архитектуру и выполни безопасный рефакторинг, который уменьшает дублирование и упрощает поддержку. Сохрани существующее поведение, запусти доступные проверки и исправь регрессии.", "icon": "⌘"},
            {"title": "Исправить дефект", "category": "ИСПРАВЛЕНИЯ",
             "description": "Найти корневую причину, добавить защиту и проверить исправление.",
             "prompt": "Исследуй проект, найди корневую причину указанной проблемы, внеси минимальное устойчивое исправление и проверь связанные сценарии.", "icon": "⌁"},
        )
        self._template_grid(body, playbooks)

    def _template_grid(self, body: tk.Misc, templates: Iterable[Mapping[str, str]]) -> None:
        fonts = self.app.fonts
        grid = tk.Frame(body, bg=Palette.bg)
        grid.pack(fill="x")
        for column in range(2):
            grid.grid_columnconfigure(column, weight=1, uniform="templates")
        for index, item in enumerate(templates):
            card = _card(grid, _display(item.get("title")), _display(item.get("category")))
            card.grid(row=index // 2, column=index % 2, sticky="nsew",
                      padx=(0 if index % 2 == 0 else 7, 7 if index % 2 == 0 else 0),
                      pady=(0, 12))
            tk.Label(card.content, text=_display(item.get("description")),
                     bg=Palette.panel, fg=Palette.text_dim, font=fonts.small,
                     wraplength=430, justify="left").pack(anchor="w", fill="x", pady=(0, 10))
            prompt = _display(item.get("prompt"))
            _action_button(card.content, "Использовать сценарий →",
                           lambda value=prompt: self.start_task(value, "goal"),
                           primary=True).pack(anchor="w")

    def _integrations(self, body: tk.Misc) -> None:
        fonts = self.app.fonts
        info = self.ctx.describe()
        card = _card(body, "Подключение модели", "Текущая конфигурация runtime")
        card.pack(fill="x", pady=(0, 12))
        details = (
            ("Провайдер", _display(info.get("provider"))),
            ("Состояние", "Подключен" if info.get("can_run") else "Требуется credential"),
            ("Базовая модель", _display((info.get("models") or {}).get("default"), "GigaChat")),
            ("Рабочий каталог", str(self.ctx.core_config.repo_root)),
        )
        for label, value in details:
            row = tk.Frame(card.content, bg=Palette.panel)
            row.pack(fill="x", pady=4)
            tk.Label(row, text=label, bg=Palette.panel, fg=Palette.text_muted,
                     font=fonts.small, width=20, anchor="w").pack(side="left")
            tk.Label(row, text=value, bg=Palette.panel, fg=Palette.text,
                     font=fonts.small_bold, anchor="w", justify="left",
                     wraplength=760).pack(side="left", fill="x", expand=True)
        _action_button(card.content, "Настроить подключение →", self.app.open_settings,
                       primary=True).pack(anchor="w", pady=(10, 0))
        _action_button(card.content, "Открыть live control room →",
                       self.app.open_browser_panel).pack(anchor="w", pady=(8, 0))

        providers = _safe_rows(self.ctx.provider_rows())
        providers_card = _card(body, f"Зарегистрированные провайдеры · {len(providers)}")
        providers_card.pack(fill="x")
        active_provider = str(info.get("provider") or "").strip().casefold()
        for provider in providers:
            provider_name = str(provider.get("name") or "").strip().casefold()
            same_giga_provider = {provider_name, active_provider} <= {"giga", "gigachat"}
            is_active = bool(provider_name) and (
                provider_name == active_provider or same_giga_provider
            )
            self._data_row(providers_card.content,
                           _display(provider.get("name")),
                           _display(provider.get("description") or provider.get("error"), "Совместимый API провайдер"),
                           "Текущий" if is_active else "Доступен",
                           icon="⇄")

    def _monitoring(self, body: tk.Misc) -> None:
        fonts = self.app.fonts
        snap = self.ctx.snapshot(force=True)
        kpis = snap.get("kpis") or {}
        tokens = (kpis.get("tokens") or {}).get("total", 0)
        calls = (kpis.get("tokens") or {}).get("calls", 0)
        cache = (kpis.get("cache") or {}).get("hit_rate", 0)
        health = kpis.get("health") or {}
        stats = tk.Frame(body, bg=Palette.bg)
        stats.pack(fill="x", pady=(0, 12))
        for index, (label, value, color) in enumerate((
            ("TOKENS", f"{int(tokens or 0):,}", Palette.accent),
            ("MODEL CALLS", f"{int(calls or 0):,}", Palette.blue),
            ("CACHE HIT", f"{float(cache or 0):.0%}", Palette.ok),
            ("ERROR SIGNALS", f"{int(health.get('router_errors') or 0):,}", Palette.violet),
        )):
            stats.grid_columnconfigure(index, weight=1, uniform="stats")
            tile = _card(stats)
            tile.grid(row=0, column=index, sticky="ew", padx=(0 if index == 0 else 6, 6 if index == 0 else 0))
            StatTile(tile.content, label=label, value=value, hint="По сохраненной телеметрии",
                     fonts=fonts, accent=color).pack(fill="x")

        run = snap.get("run") or {}
        latest = _card(body, "Последний запуск", "Статус и прогресс pipeline")
        latest.pack(fill="x", pady=(0, 12))
        self._data_row(latest.content, _display(run.get("goal"), "Задачи пока не запускались"),
                       f"Статус: {_display(run.get('status'))}",
                       f"Успешная проверка: {'да' if run.get('accepted') else 'нет'}", icon="◈")
        steps = _safe_rows(snap.get("steps"))
        timeline = _card(body, "Этапы конвейера", f"{len(steps)} записей")
        timeline.pack(fill="x", pady=(0, 12))
        for step in steps[:16]:
            self._data_row(timeline.content, _display(step.get("node")),
                           _display(step.get("status")),
                           f"{int(step.get('duration_ms') or 0):,} ms", icon="·")

        errors = list(snap.get("errors") or [])
        error_card = _card(body, "Ошибки и сигналы", f"{len(errors)}")
        error_card.pack(fill="x", pady=(0, 12))
        if errors:
            for error in errors[:8]:
                tk.Label(error_card.content, text=str(error), bg=Palette.panel,
                         fg=Palette.err, font=fonts.small, wraplength=980,
                         justify="left", anchor="w").pack(fill="x", pady=3)
        else:
            tk.Label(error_card.content, text="Сохраненных ошибок не найдено.",
                     bg=Palette.panel, fg=Palette.text_muted, font=fonts.small).pack(anchor="w")

        runs = _safe_rows(self.ctx.runs(limit=12))
        history = _card(body, "История запусков", f"{len(runs)} записей")
        history.pack(fill="x")
        for item in runs:
            self._data_row(history.content,
                           _display(item.get("goal") or item.get("input"), "Запуск GigaForge"),
                           _display(item.get("status")), _display(item.get("run_id")), icon="◈")

    def _data_row(self, parent: tk.Misc, title: str, detail: str, meta: str,
                  *, icon: str = "◈") -> None:
        fonts = self.app.fonts
        row = tk.Frame(parent, bg=Palette.panel)
        row.pack(fill="x", pady=(0, 7))
        SmartIcon(row, icon, size=32, font=fonts.small_bold).pack(
            side="left", anchor="n", padx=(0, 10)
        )
        text = tk.Frame(row, bg=Palette.panel)
        text.pack(side="left", fill="x", expand=True)
        tk.Label(text, text=title, bg=Palette.panel, fg=Palette.text,
                 font=fonts.small_bold, anchor="w", justify="left",
                 wraplength=780).pack(anchor="w")
        tk.Label(text, text=detail, bg=Palette.panel, fg=Palette.text_muted,
                 font=fonts.tiny, anchor="w", justify="left",
                 wraplength=780).pack(anchor="w", pady=(2, 0))
        tk.Label(row, text=meta, bg=Palette.panel, fg=Palette.text_faint,
                 font=fonts.tiny, anchor="e", justify="right", wraplength=220).pack(
                     side="right", padx=(8, 0))


class FileWorkspacePage(tk.Frame):
    """Project-scoped editor, explicit terminal, live diff and pipeline context."""

    _IGNORED = {".git", ".gigaforge", ".venv", "venv", "env", "node_modules",
                "__pycache__", ".pytest_cache", ".ruff_cache", "dist", "build",
                "target", ".next", "coverage", "vendor"} | SENSITIVE_WORKSPACE_DIRECTORIES
    _SOURCE = {".py", ".pyi", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx",
               ".html", ".css", ".scss", ".json", ".toml", ".yaml", ".yml",
               ".md", ".txt", ".sh", ".bat", ".ps1", ".sql", ".go", ".rs",
               ".java", ".kt", ".cs", ".cpp", ".h", ".hpp", ".xml", ".svg"}
    _SOURCE_NAMES = {
        ".gitignore", ".gitattributes", ".editorconfig", ".env.example", "dockerfile", "makefile",
        "justfile", "procfile", "license", "cmakelists.txt",
    }

    def __init__(self, master: tk.Misc, app: Any) -> None:
        super().__init__(master, bg=Palette.bg, bd=0, highlightthickness=0)
        self.app = app
        self.ctx = app.ctx
        self._file_by_id: dict[str, Path] = {}
        self._last_snapshot: Mapping[str, Any] = {}
        self._open_path: Path | None = None
        self._root_at_open: Path | None = None
        self._baseline_digest: str | None = None
        self._baseline_text: str | None = None
        self._newline = "\n"
        self._loading_editor = False
        self._command_running = False
        self._terminal_results: queue.Queue[tuple[str, str]] = queue.Queue()
        self._build()
        self.refresh()

    def _build(self) -> None:
        fonts = self.app.fonts
        header = tk.Frame(self, bg=Palette.bg)
        header.pack(fill="x", padx=20, pady=(16, 10))
        copy = tk.Frame(header, bg=Palette.bg)
        copy.pack(side="left", fill="x", expand=True)
        tk.Label(copy, text="Forge  /  Файлы проекта", bg=Palette.bg,
                 fg=Palette.text_muted, font=fonts.small).pack(anchor="w")
        tk.Label(copy, text="Рабочее пространство", bg=Palette.bg,
                 fg=Palette.text, font=fonts.h2).pack(anchor="w", pady=(2, 0))
        self.root_label = tk.Label(copy, text="", bg=Palette.bg,
                                   fg=Palette.text_faint, font=fonts.tiny)
        self.root_label.pack(anchor="w", pady=(2, 0))
        _action_button(header, "↻  Обновить", self.refresh).pack(side="right", anchor="n", pady=7)

        panes = tk.Frame(self, bg=Palette.bg)
        panes.pack(fill="both", expand=True, padx=16, pady=(0, 14))
        panes.grid_columnconfigure(0, weight=3, minsize=200)
        panes.grid_columnconfigure(1, weight=7, minsize=400)
        panes.grid_columnconfigure(2, weight=3, minsize=230)
        panes.grid_rowconfigure(0, weight=1)

        tree_card = _card(panes, "Файлы", "Исходники проекта", pad=10)
        tree_card.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        self.search = ttk.Entry(tree_card.content, font=fonts.small)
        self.search.insert(0, "Фильтр файлов…")
        self.search.pack(fill="x", pady=(0, 8))
        self.search.bind("<FocusIn>", self._clear_filter_hint)
        self.search.bind("<KeyRelease>", lambda _event: self._populate_tree())
        tree_wrap = tk.Frame(tree_card.content, bg=Palette.panel)
        tree_wrap.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(tree_wrap, show="tree", selectmode="browse")
        tree_scroll = ttk.Scrollbar(tree_wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=tree_scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        tree_scroll.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self._open_file)

        center = tk.Frame(panes, bg=Palette.bg)
        center.grid(row=0, column=1, sticky="nsew", padx=(0, 8))
        center.grid_columnconfigure(0, weight=1)
        center.grid_rowconfigure(0, weight=7)
        center.grid_rowconfigure(1, weight=3)
        editor_card = _card(center, "Редактор", "Ctrl+S · сохраняет в текущий проект", pad=10)
        editor_card.grid(row=0, column=0, sticky="nsew", pady=(0, 8))
        self.file_label = tk.Label(editor_card.header, text="Выберите файл в дереве",
                                   bg=Palette.panel, fg=Palette.text_faint,
                                   font=fonts.tiny, anchor="e") if editor_card.header else None
        if self.file_label is not None:
            self.file_label.pack(side="right")
            self.save_button = _action_button(
                editor_card.header, "Сохранить", self._save_file
            )
            self.save_button.pack(side="right", padx=(0, 8))
            self.save_button.configure(state="disabled")
            self.reload_button = _action_button(
                editor_card.header, "Перечитать", self._reload_file
            )
            self.reload_button.pack(side="right", padx=(0, 6))
            self.reload_button.configure(state="disabled")
        code_wrap = Card(editor_card.content, fonts=fonts, palette=Palette, pad=3,
                         bg="#102945")
        code_wrap.pack(fill="both", expand=True)
        code_surface = code_wrap.content
        self.line_numbers = tk.Text(code_surface, width=5, bg="#0d233c", fg="#7092b5",
                                    relief="flat", bd=0, wrap="none", font=fonts.code,
                                    padx=8, pady=10, state="disabled", takefocus=False,
                                    cursor="arrow")
        self.code = tk.Text(code_surface, bg="#102945", fg="#d8eaff",
                            insertbackground="#d8eaff", selectbackground="#254c74",
                            relief="flat", bd=0, wrap="none", font=fonts.code,
                            padx=12, pady=10, undo=True, autoseparators=True)
        code_y = ttk.Scrollbar(code_surface, orient="vertical", command=self._scroll_editor)
        self._editor_scrollbar = code_y
        code_x = ttk.Scrollbar(code_surface, orient="horizontal", command=self.code.xview)
        self.code.configure(yscrollcommand=self._sync_editor_scroll, xscrollcommand=code_x.set)
        self.line_numbers.pack(side="left", fill="y")
        self.code.pack(side="left", fill="both", expand=True)
        code_y.pack(side="right", fill="y")
        code_x.pack(fill="x")
        self.code.tag_configure("added", foreground="#8de3ae", background="#173d37")
        self.code.tag_configure("removed", foreground="#ff9ca8", background="#482d3a")
        self.code.bind("<<Modified>>", self._editor_changed)
        self.code.bind("<Control-s>", self._save_event)

        pipeline_card = _card(center, "Выполнение", "Этапы последнего запуска", pad=10)
        pipeline_card.grid(row=1, column=0, sticky="nsew")
        tabs = tk.Frame(pipeline_card.content, bg=Palette.panel)
        tabs.pack(fill="x", pady=(0, 5))
        self._execution_tab = "steps"
        self._execution_buttons: dict[str, SoftButton] = {}
        for key, label in (("steps", "Этапы"), ("trace", "События"),
                           ("errors", "Ошибки"), ("terminal", "Терминал")):
            button = SoftButton(
                tabs, label, lambda name=key: self._show_execution_tab(name),
                fonts=fonts, primary=key == self._execution_tab,
            )
            button.pack(side="left", padx=(0, 5))
            self._execution_buttons[key] = button
        self.pipeline = tk.Text(pipeline_card.content, height=6, bg=Palette.panel,
                                fg=Palette.text_dim, bd=0, relief="flat",
                                font=fonts.code_small, wrap="word", state="disabled")
        self.pipeline.pack(fill="both", expand=True)
        self.terminal = tk.Frame(pipeline_card.content, bg=Palette.panel)
        self.terminal_output = tk.Text(
            self.terminal, height=6, bg="#0d233c", fg="#cce7ff", insertbackground="#cce7ff",
            bd=0, relief="flat", font=fonts.code_small, wrap="word", state="disabled",
        )
        self.terminal_output.pack(fill="both", expand=True, pady=(0, 7))
        terminal_controls = tk.Frame(self.terminal, bg=Palette.panel)
        terminal_controls.pack(fill="x")
        self.command_entry = ttk.Entry(terminal_controls, font=fonts.code_small)
        self.command_entry.pack(side="left", fill="x", expand=True, padx=(0, 7))
        self.command_entry.insert(0, "Введите команду для каталога проекта…")
        self.command_entry.bind("<FocusIn>", self._clear_command_hint)
        self.command_entry.bind("<Return>", self._run_command_event)
        self.run_command_button = _action_button(
            terminal_controls, "Выполнить", self._run_command, primary=True
        )
        self.run_command_button.pack(side="left")
        _action_button(terminal_controls, "Очистить", self._clear_terminal).pack(
            side="left", padx=(6, 0)
        )
        tk.Label(
            self.terminal, text="Команда запускается только после нажатия Enter или кнопки.",
            bg=Palette.panel, fg=Palette.text_faint, font=fonts.tiny, anchor="w",
        ).pack(fill="x", pady=(5, 0))

        right = tk.Frame(panes, bg=Palette.bg)
        right.grid(row=0, column=2, sticky="nsew")
        right.grid_columnconfigure(0, weight=1)
        changes = _card(right, "Изменения", "Из последнего запуска", pad=10)
        changes.grid(row=0, column=0, sticky="nsew", pady=(0, 8))
        right.grid_rowconfigure(0, weight=4)
        right.grid_rowconfigure(1, weight=3)
        self.diff_list = tk.Frame(changes.content, bg=Palette.panel)
        self.diff_list.pack(fill="both", expand=True)
        diff_card = _card(right, "Diff", "Проверенные изменения", pad=10)
        diff_card.grid(row=1, column=0, sticky="nsew")
        self.diff_text = tk.Text(diff_card.content, bg=Palette.panel_alt,
                                 fg=Palette.text_dim, bd=0, relief="flat",
                                 font=fonts.code_small, wrap="none", state="disabled")
        diff_y = ttk.Scrollbar(diff_card.content, orient="vertical", command=self.diff_text.yview)
        self.diff_text.configure(yscrollcommand=diff_y.set)
        self.diff_text.pack(side="left", fill="both", expand=True)
        diff_y.pack(side="right", fill="y")
        self.diff_text.tag_configure("added", foreground=Palette.ok)
        self.diff_text.tag_configure("removed", foreground=Palette.err)
        self.diff_text.tag_configure("meta", foreground=Palette.accent)

    def _clear_filter_hint(self, _event: Any = None) -> None:
        if self.search.get() == "Фильтр файлов…":
            self.search.delete(0, "end")

    def refresh(self) -> None:
        root = Path(str(self.ctx.core_config.repo_root or ".")).expanduser().resolve()
        if self._open_path is not None and self._root_at_open != root:
            if self._editor_is_dirty():
                self.app.toast(
                    "Проект переключён. Несохранённый файл останется открыт; вернитесь к исходному проекту, чтобы сохранить его.",
                    "warn",
                )
            else:
                self._open_path = None
                self._root_at_open = None
                self._baseline_digest = None
                self._baseline_text = None
                self._display_path = "Выберите файл в дереве"
                self._replace_editor_text("")
                self.file_label.configure(text=self._display_path)
                self.save_button.configure(state="disabled")
                self.reload_button.configure(state="disabled")
        self._root = root
        self.root_label.configure(text=str(root))
        self._populate_tree()
        try:
            snapshot = self.ctx.snapshot(force=True)
        except Exception:  # noqa: BLE001 - show an empty monitor if telemetry is unavailable
            snapshot = {}
        self._last_snapshot = snapshot
        self._render_pipeline(snapshot)
        self._render_diff(snapshot)

    def _populate_tree(self) -> None:
        if not hasattr(self, "tree"):
            return
        children = self.tree.get_children()
        if children:
            self.tree.delete(*children)
        self._file_by_id.clear()
        root = getattr(self, "_root", Path(str(self.ctx.core_config.repo_root or ".")).resolve())
        if not root.is_dir():
            self.tree.insert("", "end", text="Проект недоступен")
            return
        if is_sensitive_workspace_path(root.name):
            self.tree.insert("", "end", text="Папка проекта скрыта как хранилище секретов")
            return
        query = self.search.get().strip().casefold()
        if query == "фильтр файлов…":
            query = ""
        root_id = self.tree.insert("", "end", text=root.name or str(root), open=True)
        try:
            count = 0
            for current, dirs, names in os.walk(root, followlinks=False):
                current_path = Path(current)
                depth = len(current_path.relative_to(root).parts)
                dirs[:] = sorted(
                    name for name in dirs
                    if name.casefold() not in self._IGNORED
                    and not (current_path / name).is_symlink()
                ) if depth < 5 else []
                for name in sorted(names):
                    path = current_path / name
                    if (path.suffix.casefold() not in self._SOURCE
                            and name.casefold() not in self._SOURCE_NAMES) or path.is_symlink():
                        continue
                    try:
                        relative = path.relative_to(root).as_posix()
                        resolved_relative = path.resolve().relative_to(root).as_posix()
                        if (is_sensitive_workspace_path(relative)
                                or is_sensitive_workspace_path(resolved_relative)):
                            continue
                        if path.stat().st_size > 1_000_000 or (query and query not in relative.casefold()):
                            continue
                    except (OSError, ValueError):
                        continue
                    parent_id = root_id
                    relative_parent = Path(relative).parent
                    if str(relative_parent) != ".":
                        current_id = root_id
                        accumulated = Path()
                        for part in relative_parent.parts:
                            accumulated /= part
                            key = f"dir:{accumulated.as_posix()}"
                            existing = self.tree.exists(key)
                            if existing:
                                current_id = key
                            else:
                                current_id = self.tree.insert(current_id, "end", iid=key,
                                                              text=part, open=bool(query))
                        parent_id = current_id
                    file_id = f"file:{relative}"
                    item_id = self.tree.insert(parent_id, "end", iid=file_id, text=name)
                    self._file_by_id[item_id] = path
                    count += 1
                    if count >= 500:
                        break
                if count >= 500:
                    break
            if not self._file_by_id:
                self.tree.insert(root_id, "end", text="Исходники не найдены")
        except OSError as exc:
            self.tree.insert(root_id, "end", text=f"Не удалось прочитать проект: {exc}")

    def _open_file(self, _event: Any = None) -> None:
        selection = self.tree.selection()
        if not selection:
            return
        path = self._file_by_id.get(selection[0])
        if path is None:
            return
        if self._editor_is_dirty():
            answer = messagebox.askyesnocancel(
                "Несохранённые изменения",
                f"Сохранить изменения в {self._open_path.name if self._open_path else 'файле'}?",
                parent=self.winfo_toplevel(),
                default="no",
            )
            if answer is None or (answer and not self._save_file()):
                return
        try:
            resolved = path.resolve(strict=True)
            if not resolved.is_relative_to(self._root):
                raise ValueError("Файл находится за пределами проекта")
            if (is_sensitive_workspace_path(self._root.name)
                    or is_sensitive_workspace_path(resolved.relative_to(self._root).as_posix())):
                raise ValueError("Файлы с локальными секретами закрыты в рабочем пространстве")
            raw = resolved.read_bytes()
            if b"\0" in raw[:4096]:
                raise ValueError("Бинарные файлы не показываются в редакторе")
            content = raw.decode("utf-8")
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            self.app.toast(f"Не удалось открыть файл: {exc}", "warn")
            return
        self._open_path = resolved
        self._root_at_open = self._root
        self._baseline_digest = hashlib.sha256(raw).hexdigest()
        self._newline = "\r\n" if b"\r\n" in raw else "\n"
        self._baseline_text = content
        self._display_path = resolved.relative_to(self._root).as_posix()
        self._replace_editor_text(content)
        self._update_file_label()
        self.save_button.configure(state="disabled")
        self.reload_button.configure(state="normal")
        self.code.yview_moveto(0)

    def _reload_file(self) -> None:
        if self._open_path is None:
            return
        if self._editor_is_dirty():
            discard = messagebox.askyesno(
                "Перечитать файл",
                "Заменить несохранённые изменения версией файла на диске?",
                parent=self.winfo_toplevel(),
                default="no",
            )
            if not discard:
                return
        path = self._open_path
        try:
            resolved = path.resolve(strict=True)
            if not resolved.is_relative_to(self._root) or not resolved.is_file():
                raise ValueError("Файл больше не находится в текущем проекте")
            if (is_sensitive_workspace_path(self._root.name)
                    or is_sensitive_workspace_path(resolved.relative_to(self._root).as_posix())):
                raise ValueError("Файлы с локальными секретами закрыты в рабочем пространстве")
            raw = resolved.read_bytes()
            if b"\0" in raw[:4096]:
                raise ValueError("Файл выглядит как бинарный")
            content = raw.decode("utf-8")
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            self.app.toast(f"Не удалось перечитать файл: {exc}", "warn")
            return
        self._open_path = resolved
        self._root_at_open = self._root
        self._baseline_digest = hashlib.sha256(raw).hexdigest()
        self._baseline_text = content
        self._newline = "\r\n" if b"\r\n" in raw else "\n"
        self._replace_editor_text(content)
        self.save_button.configure(state="disabled")
        self._update_file_label()
        self.code.yview_moveto(0)

    def _replace_editor_text(self, content: str) -> None:
        self._loading_editor = True
        self.code.delete("1.0", "end")
        self.code.insert("1.0", content)
        self.code.edit_modified(False)
        self._render_line_numbers()
        self._loading_editor = False

    def _render_line_numbers(self) -> None:
        if not hasattr(self, "line_numbers"):
            return
        count = max(1, int(self.code.index("end-1c").split(".", 1)[0]))
        self.line_numbers.configure(state="normal")
        self.line_numbers.delete("1.0", "end")
        self.line_numbers.insert("1.0", "\n".join(f"{line:>4}" for line in range(1, count + 1)))
        self.line_numbers.configure(state="disabled")

    def _sync_editor_scroll(self, first: str, last: str) -> None:
        self.line_numbers.yview_moveto(first)
        self._editor_scrollbar.set(first, last)

    def _scroll_editor(self, *args: Any) -> None:
        self.code.yview(*args)
        self.line_numbers.yview(*args)

    def _editor_changed(self, _event: Any = None) -> None:
        if self._loading_editor:
            self.code.edit_modified(False)
            return
        if self.code.edit_modified():
            self.code.edit_modified(False)
            self._render_line_numbers()
            dirty = self._editor_is_dirty()
            self.save_button.configure(state="normal" if dirty else "disabled")
            self._update_file_label()

    def _editor_is_dirty(self) -> bool:
        return (
            self._open_path is not None
            and self._baseline_text is not None
            and self.code.get("1.0", "end-1c") != self._baseline_text
        )

    def _update_file_label(self) -> None:
        if self.file_label is None:
            return
        path = getattr(self, "_display_path", "Выберите файл в дереве")
        self.file_label.configure(text=f"{path}{'  • изменён' if self._editor_is_dirty() else ''}")

    def _save_event(self, _event: Any = None) -> str:
        self._save_file()
        return "break"

    def _save_file(self) -> bool:
        path = self._open_path
        if path is None or self._baseline_digest is None:
            self.app.toast("Сначала выберите файл проекта", "warn")
            return False
        if not self._editor_is_dirty():
            return True
        if self._root_at_open != self._root:
            self.app.toast(
                "Для сохранения переключитесь обратно на проект, в котором открыт этот файл.",
                "warn",
            )
            return False
        temporary: Path | None = None
        try:
            resolved = path.resolve(strict=True)
            if not resolved.is_relative_to(self._root) or not resolved.is_file():
                raise ValueError("Путь файла больше не принадлежит открытому проекту")
            if (is_sensitive_workspace_path(self._root.name)
                    or is_sensitive_workspace_path(resolved.relative_to(self._root).as_posix())):
                raise ValueError("Файлы с локальными секретами закрыты в рабочем пространстве")
            current = resolved.read_bytes()
            digest = hashlib.sha256(current).hexdigest()
            if digest != self._baseline_digest:
                self.app.toast("Файл изменился вне редактора. Перечитайте его перед сохранением.", "warn")
                return False
            content = self.code.get("1.0", "end-1c").replace("\n", self._newline)
            encoded = content.encode("utf-8")
            fd, name = tempfile.mkstemp(prefix=f".{resolved.name}.", suffix=".tmp",
                                        dir=resolved.parent)
            temporary = Path(name)
            with os.fdopen(fd, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, resolved.stat().st_mode)
            os.replace(temporary, resolved)
            temporary = None
            self._baseline_digest = hashlib.sha256(encoded).hexdigest()
            self._baseline_text = self.code.get("1.0", "end-1c")
            self.save_button.configure(state="disabled")
            self._update_file_label()
            self.app.toast(f"Сохранено: {self._display_path}", "ok")
            return True
        except (OSError, ValueError, UnicodeEncodeError) as exc:
            self.app.toast(f"Не удалось сохранить файл: {exc}", "err")
            return False
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _clear_command_hint(self, _event: Any = None) -> None:
        if self.command_entry.get() == "Введите команду для каталога проекта…":
            self.command_entry.delete(0, "end")

    def _run_command_event(self, _event: Any = None) -> str:
        self._run_command()
        return "break"

    def _run_command(self) -> None:
        command = self.command_entry.get().strip()
        if not command or command == "Введите команду для каталога проекта…":
            self.command_entry.focus_set()
            return
        if self._command_running:
            return
        active = getattr(getattr(self.ctx, "runtime", None), "has_active_work", False)
        if callable(active):
            active = active()
        if active:
            self.app.toast("Дождитесь завершения активной задачи перед запуском команды", "warn")
            return
        self.command_entry.delete(0, "end")
        self._command_running = True
        self.run_command_button.configure(state="disabled")
        self._append_terminal(f"$ {command}\n")
        root = self._root

        def execute() -> None:
            try:
                result = subprocess.run(
                    command, cwd=root, shell=True, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, text=True,
                    encoding="utf-8", errors="replace", timeout=120, check=False,
                )
                output = result.stdout
                suffix = f"\n[exit code {result.returncode}]\n"
            except subprocess.TimeoutExpired as exc:
                output = exc.stdout or ""
                if isinstance(output, bytes):
                    output = output.decode("utf-8", errors="replace")
                suffix = "\n[command timed out after 120 seconds]\n"
            except OSError as exc:
                output, suffix = "", f"\n[command failed: {exc}]\n"
            self._terminal_results.put((output, suffix))

        self.after(100, self._poll_terminal_result)
        threading.Thread(target=execute, name="gigaforge-terminal", daemon=True).start()

    def _poll_terminal_result(self) -> None:
        try:
            output, suffix = self._terminal_results.get_nowait()
        except queue.Empty:
            if self.winfo_exists():
                self.after(100, self._poll_terminal_result)
            return
        self._command_finished(output, suffix)

    def _command_finished(self, output: str, suffix: str) -> None:
        self._append_terminal(output + suffix)
        self._command_running = False
        self.run_command_button.configure(state="normal")

    def _append_terminal(self, text: str) -> None:
        self.terminal_output.configure(state="normal")
        self.terminal_output.insert("end", text[-30000:])
        char_count = len(self.terminal_output.get("1.0", "end-1c"))
        if char_count > 30000:
            self.terminal_output.delete("1.0", f"1.0 + {char_count - 30000} chars")
        self.terminal_output.see("end")
        self.terminal_output.configure(state="disabled")

    def _clear_terminal(self) -> None:
        self.terminal_output.configure(state="normal")
        self.terminal_output.delete("1.0", "end")
        self.terminal_output.configure(state="disabled")

    def _render_pipeline(self, snapshot: Mapping[str, Any]) -> None:
        if self._execution_tab == "trace":
            events = _safe_rows(snapshot.get("trace"))
            lines = [
                f"{_display(event.get('event')):<24} {_display(event.get('node')):<12} "
                f"{_display(event.get('ts') or event.get('timestamp'))}"
                for event in events[:30]
            ]
            empty = "Событий пока нет."
        elif self._execution_tab == "errors":
            errors = list(snapshot.get("errors") or [])
            lines = [str(error) for error in errors[:30]]
            empty = "Сохраненных ошибок не найдено."
        else:
            steps = _safe_rows(snapshot.get("steps"))
            lines = [
                f"●  {_display(step.get('node')):<12} {_display(step.get('status')):<12} "
                f"{int(step.get('duration_ms') or 0):>6,} ms"
                for step in steps[:30]
            ]
            empty = "Нет сохраненных этапов. Запустите задачу в Forge."
        self.pipeline.configure(state="normal")
        self.pipeline.delete("1.0", "end")
        if not lines:
            self.pipeline.insert("end", empty)
        else:
            self.pipeline.insert("end", "\n".join(lines))
        self.pipeline.configure(state="disabled")

    def _show_execution_tab(self, name: str) -> None:
        if name not in {"steps", "trace", "errors", "terminal"}:
            return
        self._execution_tab = name
        for key, button in self._execution_buttons.items():
            active = key == name
            button.set_primary(active)
        if name == "terminal":
            self.pipeline.pack_forget()
            self.terminal.pack(fill="both", expand=True)
        else:
            self.terminal.pack_forget()
            self.pipeline.pack(fill="both", expand=True)
            self._render_pipeline(self._last_snapshot)

    def _render_diff(self, snapshot: Mapping[str, Any]) -> None:
        fonts = self.app.fonts
        for child in self.diff_list.winfo_children():
            child.destroy()
        diff = snapshot.get("diff") or {}
        files = _safe_rows(diff.get("files"))
        if not files:
            tk.Label(self.diff_list, text="Нет изменений последнего запуска.",
                     bg=Palette.panel, fg=Palette.text_muted, font=fonts.small,
                     justify="left", wraplength=230).pack(anchor="w", pady=4)
        for item in files[:30]:
            row = tk.Frame(self.diff_list, bg=Palette.panel)
            row.pack(fill="x", pady=3)
            name = _display(item.get("path") or item.get("file"), "измененный файл")
            tk.Label(row, text="▤", bg=Palette.panel, fg=Palette.accent,
                     font=fonts.tiny).pack(side="left", padx=(0, 6))
            tk.Label(row, text=name, bg=Palette.panel, fg=Palette.text_dim,
                     font=fonts.tiny, anchor="w", justify="left", wraplength=200).pack(
                         side="left", fill="x", expand=True)
        text = str(diff.get("text") or "")
        self.diff_text.configure(state="normal")
        self.diff_text.delete("1.0", "end")
        if not text:
            self.diff_text.insert("end", "Полный diff появится после запуска задачи.")
        else:
            for line in text.splitlines()[:600]:
                tag = "added" if line.startswith("+") and not line.startswith("+++") else (
                    "removed" if line.startswith("-") and not line.startswith("---") else
                    "meta" if line.startswith(("diff ", "index ", "@@")) else "")
                if tag:
                    self.diff_text.insert("end", line + "\n", tag)
                else:
                    self.diff_text.insert("end", line + "\n")
        self.diff_text.configure(state="disabled")
