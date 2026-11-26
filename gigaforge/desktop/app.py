"""GigaForge V6.fresh desktop shell and product navigation."""
from __future__ import annotations

import base64
import queue
import threading
import tkinter as tk
from pathlib import Path
from typing import Any

from .context import AppContext
from .settings import CredentialsDialog, SettingsDialog
from .theme import Palette, apply_theme, enable_hidpi
from .views.hub import DashboardView, FileWorkspacePage, ProductPage
from .views.workspace import WorkspaceView
from .widgets import Badge, Card, NavButton, SoftButton
from .worker import Worker

__all__ = ["GigaForgeWindow", "run", "SettingsDialog", "CredentialsDialog"]

APP_NAME = "GigaForge"
APP_TAGLINE = "V6.fresh · agent workspace · GigaChat"
VERSION = "6.0.0"
PRODUCT_VERSION = "6.fresh"

# Drag & drop is an optional enhancement. Load it into an already-created Tk root
# so a missing or incompatible native tkdnd binary can never prevent startup.
try:  # pragma: no cover - optional dependency
    from tkinterdnd2 import TkinterDnD
except Exception:  # noqa: BLE001
    TkinterDnD = None  # type: ignore[assignment,misc]


class GigaForgeWindow(tk.Tk):
    """Navigation shell connecting the live dashboard and agent workspace."""

    def __init__(self, ctx: AppContext, *, offline_note: str = "",
                 view: str | None = None) -> None:
        super().__init__()
        self.drag_drop_available = False
        if TkinterDnD is not None:
            try:
                TkinterDnD.require(self)
                self.drag_drop_available = True
            except Exception:  # noqa: BLE001 - attachment button remains available without tkdnd
                pass
        self.ctx = ctx
        self.version = PRODUCT_VERSION
        self.offline_note = offline_note
        enable_hidpi(self)
        self.title(f"{APP_NAME} — {APP_TAGLINE}")
        self.geometry("1500x940")
        self.minsize(1120, 720)
        self._set_icon()
        self.palette, self.fonts = apply_theme(self)
        self.worker = Worker()
        self._toast_job: str | None = None
        self._toast_frame: tk.Frame | None = None

        self._build_shell()
        self.workspace = WorkspaceView(self._page_host, self)
        self.pages: dict[str, tk.Frame] = {"forge": self.workspace}
        self._page_factories: dict[str, Any] = {
            "dashboard": lambda: DashboardView(
                self._page_host, self, navigate=self.show_page, start_task=self.start_task
            ),
            "files": lambda: FileWorkspacePage(self._page_host, self),
        }
        for key in ("agents", "scenarios", "library", "integrations", "monitoring"):
            self._page_factories[key] = (
                lambda page=key: ProductPage(self._page_host, self, page, self.start_task)
            )
        self._active_page = ""
        self.show_page(self._initial_page(view))
        self._build_status()

        self.worker.start(self)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._bind_keys()
        if offline_note:
            self.toast(offline_note, "warn", timeout=9000)

    def _build_shell(self) -> None:
        shell = tk.Frame(self, bg=Palette.bg)
        shell.pack(fill="both", expand=True)
        self._sidebar = tk.Frame(shell, bg=Palette.surface, width=218,
                                 highlightthickness=1, highlightbackground=Palette.border)
        self._sidebar.pack(side="left", fill="y")
        self._sidebar.pack_propagate(False)
        self._build_sidebar()
        main = tk.Frame(shell, bg=Palette.bg)
        main.pack(side="left", fill="both", expand=True)
        self._build_topbar(main)
        self._page_host = tk.Frame(main, bg=Palette.bg)
        self._page_host.pack(fill="both", expand=True)

    def _build_sidebar(self) -> None:
        fonts = self.fonts
        brand = tk.Frame(self._sidebar, bg=Palette.surface)
        brand.pack(fill="x", padx=15, pady=(18, 22))
        path = Path(__file__).resolve().parents[1] / "assets" / "gigaforge-mark.png"
        self._brand_logo = None
        try:
            if path.is_file():
                self._brand_logo = tk.PhotoImage(
                    master=self, data=base64.b64encode(path.read_bytes()).decode("ascii")
                )
                if self._brand_logo.width() > 48:
                    self._brand_logo = self._brand_logo.subsample(
                        max(1, (self._brand_logo.width() + 43) // 44)
                    )
        except (tk.TclError, OSError):
            self._brand_logo = None
        if self._brand_logo is not None:
            tk.Label(brand, image=self._brand_logo, bg=Palette.surface).pack(side="left")
        text = tk.Frame(brand, bg=Palette.surface)
        text.pack(side="left", padx=(7, 0))
        tk.Label(text, text="GigaForge", bg=Palette.surface, fg=Palette.text,
                 font=(fonts.ui, 14, "bold")).pack(anchor="w")
        tk.Label(text, text="AGENT HARNESS · V6", bg=Palette.surface,
                 fg=Palette.text_faint, font=fonts.tiny).pack(anchor="w", pady=(1, 0))

        self._nav_buttons: dict[str, NavButton] = {}
        entries = (
            ("dashboard", "⌂", "Главная"), ("forge", "✦", "Forge"),
            ("files", "▧", "Файлы проекта"),
            ("agents", "◈", "Агенты"), ("scenarios", "▷", "Сценарии"),
            ("library", "▤", "Библиотека"), ("integrations", "⇄", "Интеграции"),
            ("monitoring", "⌁", "Мониторинг"),
        )
        nav = tk.Frame(self._sidebar, bg=Palette.surface)
        nav.pack(fill="x", padx=9)
        for key, icon, label in entries:
            button = NavButton(
                nav, glyph=icon, label=label, fonts=fonts,
                command=lambda page=key: self.show_page(page),
            )
            button.pack(fill="x", pady=2)
            self._nav_buttons[key] = button

        tk.Frame(self._sidebar, bg=Palette.surface).pack(fill="both", expand=True)
        project = Card(self._sidebar, title="Текущий проект", fonts=fonts, pad=10,
                       bg=Palette.panel_alt)
        project.pack(fill="x", padx=12, pady=(8, 10))
        repo = Path(str(getattr(self.ctx.core_config, "repo_root", ".") or "."))
        self._repo_label = tk.Label(project.content, text=repo.name or str(repo), bg=Palette.panel_alt,
                                    fg=Palette.text, font=fonts.small_bold, anchor="w",
                                    cursor="hand2")
        self._repo_label.pack(fill="x", pady=(0, 2))
        self._repo_label.bind("<Button-1>", lambda _event: self.workspace.select_workspace()
                              if hasattr(self, "workspace") else None)
        self._repo_path_label = tk.Label(
            project.content, text=str(repo), bg=Palette.panel_alt, fg=Palette.text_muted,
            font=fonts.tiny, anchor="w", justify="left", wraplength=185,
        )
        self._repo_path_label.pack(fill="x", pady=(0, 4))
        SoftButton(self._sidebar, "⚙  Настройки", self.open_settings,
                   fonts=fonts).pack(fill="x", padx=13, pady=(0, 8))

    def _build_topbar(self, parent: tk.Misc) -> None:
        fonts = self.fonts
        bar = tk.Frame(parent, bg=Palette.surface, height=58,
                       highlightthickness=1, highlightbackground=Palette.border)
        bar.pack(fill="x")
        bar.pack_propagate(False)
        self._page_context_label = tk.Label(bar, text="Обзор", bg=Palette.surface,
                                            fg=Palette.text_muted, font=fonts.small)
        self._page_context_label.pack(side="left", padx=20)
        info = self.ctx.describe()
        ready = bool(info.get("can_run"))
        self._connection_badge = Badge(
            bar, "Подключено" if ready else "Настроить подключение",
            fonts=fonts, status="ok" if ready else "warn",
        )
        self._connection_badge.configure(cursor="hand2")
        self._connection_badge.bind("<Button-1>", lambda _event: self.open_settings())
        self._connection_badge.pack(side="right", padx=8, pady=12)
        model = str((info.get("models") or {}).get("strong")
                    or (info.get("models") or {}).get("default") or "GigaChat")
        self._model_badge = SoftButton(bar, model, self.open_settings, fonts=fonts, height=32)
        self._model_badge.pack(side="right", padx=(8, 18), pady=10)

    @staticmethod
    def _initial_page(view: str | None) -> str:
        requested = str(view or "").strip().lower()
        requested = {
            "home": "dashboard", "chat": "forge", "workspace": "forge",
            "runs": "monitoring", "diff": "forge", "index": "monitoring",
            "system": "integrations",
        }.get(requested, requested)
        choices = {"dashboard", "forge", "files", "agents", "scenarios", "library",
                   "integrations", "monitoring"}
        return requested if requested in choices else "dashboard"

    def show_page(self, name: str) -> None:
        page = self.pages.get(name)
        fresh = page is None
        if fresh:
            factory = self._page_factories.get(name)
            if factory is None:
                return
            page = factory()
            self.pages[name] = page
        if self._active_page:
            self.pages[self._active_page].pack_forget()
        self._active_page = name
        page.pack(fill="both", expand=True)
        titles = {
            "dashboard": "Обзор",
            "forge": "Рабочее пространство",
            "files": "Файлы проекта",
            "agents": "Команда агентов",
            "scenarios": "Сценарии",
            "library": "Библиотека",
            "integrations": "Интеграции",
            "monitoring": "Мониторинг",
        }
        self._page_context_label.configure(text=titles.get(name, name))
        for key, button in self._nav_buttons.items():
            button.set_active(key == name)
        if name == "forge":
            self.workspace.on_show()
        elif not fresh and hasattr(page, "refresh"):
            page.refresh()

    def start_task(self, prompt: str, mode: str = "goal") -> None:
        self.show_page("forge")
        self.workspace.set_mode(mode)
        self.workspace.submit(prompt)

    def open_browser_panel(self) -> None:
        def done(url: Any) -> None:
            try:
                import webbrowser

                webbrowser.open(str(url))
                self.toast(f"Панель мониторинга открыта: {url}", "ok")
            except Exception as exc:  # noqa: BLE001 - the server is still available by URL
                self.toast(f"Панель доступна по адресу {url}: {exc}", "warn")

        self.set_busy("Запуск панели мониторинга")
        self.worker.submit(
            "browser panel", lambda: self.ctx.open_browser_panel(), on_done=done,
            on_error=lambda exc: self.toast(f"Не удалось открыть панель: {exc}", "err"),
        )

    def refresh_shell_state(self) -> None:
        info = self.ctx.describe()
        ready = bool(info.get("can_run"))
        if hasattr(self, "_connection_badge"):
            self._connection_badge.set(
                "Подключено" if ready else "Настроить подключение",
                "ok" if ready else "warn",
            )
        model = str((info.get("models") or {}).get("strong")
                    or (info.get("models") or {}).get("default") or "GigaChat")
        if hasattr(self, "_model_badge"):
            self._model_badge.set_text(model)
        self.refresh_project_label()
        dashboard = getattr(self, "pages", {}).get("dashboard")
        if dashboard is not None and hasattr(dashboard, "refresh"):
            dashboard.refresh()

    def refresh_project_label(self) -> None:
        if not hasattr(self, "_repo_label"):
            return
        repo = Path(str(getattr(self.ctx.core_config, "repo_root", ".") or "."))
        self._repo_label.configure(text=repo.name or str(repo))
        self._repo_path_label.configure(text=str(repo))

    # ── status bar ───────────────────────────────────────────────────────
    def _build_status(self) -> None:
        bar = tk.Frame(self, bg=Palette.surface, height=24)
        bar.pack(fill="x", side="bottom")
        bar.pack_propagate(False)
        tk.Frame(bar, bg=Palette.border, height=1).pack(fill="x", side="top")
        self._dot = tk.Label(bar, text="●", bg=Palette.surface, fg=Palette.ok,
                             font=self.fonts.tiny)
        self._dot.pack(side="left", padx=(10, 4))
        self._status = tk.Label(bar, text="ready", bg=Palette.surface,
                                fg=Palette.text_muted, font=self.fonts.tiny, anchor="w")
        self._status.pack(side="left")
        self._busy = tk.Label(bar, text="", bg=Palette.surface, fg=Palette.accent_ink,
                              font=self.fonts.tiny)
        self._busy.pack(side="right", padx=(0, 10))
        tk.Label(bar, text="ctrl+1…8 navigation · ctrl+n new chat · ctrl+, settings · ctrl+q quit",
                 bg=Palette.surface, fg=Palette.text_faint, font=self.fonts.tiny).pack(
            side="right", padx=(0, 16))

    def set_status(self, message: str, kind: str = "idle") -> None:
        colour = {"ok": Palette.ok, "running": Palette.info, "warn": Palette.warn,
                  "err": Palette.err, "error": Palette.err}.get(kind, Palette.text_muted)
        text = message or "ready"
        if getattr(self, "_last_status", None) == (text, kind):
            return
        self._last_status = (text, kind)
        self._status.configure(text=text, fg=colour)
        self._dot.configure(fg=colour)

    def set_busy(self, label: str) -> None:
        self._busy.configure(text=f"⟳ {label}…" if label else "", )

    # ── feedback ─────────────────────────────────────────────────────────
    def toast(self, message: str, kind: str = "info", *, timeout: int = 4000) -> None:
        # A repeated toast used to destroy and re-create the plate every time,
        # which reads as flickering while a run streams events.
        if getattr(self, "_last_toast", None) == message:
            return
        self._last_toast = message
        colour = {"info": Palette.info, "ok": Palette.ok, "warn": Palette.warn,
                  "err": Palette.err}.get(kind, Palette.info)
        background = {"info": Palette.info_soft, "ok": Palette.ok_soft,
                      "warn": Palette.warn_soft, "err": Palette.err_soft}.get(
            kind, Palette.info_soft)
        if self._toast_frame is not None:
            try:
                self._toast_frame.destroy()
            except Exception:  # noqa: BLE001
                pass
        frame = tk.Frame(self, bg=Palette.border)
        frame.place(relx=1.0, rely=1.0, x=-18, y=-34, anchor="se")
        tk.Label(frame, text=message, bg=background, fg=colour, font=self.fonts.small,
                 padx=12, pady=7, justify="left", wraplength=520).pack(padx=1, pady=1)
        self._toast_frame = frame

        def hide() -> None:
            try:
                frame.destroy()
            except Exception:  # noqa: BLE001
                pass

        if self._toast_job:
            try:
                self.after_cancel(self._toast_job)
            except Exception:  # noqa: BLE001
                pass
        self._toast_job = self.after(timeout, hide)

    # ── settings ─────────────────────────────────────────────────────────
    def open_settings(self) -> None:
        SettingsDialog(self)

    # ── lifecycle ────────────────────────────────────────────────────────
    def _bind_keys(self) -> None:
        self.bind_all("<Control-n>", lambda _e: self.workspace.new_chat())
        self.bind_all("<Control-comma>", lambda _e: self.open_settings())
        self.bind_all("<Control-q>", lambda _e: self._on_close())
        for number, page in enumerate(("dashboard", "forge", "files", "agents", "scenarios",
                                       "library", "integrations", "monitoring"), start=1):
            self.bind_all(f"<Control-Key-{number}>",
                          lambda _event, key=page: self.show_page(key))

    def _set_icon(self) -> None:
        icon = Path(__file__).resolve().parents[1] / "assets" / "gigaforge.ico"
        try:
            if icon.is_file():
                self.iconbitmap(default=str(icon))
                self.iconbitmap(str(icon))
        except Exception:  # noqa: BLE001 - cosmetic
            pass

    def _on_close(self) -> None:
        try:
            self.workspace.shutdown()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.worker.stop()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.ctx.close()
        except Exception:  # noqa: BLE001
            pass
        self.destroy()


def run(*, repo_root: str | None = None, offline: bool = False,
        require_provider: bool = False, view: str | None = None,
        startup_note: str = "") -> int:
    """Open the window and block until it closes."""
    import logging

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                        datefmt="%H:%M:%S")
    notes = [str(startup_note or "").strip()] if startup_note else []
    try:
        ctx = AppContext.create(repo_root=repo_root, require_provider=require_provider)
    except Exception as exc:  # noqa: BLE001
        ctx = _fallback_context()
        notes.append(f"harness not fully wired: {type(exc).__name__}: {exc}")
    if ctx.offline:
        notes.append("read-only: no credential — chat answers need GIGAFORGE_AUTH_KEY")
    if offline:
        notes.append("scripted backend (--offline)")
    note = " · ".join(dict.fromkeys(notes))

    window = GigaForgeWindow(ctx, offline_note=note, view=view)

    # Dependency checks and first-time Chromium setup can take minutes on a new
    # machine. They must not hold the desktop window hostage before the mainloop.
    setup_result: "queue.Queue[tuple[str, str]]" = queue.Queue()

    def prepare_goal_runtime() -> None:
        try:
            from gigaforge.runtime_dependencies import (
                ensure_browser_runtime,
                ensure_goal_support_packages,
            )

            issues = [
                "Goal verifier setup needs attention: " + issue
                for issue in ensure_goal_support_packages(auto_install=True, timeout_s=60)
            ]
            browser = ensure_browser_runtime(auto_install=True, timeout_s=180)
            if not browser.available:
                issues.append("Browser verification setup needs attention: " + browser.detail)
            setup_result.put(("warn", " · ".join(issues)) if issues
                             else ("ok", "Goal runtime is ready"))
        except Exception as exc:  # noqa: BLE001 - Goal setup cannot prevent basic app use
            setup_result.put(("warn", f"Goal runtime setup failed: {type(exc).__name__}: {exc}"))

    def poll_goal_runtime() -> None:
        try:
            kind, message = setup_result.get_nowait()
        except queue.Empty:
            if window.winfo_exists():
                window.after(180, poll_goal_runtime)
            return
        if not window.winfo_exists():
            return
        window.set_status("Среда Goal готова" if kind == "ok" else "Среда Goal требует внимания",
                          kind)
        if kind != "ok":
            window.toast(message, "warn", timeout=12000)

    window.set_status("Подготовка среды Goal", "running")
    window.after(180, poll_goal_runtime)
    setup_thread = threading.Thread(target=prepare_goal_runtime,
                                    name="gigaforge-runtime-setup", daemon=True)
    setup_thread.start()
    window._runtime_setup_thread = setup_thread
    try:
        window.mainloop()
    finally:
        try:
            window.worker.stop()
        except Exception:  # noqa: BLE001
            pass
    return 0


class _NullConfig:
    repo_root = "."
    db_path = ".gigaforge/core.sqlite3"
    trace_path = ".gigaforge/traces/core.jsonl"
    default_model = "GigaChat"
    cheap_model = "GigaChat"
    strong_model = "GigaChat"
    tenant = "default"
    log_level = "INFO"


def _fallback_context() -> AppContext:
    from .context import _empty_snapshot

    class _NullRuntime:
        store = None
        core_config = _NullConfig()
        offline = True
        can_run = False
        client = None

        def snapshot(self) -> dict[str, Any]:
            return _empty_snapshot("runtime unavailable")

        def describe(self) -> dict[str, Any]:
            return {"provider": "none", "offline": True, "models": {},
                    "notes": ["the harness runtime could not be built"]}

        def chat_engine(self, **kwargs: Any) -> Any:
            from gigaforge.ui.chat import ChatEngine, ScriptedBackend
            from gigaforge.ui.store import UiStore

            return ChatEngine(UiStore(), backend=ScriptedBackend())

        def set_model(self, name: str) -> str:
            return name

        def set_think_mode(self, enabled: bool) -> dict[str, str]:
            return {}

        def aclose(self) -> None:
            return None

        close = aclose

    return AppContext(runtime=_NullRuntime(), offline=True)
