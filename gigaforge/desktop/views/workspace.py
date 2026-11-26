"""The workspace — one chat window, its history rail, a collapsible panel.

Layout, top to bottom:

``header``   app name, mode/model badges, the panel toggle
``panel``    collapsible: tokens, cost, cache, model, status, last run summary
``rail``     chat history (new / switch / rename / delete) + settings
``transcript`` the conversation, with step lines and diff summaries inline
``composer`` Chat ⇄ Agent mode, model picker, think, cache, send

Two verbs:

* **Chat** answers with the model.  It never touches the repository — asking
  "what can you do?" must not start a run.
* **Agent** drives the DAG so a request becomes a real edit, then reports the
  verdict, the changed files and a diff preview *in the transcript*.
"""
from __future__ import annotations

import json
import os
import queue
import re
import threading
import tkinter as tk
from pathlib import Path
from tkinter import simpledialog, ttk
from typing import Any, Dict, List, Optional

from ...workspace_paths import (
    SENSITIVE_WORKSPACE_DIRECTORIES,
    is_sensitive_workspace_path,
)
from ..preferences import load_preferences, save_preferences
from ..theme import Palette
from ..widgets import Badge, Card, SegmentedControl, SoftButton, dark_scrollbar

__all__ = ["WorkspaceView"]

CHAT_DIR = Path(".gigaforge/chat")

def _diff_summary(diff: str) -> str:
    """One line describing the shape of a diff for the approval dialog."""
    files = [line.split(" b/")[-1] for line in diff.splitlines()
             if line.startswith("diff --git ")]
    added = sum(1 for line in diff.splitlines()
                if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in diff.splitlines()
                  if line.startswith("-") and not line.startswith("---"))
    names = ", ".join(Path(name).name for name in files[:6]) or "—"
    if len(files) > 6:
        names += f" (+{len(files) - 6})"
    return f"Файлы: {names}\n+{added} / −{removed} строк"


_WRITE_REQUEST = re.compile(
    r"созда|сдела|исправ|измен|редакт|реализ|добав|удал|постро|доработ|"
    r"create|build|fix|edit|implement|refactor|write|modify|update|remove",
    re.IGNORECASE,
)
_REFERENCE_REQUEST = re.compile(
    r"как пример|для справки|референс|образец|read[- ]only|reference",
    re.IGNORECASE,
)
_IGNORED_PROJECT_DIRS = {
    ".git", ".gigaforge", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".venv", "venv", "env", "__pycache__", "node_modules", "dist", "build",
    "target", ".next", "coverage", "vendor",
} | SENSITIVE_WORKSPACE_DIRECTORIES
_PROJECT_SOURCE_SUFFIXES = {
    ".c", ".cc", ".cpp", ".cs", ".css", ".go", ".h", ".hpp", ".html",
    ".ini", ".java", ".js", ".json", ".jsx", ".kt", ".lua", ".md", ".mjs",
    ".php", ".py", ".rb", ".rs", ".scss", ".sh", ".sql", ".svelte", ".swift",
    ".toml", ".ts", ".tsx", ".txt", ".vue", ".xml", ".yaml", ".yml",
}


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _normalise_path_text(value: str) -> str:
    return str(value).replace("/", "\\").rstrip("\\").casefold()


def _mentioned_path(path: Path, text: str) -> bool:
    haystack = _normalise_path_text(text)
    needle = _normalise_path_text(str(path))
    return bool(needle and needle in haystack)


def _infer_workspace_root(goal: str, attachments: List[str], current_root: Path) -> Optional[Path]:
    """Choose an attached project folder only for an explicit write request."""
    if not _WRITE_REQUEST.search(goal) or _REFERENCE_REQUEST.search(goal):
        return None
    try:
        current = current_root.expanduser().resolve()
    except OSError:
        current = current_root.absolute()

    # An attached directory is a deliberate project selection. A file becomes a
    # project only when the task refers to that location or to its attachment.
    for raw in attachments:
        try:
            item = Path(raw).expanduser().resolve()
        except OSError:
            continue
        candidate = item if item.is_dir() else item.parent
        if not candidate.is_dir() or candidate == current or _is_under(candidate, current):
            continue
        explicit = item.is_dir() or _mentioned_path(item, goal) or _mentioned_path(candidate, goal)
        if not explicit and re.search(r"прикреп|вложен|attached", goal, re.IGNORECASE):
            explicit = True
        if explicit:
            return candidate

    # Handle a path typed into the task even if the user did not attach a chip.
    # Try progressively shorter prefixes so a path followed by prose is resolved
    # to the deepest existing directory/file rather than its drive root.
    for match in re.finditer(r"(?i)[A-Z]:[\\/][^\r\n\"'<>|]+", goal):
        raw = match.group(0).strip().rstrip(" .,;:!?)]}")
        words = raw.split()
        for end in range(len(words), 0, -1):
            candidate_text = " ".join(words[:end]).rstrip(" .,;:!?)]}")
            try:
                candidate = Path(candidate_text).expanduser().resolve()
            except OSError:
                continue
            if not candidate.exists():
                continue
            target = candidate if candidate.is_dir() else candidate.parent
            if target.is_dir() and target != current and not _is_under(target, current):
                return target
            break
    return None


def _project_files(root: Path, attachments: List[str], *, limit: int = 40) -> List[str]:
    """Return workspace-relative source paths for attached files/folders."""
    root = root.resolve()
    if is_sensitive_workspace_path(root.name):
        return []
    found: set[str] = set()
    for raw in attachments:
        try:
            item = Path(raw).expanduser().resolve()
            item_relative = item.relative_to(root).as_posix()
            if is_sensitive_workspace_path(item_relative):
                continue
        except (OSError, ValueError):
            continue
        candidates: List[Path] = []
        if item.is_file():
            candidates = [item]
        elif item.is_dir():
            for current, dirs, names in os.walk(item, followlinks=False):
                dirs[:] = sorted(
                    name for name in dirs
                    if name.casefold() not in _IGNORED_PROJECT_DIRS
                    and not (Path(current) / name).is_symlink()
                )
                for name in sorted(names):
                    candidate = Path(current) / name
                    if candidate.is_symlink():
                        continue
                    if candidate.suffix.casefold() not in _PROJECT_SOURCE_SUFFIXES:
                        continue
                    try:
                        relative = candidate.relative_to(root).as_posix()
                        resolved_relative = candidate.resolve().relative_to(root).as_posix()
                        if (is_sensitive_workspace_path(relative)
                                or is_sensitive_workspace_path(resolved_relative)):
                            continue
                        if candidate.stat().st_size > 2_000_000:
                            continue
                        found.add(relative)
                    except (OSError, ValueError):
                        continue
                    if len(found) >= limit:
                        break
                if len(found) >= limit:
                    break
        for candidate in candidates:
            try:
                if candidate.is_symlink():
                    continue
                relative = candidate.relative_to(root).as_posix()
                resolved_relative = candidate.resolve().relative_to(root).as_posix()
                if (is_sensitive_workspace_path(relative)
                        or is_sensitive_workspace_path(resolved_relative)):
                    continue
                if (candidate.suffix.casefold() in _PROJECT_SOURCE_SUFFIXES
                        and candidate.stat().st_size <= 2_000_000):
                    found.add(relative)
            except (OSError, ValueError):
                continue
        if len(found) >= limit:
            break
    return sorted(found)[:limit]


class WorkspaceView(tk.Frame):
    """The whole product surface."""

    # ── construction ─────────────────────────────────────────────────────
    def __init__(self, master: tk.Misc, app: Any) -> None:
        super().__init__(master, bg=Palette.bg, bd=0, highlightthickness=0)
        self.app = app
        self.ctx = app.ctx
        self.fonts = app.fonts
        self.palette = app.palette

        self._events: "queue.Queue[Any]" = queue.Queue()
        self._following = False
        self._closing = False
        self._session_id = ""
        self._mode = "chat"
        self._sidebar_width = load_preferences().get("chat_sidebar_width", 224)
        self._engines: Dict[str, Any] = {}
        self._panel_open = False
        self._max_tokens = 900
        self._think = False
        self._attachments: List[str] = []
        # History belongs to the repository it happened in: a CWD-relative path
        # showed another project's chats in the rail (and wrote new ones there).
        repo_root = Path(str(getattr(self.ctx.core_config, "repo_root", ".") or "."))
        self._chat_dir = (repo_root / ".gigaforge" / "chat").resolve()

        self._build_panel()
        self._build_body()
        self._build_composer(self._main)

    # ── header + collapsible panel ───────────────────────────────────────
    def _build_panel(self) -> None:
        head = tk.Frame(self, bg=Palette.surface)
        head.pack(fill="x")
        row = tk.Frame(head, bg=Palette.surface)
        row.pack(fill="x", padx=14, pady=(7, 5))

        self._chevron = tk.Label(row, text="▾", bg=Palette.surface, fg=Palette.text_muted,
                                 font=(self.fonts.mono[0], 11), cursor="hand2")
        self._chevron.pack(side="left")
        self._chevron.bind("<Button-1>", lambda _e: self.toggle_panel())
        tk.Label(row, text="GigaForge · Forge", bg=Palette.surface, fg=Palette.text,
                 font=self.fonts.logo).pack(side="left", padx=(8, 0))

        self._badge_state = Badge(row, "ready", fonts=self.fonts, status="idle")
        self._badge_state.pack(side="right")
        # The panel is a compact telemetry strip. Model and mode already have
        # dedicated controls in the composer, so repeating them here adds noise.
        self._panel = tk.Frame(head, bg=Palette.panel)
        self._panel.pack(fill="x", padx=14, pady=(0, 8))
        inner = tk.Frame(self._panel, bg=Palette.panel)
        inner.pack(fill="x", padx=12, pady=8)
        self._kpi: Dict[str, tk.Label] = {}
        cells = (
            ("tokens", "ТОКЕНЫ"), ("cost", "СТОИМОСТЬ"), ("cache", "КЭШ"),
            ("repo", "ПРОЕКТ"), ("last", "ПОСЛЕДНИЙ ЗАПУСК"),
        )
        for index, (key, label) in enumerate(cells):
            if index:
                separator = tk.Frame(inner, bg=Palette.border, width=1)
                separator.grid(row=0, column=index * 2 - 1, sticky="ns", padx=(10, 10))
            cell = tk.Frame(inner, bg=Palette.panel)
            cell.grid(row=0, column=index * 2, sticky="nsew")
            tk.Label(cell, text=label, bg=Palette.panel, fg=Palette.text_faint,
                     font=self.fonts.tiny, anchor="w").pack(fill="x")
            value = tk.Label(cell, text="—", bg=Palette.panel, fg=Palette.text,
                             font=self.fonts.body_bold, anchor="w", width=1)
            value.pack(fill="x")
            self._kpi[key] = value
            inner.grid_columnconfigure(index * 2, weight=1, uniform="kpi")
        self._kpi["repo"].configure(cursor="hand2")
        self._kpi["repo"].bind("<Button-1>", lambda _event: self.select_workspace())
        self._panel.pack_forget()
        self._chevron.configure(text="▸")

    def toggle_panel(self) -> None:
        self._panel_open = not self._panel_open
        if self._panel_open:
            self._panel.pack(fill="x", padx=14, pady=(0, 8))
            self._chevron.configure(text="▾")
        else:
            self._panel.pack_forget()
            self._chevron.configure(text="▸")

    def refresh_after_credentials(self) -> None:
        """A new credential changes the models, the state and the transcript view."""
        self._engines.clear()
        self._refresh_models()
        self._refresh_panel()
        try:
            self.app.refresh_shell_state()
        except Exception:  # noqa: BLE001 - shell badges are cosmetic
            pass
        try:
            info = self.ctx.describe()
            state = "read-only" if info.get("offline") else "runnable"
            self.app.set_status(f"credential applied · {state}", "ok")
        except Exception:  # noqa: BLE001
            pass

    def select_workspace(self) -> None:
        """Let the user explicitly choose which project the agent may edit."""
        from tkinter import filedialog, messagebox

        current = Path(str(self.ctx.core_config.repo_root or ".")).resolve()
        selected = filedialog.askdirectory(
            title="Выбрать папку проекта", initialdir=str(current), mustexist=True
        )
        if not selected:
            return
        try:
            self._activate_workspace(Path(selected))
        except Exception as exc:  # noqa: BLE001 - keep Tk callbacks contained
            messagebox.showerror("Не удалось открыть проект", str(exc), parent=self)

    def _activate_workspace(self, root: Path) -> Path:
        current = Path(str(self.ctx.core_config.repo_root or ".")).resolve()
        target = self.ctx.switch_workspace(root)
        if target == current:
            return target
        try:
            self.app.refresh_project_label()
        except Exception:  # noqa: BLE001 - project selection remains valid without chrome refresh
            pass
        self._chat_dir = target / ".gigaforge" / "chat"
        self._engines.clear()
        self._session_id = ""
        self._render_rail()
        self._refresh_models()
        self._refresh_panel()
        return target

    # ── body: rail + transcript ──────────────────────────────────────────
    def _build_body(self) -> None:
        self._split = tk.PanedWindow(
            self, orient="horizontal", bg=Palette.bg, bd=0,
            sashwidth=6, sashpad=2, sashrelief="flat",
            sashcursor="sb_h_double_arrow", showhandle=False,
            opaqueresize=True, relief="flat",
        )
        self._split.pack(fill="both", expand=True)
        self._rail = tk.Frame(self._split, bg=Palette.surface, width=self._sidebar_width)
        self._rail.pack_propagate(False)
        self._main = tk.Frame(self._split, bg=Palette.bg, bd=0, highlightthickness=0)
        self._split.add(self._rail, minsize=184, width=self._sidebar_width,
                        stretch="never", sticky="nsew")
        self._split.add(self._main, minsize=620, stretch="always", sticky="nsew")
        self._split.bind("<ButtonRelease-1>", self._persist_sidebar_width)

        bar = tk.Frame(self._rail, bg=Palette.surface)
        bar.pack(fill="x", padx=10, pady=(8, 4))
        tk.Label(bar, text="ЧАТЫ", bg=Palette.surface, fg=Palette.text_muted,
                 font=self.fonts.tiny).pack(side="left")
        SoftButton(bar, "📂 Проект", self.select_workspace, fonts=self.fonts,
                   height=28, font=self.fonts.tiny).pack(side="right", padx=(4, 0))
        SoftButton(bar, "+ Новый", self.new_chat, fonts=self.fonts, primary=True,
                   height=28, font=self.fonts.tiny).pack(side="right")
        self._list = tk.Frame(self._rail, bg=Palette.surface)
        self._list.pack(fill="both", expand=True, padx=6, pady=(2, 6))
        foot = tk.Frame(self._rail, bg=Palette.surface)
        foot.pack(fill="x", padx=10, pady=8)
        SoftButton(foot, "⚙  Настройки", self.app.open_settings,
                   fonts=self.fonts).pack(fill="x")

        self._main.grid_columnconfigure(0, weight=1)
        self._main.grid_rowconfigure(0, weight=1)
        wrap = Card(self._main, fonts=self.fonts, palette=self.palette, pad=0,
                    bg=Palette.surface)
        wrap.grid(row=0, column=0, sticky="nsew", padx=(8, 12), pady=(4, 4))
        inner = wrap.content
        self.text = tk.Text(inner, bg=Palette.surface, fg=Palette.text, bd=0,
                            highlightthickness=0, font=self.fonts.body, wrap="word",
                            padx=18, pady=14, insertbackground=Palette.accent,
                            selectbackground=Palette.select_bg,
                            selectforeground=Palette.text, spacing1=2, spacing3=6,
                            state="disabled")
        scroll = dark_scrollbar(inner, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=scroll.set)
        self.text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self._tags()

    def _persist_sidebar_width(self, _event: Any = None) -> None:
        try:
            width = max(184, min(420, int(self._split.sash_coord(0)[0])))
            if width == self._sidebar_width:
                return
            save_preferences(chat_sidebar_width=width)
            self._sidebar_width = width
        except (tk.TclError, OSError, ValueError):
            return

    def _tags(self) -> None:
        self.text.tag_configure("role_user", foreground=Palette.accent_ink,
                                font=self.fonts.small_bold, spacing1=12)
        self.text.tag_configure("role_assistant", foreground=Palette.violet_ink,
                                font=self.fonts.small_bold, spacing1=12)
        self.text.tag_configure("role_tool", foreground=Palette.text_muted,
                                font=self.fonts.small_bold, spacing1=8)
        self.text.tag_configure("role_error", foreground=Palette.err,
                                font=self.fonts.small_bold, spacing1=8)
        self.text.tag_configure("role_system", foreground=Palette.text_faint,
                                font=self.fonts.small, spacing1=8)
        self.text.tag_configure("body", foreground=Palette.text, lmargin1=2, lmargin2=2)
        self.text.tag_configure("code", foreground=Palette.accent_ink, font=self.fonts.code)
        self.text.tag_configure("muted", foreground=Palette.text_faint, font=self.fonts.tiny)
        self.text.tag_configure("ok", foreground=Palette.ok, font=self.fonts.code_small)
        self.text.tag_configure("run", foreground=Palette.blue_ink, font=self.fonts.code_small)
        self.text.tag_configure("fail", foreground=Palette.err, font=self.fonts.code_small)
        self.text.tag_configure("diff_add", foreground=Palette.diff_add_fg,
                                background=Palette.diff_add_bg, font=self.fonts.code_small)
        self.text.tag_configure("diff_del", foreground=Palette.diff_del_fg,
                                background=Palette.diff_del_bg, font=self.fonts.code_small)

    # ── composer ─────────────────────────────────────────────────────────
    def _build_composer(self, parent: tk.Misc) -> None:
        self._composer_lane = tk.Frame(parent, bg=Palette.bg, height=164,
                                       bd=0, highlightthickness=0)
        self._composer_lane.grid(row=1, column=0, sticky="ew")
        self._composer_lane.grid_propagate(False)
        self._composer_lane.bind("<Configure>", self._center_composer)
        self._composer = Card(self._composer_lane, fonts=self.fonts,
                              palette=self.palette, pad=10, bg=Palette.panel)
        self._composer.place(x=0, y=4, width=700, height=148)
        box = self._composer.content

        title_row = tk.Frame(box, bg=Palette.panel)
        title_row.pack(fill="x", padx=10, pady=(5, 1))
        tk.Label(title_row, text="Ваша следующая задача", bg=Palette.panel,
                 fg=Palette.text, font=self.fonts.small_bold).pack(side="left")
        tk.Label(title_row, text="Enter · отправить    Shift+Enter · новая строка",
                 bg=Palette.panel, fg=Palette.text_faint,
                 font=self.fonts.tiny).pack(side="right")

        entry_wrap = tk.Frame(box, bg=Palette.panel)
        entry_wrap.pack(fill="both", expand=True, padx=10, pady=(2, 1))
        self._placeholder = "Напишите задачу или задайте вопрос…"
        self.entry = tk.Text(entry_wrap, bg=Palette.panel, fg=Palette.text_faint,
                             bd=0, highlightthickness=0, font=self.fonts.body,
                             height=2, wrap="word", padx=1, pady=2,
                             insertbackground=Palette.accent)
        self.entry.pack(fill="both", expand=True)
        self.entry.insert("1.0", self._placeholder)
        self.entry.bind("<FocusIn>", self._clear_placeholder)
        self.entry.bind("<FocusOut>", self._restore_placeholder)
        self.entry.bind("<Return>", self._on_return)

        self._chips_row = tk.Frame(box, bg=Palette.panel)
        self._chips_row.pack(fill="x", padx=10, pady=(0, 2))

        controls = tk.Frame(box, bg=Palette.panel)
        controls.pack(fill="x", padx=10, pady=(1, 3))
        self._mode_switch = SegmentedControl(
            controls,
            (("chat", "Чат", "✧"), ("agent", "Агент", "◈"),
             ("goal", "Goal", "⟳")),
            self.set_mode, fonts=self.fonts, selected=self._mode,
            palette=self.palette, height=36,
        )
        self._mode_switch.pack(side="left")
        SoftButton(controls, "＋  Файл", self.pick_attachment, fonts=self.fonts,
                   height=30, font=self.fonts.tiny).pack(side="left", padx=(8, 0))

        self._think_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(controls, text="Рассуждение", variable=self._think_var,
                        command=self._on_think,
                        style="Panel.TCheckbutton").pack(side="right", padx=(8, 0))
        self._cache_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(controls, text="Кэш", variable=self._cache_var,
                        style="Panel.TCheckbutton").pack(side="right", padx=(8, 0))
        self._model_picker = ttk.Combobox(controls, state="readonly", width=16,
                                          font=self.fonts.code_small)
        self._model_picker.pack(side="right", padx=(8, 0))
        self._model_picker.bind("<<ComboboxSelected>>", self._on_model)

        self._send = SoftButton(controls, "➤", self._on_send,
                                fonts=self.fonts, primary=True, height=34)
        self._send.pack(side="right", padx=(8, 0))
        self._stop = SoftButton(controls, "Стоп", self.stop, fonts=self.fonts,
                                height=30, font=self.fonts.tiny)
        self._stop.configure(state="disabled")

        # Drag & drop: a file or folder dropped anywhere on the transcript or the
        # composer becomes an attachment.  Needs tkinterdnd2; without it the 📎
        # button and pasting a path still work.
        self._dnd_enabled = False
        try:
            for target in (self.text, self.entry):
                target.drop_target_register("DND_Files")  # type: ignore[attr-defined]
                target.dnd_bind("<<Drop>>", self._on_drop)  # type: ignore[attr-defined]
            self._dnd_enabled = True
        except Exception:  # noqa: BLE001 - optional dependency
            self._dnd_enabled = False

    def _center_composer(self, event: Any) -> None:
        if not hasattr(self, "_composer"):
            return
        available = max(360, int(event.width) - 40)
        width = min(1040, available)
        self._composer.place(x=max(20, (int(event.width) - width) // 2),
                             y=4, width=width, height=148)

    def _clear_placeholder(self, _event: Any = None) -> None:
        if self.entry.get("1.0", "end").strip() == self._placeholder:
            self.entry.delete("1.0", "end")
            self.entry.configure(fg=Palette.text)

    def _restore_placeholder(self, _event: Any = None) -> None:
        if not self.entry.get("1.0", "end").strip():
            self.entry.insert("1.0", self._placeholder)
            self.entry.configure(fg=Palette.text_faint)

    def _paint_modes(self) -> None:
        if hasattr(self, "_mode_switch"):
            self._mode_switch.set_selected(self._mode)

    # ── attachments ──────────────────────────────────────────────────────
    def _on_drop(self, event: Any) -> str:
        try:
            raw = self.tk.splitlist(event.data)
        except Exception:  # noqa: BLE001
            raw = [str(getattr(event, "data", ""))]
        for item in raw:
            self.attach(str(item))
        return "break"

    # ── approval gate ────────────────────────────────────────────────────
    def _ask_approval(self, diff: str) -> bool:
        """Asked on the pipeline thread, answered on the Tk thread.

        Returns ``False`` unless the user explicitly approves within the timeout —
        a gate that defaults to "yes" when nobody answers is not a gate.
        """
        import threading
        from tkinter import messagebox

        summary = _diff_summary(diff)
        answer: Dict[str, bool] = {}
        done = threading.Event()

        def ask() -> None:
            try:
                answer["ok"] = bool(messagebox.askokcancel(
                    "Применить изменения?",
                    f"{summary}\n\nDiff ({len(diff)} символов) будет применён к репозиторию.",
                    parent=self.winfo_toplevel(),
                ))
            finally:
                done.set()

        self.after(0, ask)
        done.wait(300)
        return bool(answer.get("ok"))

    def pick_attachment(self) -> None:
        from tkinter import filedialog

        path = filedialog.askopenfilename(title="Прикрепить файл")
        if path:
            self.attach(path)
            return
        folder = filedialog.askdirectory(title="Прикрепить папку")
        if folder:
            self.attach(folder)

    def attach(self, path: str) -> None:
        """Attach a file or a directory to the next message.

        Keep directory boundaries intact. Flattening a folder to eight arbitrary
        files discarded the project root and made edits land in the app's default
        repository instead of the attached project.
        """
        candidate = Path(str(path).strip().strip('"'))
        if not candidate.exists():
            self.app.toast(f"not found: {candidate}", "warn")
            return
        try:
            candidate = candidate.resolve()
        except OSError:
            candidate = candidate.absolute()
        item = str(candidate)
        if item not in self._attachments:
            self._attachments.append(item)
        self._render_attachments()
        label = candidate.name + ("/*" if candidate.is_dir() else "")
        self.app.toast(f"📎 {label} attached", "ok")

    def _render_attachments(self) -> None:
        for child in self._chips_row.winfo_children():
            child.destroy()
        if not self._attachments:
            return
        for item in list(self._attachments):
            chip = tk.Frame(self._chips_row, bg=Palette.panel_alt)
            chip.pack(side="left", padx=(0, 5))
            tk.Label(chip, text=f"📎 {Path(item).name}", bg=Palette.panel_alt,
                     fg=Palette.accent_ink, font=self.fonts.tiny, padx=6, pady=1).pack(side="left")
            remove = tk.Label(chip, text="×", bg=Palette.panel_alt, fg=Palette.text_faint,
                              font=self.fonts.tiny, padx=4, cursor="hand2")
            remove.pack(side="left")
            remove.bind("<Button-1>", lambda _e, p=item: self._detach(p))

    def _detach(self, path: str) -> None:
        if path in self._attachments:
            self._attachments.remove(path)
        self._render_attachments()

    def _compose_goal(self, text: str, workspace_root: Optional[Path] = None) -> str:
        """Build a prompt with explicit workspace-relative edit paths."""
        if not self._attachments and self._mode == "chat":
            return text
        if self._mode == "chat":
            lines = [text.rstrip(), "", "Attached references:"]
            files: List[Path] = []
            for raw in self._attachments:
                item = Path(raw)
                if item.is_file():
                    files.append(item)
                elif item.is_dir():
                    files.extend(item / rel for rel in _project_files(item, [str(item)], limit=4))
            for item in files[:4]:
                lines.append(f"- {item}")
                try:
                    body = item.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                lines.append("")
                lines.append(f"```{item.suffix.lstrip('.') or 'text'} {item.name}")
                lines.append(body[:6000])
                lines.append("```")
            return "\n".join(lines)

        root = (workspace_root or Path(str(self.ctx.core_config.repo_root or "."))).resolve()
        message = text
        replacements: List[tuple[str, str]] = [(str(root), "project root")]
        for raw in self._attachments:
            item = Path(raw).expanduser()
            try:
                resolved = item.resolve()
            except OSError:
                resolved = item.absolute()
            if _is_under(resolved, root):
                label = ("project root" if resolved == root else
                         resolved.relative_to(root).as_posix())
                replacements.append((str(resolved), label))
        for original, replacement in sorted(replacements, key=lambda pair: -len(pair[0])):
            variants = {
                original,
                original.replace("\\", "/"),
                original.replace("/", "\\"),
            }
            for variant in sorted(variants, key=len, reverse=True):
                message = re.sub(re.escape(variant), replacement, message, flags=re.IGNORECASE)

        project_files = _project_files(root, self._attachments)
        lines = [
            message.rstrip(), "",
            "Project workspace: the selected project root.",
            "Use workspace-relative paths in every diff; never emit absolute paths.",
        ]
        if project_files:
            lines.extend(["Attached project files (workspace-relative):",
                          *[f"- {path}" for path in project_files]])
            if len(project_files) >= 40:
                lines.append("- [additional project files omitted from this list]")
        external = []
        for raw in self._attachments:
            item = Path(raw).expanduser()
            try:
                resolved = item.resolve()
            except OSError:
                resolved = item.absolute()
            if not _is_under(resolved, root) and resolved.is_file():
                external.append(str(resolved))
        if external:
            lines.extend([
                "Attached external files are read-only references; edit only files "
                "inside the project workspace:",
                *[f"- {path}" for path in external[:8]],
            ])
        return "\n".join(lines)

    def _clear_attachments(self) -> None:
        self._attachments.clear()
        self._render_attachments()


    # ── mode / model / think ─────────────────────────────────────────────
    def set_mode(self, mode: str) -> None:
        self._mode = mode if mode in {"agent", "goal"} else "chat"
        self._paint_modes()
        mode_label = {"chat": "чат", "agent": "агент", "goal": "долгий цикл"}.get(
            self._mode, self._mode
        )
        self.app.set_status(f"режим: {mode_label}", "idle")
        self.entry.focus_set()

    def set_model(self, name: str) -> None:
        try:
            self.ctx.set_model(name)
        except Exception:  # noqa: BLE001 - the label still updates
            pass
        backend = getattr(self._engines.get("chat"), "backend", None)
        if backend is not None and hasattr(backend, "set_model"):
            backend.set_model(name)
        try:
            self.app.refresh_shell_state()
        except Exception:  # noqa: BLE001 - model selection must not depend on shell chrome
            pass

    def sync_think(self, enabled: bool) -> None:
        self._think = bool(enabled)
        self._think_var.set(self._think)

    def sync_approval(self) -> None:
        """Apply the current persisted permission mode to cached agent engines."""
        for mode in ("agent", "goal"):
            engine = self._engines.get(mode)
            if engine is not None:
                self._sync_engine_approval(mode, engine)

    def set_max_tokens(self, value: int) -> None:
        self._max_tokens = max(64, int(value))
        backend = getattr(self._engines.get("chat"), "backend", None)
        if backend is not None and hasattr(backend, "max_tokens"):
            backend.max_tokens = self._max_tokens

    def _on_model(self, _event: Any = None) -> None:
        name = self._model_picker.get()
        if name:
            self.set_model(name)
            self.app.toast(f"model → {name}", "ok")

    def _on_think(self) -> None:
        self.sync_think(bool(self._think_var.get()))
        try:
            self.ctx.set_think_mode(self._think)
        except Exception as exc:  # noqa: BLE001
            self.app.toast(f"think mode failed: {exc}", "err")
            return
        self.app.toast("think mode " + ("on" if self._think else "off"), "ok")

    # ── lifecycle ────────────────────────────────────────────────────────
    def on_show(self) -> None:
        self._refresh_models()
        self._render_rail()
        self._refresh_panel()
        if not self.text.get("1.0", "end").strip():
            self._append("system",
                         "Chat: вопрос → ответ. Agent: один цикл правки. Goal: выполнить, "
                         "запустить проверки и улучшать до результата. Enter — отправить, "
                         "Shift+Enter — новая строка.")
        self.entry.focus_set()
        self._pump()

    def shutdown(self) -> None:
        self._closing = True

    def new_chat(self) -> None:
        self._session_id = ""
        self._engines.clear()
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")
        self._append("system", "Новый чат.")
        self._render_rail()

    def _ensure_engine(self, mode: str) -> Any:
        engine = self._engines.get(mode)
        if engine is not None:
            self._sync_engine_approval(mode, engine)
            return engine
        kwargs: Dict[str, Any] = {"mode": mode, "chat_dir": str(self._chat_dir)}
        if self._session_id:
            kwargs["session_id"] = self._session_id
        if mode == "chat":
            model = self._model_picker.get() or None
            if model:
                kwargs["model"] = model
        engine = self.ctx.chat_engine(**kwargs)
        if mode == "chat":
            backend = getattr(engine, "backend", None)
            if backend is not None:
                backend.max_tokens = self._max_tokens
        else:
            self._sync_engine_approval(mode, engine)
        self._engines[mode] = engine
        self._session_id = getattr(engine.session, "session_id", self._session_id)
        self._render_rail()
        return engine

    def _sync_engine_approval(self, mode: str, engine: Any) -> None:
        if mode not in {"agent", "goal"}:
            return
        backend = getattr(engine, "backend", None)
        if backend is not None and hasattr(backend, "approve"):
            backend.approve = self._ask_approval if self.ctx.approve_changes else None

    # ── rail ─────────────────────────────────────────────────────────────
    def _sessions(self) -> List[tuple[str, str, int]]:
        out: List[tuple[str, str, int]] = []
        try:
            files = sorted(self._chat_dir.glob("*.jsonl"),
                           key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            return out
        for path in files:
            session_id = path.stem
            label, count = session_id, 0
            try:
                for line in path.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    count += 1
                    payload = json.loads(line)
                    data = payload.get("data") or {}
                    text = str(payload.get("text") or "")
                    if (label == session_id and payload.get("kind") == "command"
                            and data.get("role") == "user" and text
                            and not text.startswith("/")):
                        label = " ".join(text.split())[:32]
            except (OSError, ValueError):
                pass
            out.append((session_id, label, count))
        return out

    def _render_rail(self) -> None:
        for child in self._list.winfo_children():
            child.destroy()
        sessions = self._sessions()
        if not sessions:
            tk.Label(self._list, text="пусто — начните чат", bg=Palette.surface,
                     fg=Palette.text_faint, font=self.fonts.tiny).pack(anchor="w", padx=6)
            return
        for session_id, label, count in sessions[:40]:
            active = session_id == self._session_id
            row = tk.Frame(self._list, bg=Palette.panel_alt if active else Palette.surface,
                           cursor="hand2")
            row.pack(fill="x", pady=1)
            tk.Frame(row, bg=Palette.accent if active else Palette.surface,
                     width=3).pack(side="left", fill="y")
            text = tk.Label(row, text=label, bg=row["bg"],
                            fg=Palette.text if active else Palette.text_dim,
                            font=self.fonts.small, anchor="w")
            text.pack(side="left", fill="x", expand=True, padx=(6, 2), pady=4)
            tk.Label(row, text=str(count), bg=row["bg"], fg=Palette.text_faint,
                     font=self.fonts.tiny).pack(side="right", padx=(0, 4))
            delete = tk.Label(row, text="×", bg=row["bg"], fg=Palette.text_faint,
                              font=self.fonts.small, cursor="hand2")
            delete.pack(side="right", padx=(0, 4))
            delete.bind("<Button-1>", lambda _e, s=session_id: self._delete_chat(s))
            for widget in (row, text):
                widget.bind("<Button-1>", lambda _e, s=session_id: self.open_chat(s))
            text.bind("<Double-1>", lambda _e, s=session_id: self._rename_chat(s))

    def open_chat(self, session_id: str) -> None:
        self._session_id = session_id
        self._engines.clear()
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")
        self._load_transcript(session_id)
        self._render_rail()

    def _load_transcript(self, session_id: str) -> None:
        path = self._chat_dir / f"{session_id}.jsonl"
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        for line in lines[-200:]:
            try:
                payload = json.loads(line)
            except ValueError:
                continue
            kind = str(payload.get("kind") or "say")
            text = str(payload.get("text") or "")
            data = payload.get("data") or {}
            if kind == "step":
                state = str(data.get("status") or "")
                if state == "running":
                    continue
                self._write(f"  ● {data.get('node', '?')}: {state}\n",
                            "ok" if state in {"succeeded", "ok"} else "fail")
            elif text:
                self._append("user" if data.get("role") == "user"
                             else ("assistant" if kind in {"say", "assistant", "text"}
                                   else kind), text)

    def _rename_chat(self, session_id: str) -> None:
        name = simpledialog.askstring("Переименовать чат", "Название:", initialvalue=session_id)
        if not name or not name.strip() or name.strip() == session_id:
            return
        target = self._chat_dir / f"{name.strip().replace(' ', '-')}.jsonl"
        try:
            (self._chat_dir / f"{session_id}.jsonl").replace(target)
            self.app.toast(f"→ {target.stem}", "ok")
        except OSError as exc:
            self.app.toast(f"rename failed: {exc}", "err")
        if self._session_id == session_id:
            self._session_id = target.stem
        self._render_rail()

    def _delete_chat(self, session_id: str) -> None:
        try:
            (self._chat_dir / f"{session_id}.jsonl").unlink()
        except OSError:
            return
        if self._session_id == session_id:
            self.new_chat()
        else:
            self._render_rail()
        self.app.toast("chat deleted", "warn")

    # ── panel data ───────────────────────────────────────────────────────
    def _refresh_panel(self) -> None:
        snap = self.ctx.snapshot()
        kpis = snap.get("kpis") or {}
        tokens = kpis.get("tokens") or {}
        cost = kpis.get("cost") or {}
        cache = kpis.get("cache") or {}
        run = snap.get("run") or {}
        self._kpi["tokens"].configure(text=f"{int(tokens.get('total') or 0):,}")
        self._kpi["cost"].configure(text=str(cost.get("display") or "$0.00"))
        hit = float(cache.get("hit_rate") or 0.0)
        self._kpi["cache"].configure(text=f"{hit * 100:.0f}%",
                                     fg=Palette.ok if hit > 0.2 else Palette.text)
        repo = Path(str(self.ctx.core_config.repo_root or ".")).resolve()
        self._kpi["repo"].configure(text=repo.name or str(repo))
        status = str(run.get("status") or "").strip()
        accepted = bool(run.get("accepted"))
        if not status or status.lower() in {"unknown", "none"}:
            # No run yet: the useful state is whether the harness can act at all.
            try:
                offline = bool((self.ctx.describe() or {}).get("offline"))
            except Exception:  # noqa: BLE001
                offline = False
            self._kpi["last"].configure(text="no runs yet", fg=Palette.text_faint)
            self._badge_state.set("read-only" if offline else "live",
                                  "warn" if offline else "ok")
        else:
            self._kpi["last"].configure(
                text=("accepted" if accepted else status),
                fg=Palette.ok if accepted else Palette.text_dim,
            )
            self._badge_state.set("accepted" if accepted else status,
                                  "ok" if accepted else "warn")

    def _refresh_models(self) -> None:
        models: List[str] = []
        try:
            info = self.ctx.describe()
            models = list(info.get("models_live") or [])
            roles = info.get("model_roles") or {}
        except Exception:  # noqa: BLE001
            roles = {}
        if not models:
            try:
                from gigaforge.core.models import load_catalog

                catalog = load_catalog(Path(self.ctx.core_config.db_path).parent
                                       / "models.json", ttl_s=1e9)
                models = list(catalog.models) if catalog else []
                roles = catalog.roles.to_dict() if catalog else {}
            except Exception:  # noqa: BLE001
                models, roles = [], {}
        if not models:
            models = [str(getattr(self.ctx.core_config, "strong_model", "GigaChat"))]
        self._model_picker.configure(values=models)
        current = str(roles.get("strong") or models[0])
        self._model_picker.set(current if current in models else models[0])

    # ── sending ──────────────────────────────────────────────────────────
    def _on_return(self, event: Any) -> str:
        if event.state & 0x0001:
            return ""
        self._on_send()
        return "break"

    def _on_send(self) -> None:
        text = self.entry.get("1.0", "end").strip()
        if not text or text == self._placeholder or self._following:
            return
        self.entry.delete("1.0", "end")
        self.entry.configure(fg=Palette.text)
        self.submit(text)

    def submit(self, text: str) -> None:
        text = (text or "").strip()
        if not text or self._following:
            return
        attachments = list(self._attachments)
        if self._mode in {"agent", "goal"}:
            current_root = Path(str(self.ctx.core_config.repo_root or ".")).resolve()
            target_root = _infer_workspace_root(text, attachments, current_root)
            if target_root is not None:
                try:
                    selected = self._activate_workspace(target_root)
                except Exception as exc:  # noqa: BLE001 - never run against a fallback root
                    self.app.toast(f"project could not be opened: {exc}", "err")
                    self.entry.insert("1.0", text)
                    return
                if selected != current_root:
                    self.app.toast(f"project workspace: {selected}", "ok")
        root = Path(str(self.ctx.core_config.repo_root or ".")).resolve()
        goal = self._compose_goal(text, root)
        engine = self._ensure_engine(self._mode)
        self._append("user", text)
        if attachments:
            self._write("  📎 " + "  ·  ".join(Path(p).name for p in attachments)
                        + "\n", "muted")
        self._clear_attachments()
        if text.startswith("/") and text.split()[0] == "/clear":
            self.new_chat()
            return

        self._following = True
        self._send.configure(state="disabled")
        self._stop.configure(state="normal")
        self._stop.pack(side="right", padx=(8, 0))
        self._badge_state.set("working", "running")
        self.app.set_status(f"{self._mode}: {text[:60]}", "running")

        backend = getattr(engine, "backend", None)
        if backend is not None and self._mode in {"agent", "goal"}:
            try:
                run_kwargs = dict(getattr(backend, "run_kwargs", {}) or {})
                if self._mode == "agent":
                    run_kwargs["use_cache"] = bool(self._cache_var.get())
                run_kwargs["files"] = tuple(_project_files(root, attachments))
                backend.run_kwargs = run_kwargs
            except Exception:  # noqa: BLE001
                pass

        def pump() -> None:
            try:
                for event in engine.stream(goal):
                    self._events.put(event)
            except Exception as exc:  # noqa: BLE001
                self._events.put(_Failure(f"{type(exc).__name__}: {exc}"))
            finally:
                self._events.put(None)

        threading.Thread(target=pump, name="gigaforge-chat", daemon=True).start()
        self._pump()

    def stop(self) -> None:
        if not self._following:
            return
        self._following = False
        self._stop.configure(state="disabled")
        self._stop.pack_forget()
        self._send.configure(state="normal")
        self._append("system", "отсоединился от потока; задача выполняется в фоне.")
        self._badge_state.set("detached", "warn")
        while True:
            try:
                self._events.get_nowait()
            except queue.Empty:
                break

    def _pump(self) -> None:
        if self._closing:
            return
        finished = False
        handled = 0
        while handled < 64:
            try:
                event = self._events.get_nowait()
            except queue.Empty:
                break
            handled += 1
            if event is None:
                finished = True
                break
            self._render_event(event)
        if finished or not self._following:
            if self._following:
                self._following = False
                self._stop.configure(state="disabled")
                self._stop.pack_forget()
                self._send.configure(state="normal")
                self._finish_turn()
            return
        self.after(70, self._pump)

    def _finish_turn(self) -> None:
        engine = self._engines.get(self._mode)
        result = getattr(getattr(engine, "backend", None), "last_result", {}) or {}
        if not isinstance(result, dict):
            result = {}
        accepted = bool(result.get("accepted"))
        if result.get("mode") == "chat":
            self._badge_state.set("answered", "ok")
            self.app.set_status("answered", "ok")
        else:
            self._badge_state.set("accepted" if accepted else
                                  str(result.get("status") or "done"),
                                  "ok" if accepted else "warn")
            self.app.set_status("run finished" if accepted else "run not accepted",
                                "ok" if accepted else "warn")
            self._report_run(result)
        self.ctx.invalidate()
        self._refresh_panel()
        self._render_rail()

    def _report_run(self, result: Dict[str, Any]) -> None:
        """Inline run report: verdict, files, diff preview — in the transcript."""
        root = Path(str(getattr(self.ctx.core_config, "repo_root", ".") or ".")).resolve()

        if result.get("goal_id"):
            cycles = result.get("cycles") or []
            lines: List[tuple[str, str]] = [
                (f"  goal {result.get('goal_id')} · {result.get('status') or 'unknown'} · "
                 f"{'verified' if result.get('accepted') else 'not verified'} · "
                 f"{len(cycles)} cycles · {int(result.get('tokens') or 0):,} tokens",
                 "ok" if result.get("accepted") else "fail")
            ]
            for cycle in cycles:
                checks = (cycle.get("verification") or {}).get("checks") or []
                outcomes = ", ".join(
                    f"{item.get('name')}: {item.get('status')}" for item in checks
                )
                if not outcomes:
                    detail = str(cycle.get("error") or "no validation details")
                    outcomes = "pipeline rejected — " + detail[:260]
                lines.append((f"  cycle {cycle.get('index')}: {outcomes}", "muted"))
            if result.get("error"):
                lines.append((f"  detail: {result.get('error')}", "fail"))
            for text, tag in lines:
                self._write(text + "\n", tag)
            return

        def rel(path: str) -> str:
            try:
                return str(Path(path).resolve().relative_to(root)).replace("\\", "/")
            except Exception:  # noqa: BLE001 - keep the path as reported
                return str(path)

        changed = [rel(p) for p in (result.get("changed_files") or [])]
        diff = str(result.get("final_diff") or "")
        lines: List[tuple[str, str]] = [
            (f"  run {result.get('run_id') or '—'} · "
             f"{'accepted' if result.get('accepted') else 'not accepted'} · "
             f"{int(result.get('tokens') or 0):,} tokens", "ok")
        ]
        if changed:
            lines.append(("  files: " + ", ".join(changed), "muted"))
        preview = [line for line in diff.splitlines()
                   if line.startswith(("+++", "@@", "+", "-")) and not line.startswith(("+++", "---"))]
        for line in preview[:24]:
            lines.append((f"  {line}", "diff_add" if line.startswith("+") else "diff_del"))
        if len(preview) > 24:
            lines.append((f"  … {len(preview) - 24} more diff lines", "muted"))
        for text, tag in lines:
            self._write(text + "\n", tag)

    # ── transcript ───────────────────────────────────────────────────────
    def _render_event(self, event: Any) -> None:
        if isinstance(event, _Failure):
            self._append("error", str(event))
            return
        kind = str(getattr(event, "kind", "") or "system")
        text = str(getattr(event, "text", "") or "")
        data = getattr(event, "data", {}) or {}
        if kind == "step":
            state = str(data.get("status") or "")
            if state in {"running", ""}:
                return
            duration = data.get("duration_ms")
            suffix = f"  {float(duration):,.0f} ms" if duration else ""
            tag = "ok" if state in {"succeeded", "ok"} else "fail"
            self._write(f"  ● {data.get('node', '?'):<10} {state}{suffix}\n", tag)
            return
        if kind == "tool":
            name = str(data.get("name") or "")
            if name in {"model", "pipeline", "snapshot"}:
                return
            self._append("tool", text or name)
            return
        if kind == "error":
            self._append("error", text or "error")
            return
        if kind == "command":
            return
        self._append("assistant", text)

    def _append(self, kind: str, text: str) -> None:
        if not (text or "").strip():
            return
        role = kind if kind in {"user", "assistant", "tool", "error"} else "system"
        self.text.configure(state="normal")
        self.text.insert("end", f"\n{role}\n", f"role_{role}")
        self.text.insert("end", text.strip() + "\n",
                         "code" if text.lstrip().startswith(("/", "$")) else "body")
        self.text.configure(state="disabled")
        self.text.see("end")

    def _write(self, text: str, tag: str) -> None:
        self.text.configure(state="normal")
        self.text.insert("end", text, tag)
        self.text.configure(state="disabled")
        self.text.see("end")


class _Failure:
    __slots__ = ("message",)

    def __init__(self, message: str) -> None:
        self.message = message

    def __str__(self) -> str:
        return self.message
