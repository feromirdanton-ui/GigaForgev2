"""Settings and credentials dialogs.

Kept out of :mod:`gigaforge.desktop.app` because the window's shell and the
configuration forms change for different reasons.  Two dialogs:

* :class:`SettingsDialog` — the hub: connection status, model selection, reasoning
  mode, extra providers, health.
* :class:`CredentialsDialog` — opened from the connection row: a small, focused
  form for the key and endpoint, with *test before save*.  Entering a credential
  is the one flow that must never require editing a file by hand.
"""
from __future__ import annotations

import os
import tkinter as tk
from pathlib import Path
from tkinter import ttk
from typing import Any, Dict

from gigaforge.env import update_env_file

from .theme import Palette
from .widgets import Card, CodeView

__all__ = ["SettingsDialog", "CredentialsDialog"]

class SettingsDialog(tk.Toplevel):
    """One scrollable page of settings, opened from the rail."""

    def __init__(self, app: Any) -> None:
        super().__init__(app)
        self.app = app
        self.title("Settings")
        self.configure(bg=Palette.bg)
        self.geometry("780x740")
        self.minsize(680, 600)
        self.transient(app)
        self.grab_set()

        header = tk.Frame(self, bg=Palette.surface)
        header.pack(fill="x")
        row = tk.Frame(header, bg=Palette.surface)
        row.pack(fill="x", padx=16, pady=10)
        tk.Label(row, text="SETTINGS", bg=Palette.surface, fg=Palette.text,
                 font=app.fonts.h3).pack(side="left")
        tk.Label(row, text="· provider, model, reasoning", bg=Palette.surface,
                 fg=Palette.text_faint, font=app.fonts.tiny).pack(side="left", padx=(8, 0))
        ttk.Button(row, text="close", style="Ghost.TButton",
                   command=self.destroy).pack(side="right")

        self._scroll = tk.Frame(self, bg=Palette.bg)
        self._scroll.pack(fill="both", expand=True)
        self._canvas = tk.Canvas(self._scroll, bg=Palette.bg, highlightthickness=0)
        self._scrollbar = ttk.Scrollbar(self._scroll, orient="vertical", command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=self._scrollbar.set)
        self._scrollbar.pack(side="right", fill="y")
        self._canvas.pack(side="left", fill="both", expand=True)
        self._body = tk.Frame(self._canvas, bg=Palette.bg, padx=16, pady=12)
        self._body_window = self._canvas.create_window((0, 0), window=self._body, anchor="nw")
        self._body.bind("<Configure>", self._update_scroll_region)
        self._canvas.bind("<Configure>", self._resize_settings_body)

        self._build_connection()
        self._build_models()
        self._build_reasoning()
        self._build_permissions()
        self._build_providers()
        self._build_health()

        self._refresh()
        self._run_doctor()

    def _update_scroll_region(self, _event: Any = None) -> None:
        self._canvas.configure(scrollregion=self._canvas.bbox("all"))

    def _resize_settings_body(self, event: Any) -> None:
        self._canvas.itemconfigure(self._body_window, width=event.width)

    # ── sections ─────────────────────────────────────────────────────────
    def _section(self, title: str, subtitle: str = "") -> tk.Frame:
        card = Card(self._body, title=title, subtitle=subtitle, fonts=self.app.fonts)
        card.pack(fill="x", pady=(0, 10))
        return card.content

    def _build_connection(self) -> None:
        body = self._section("connection", "the live credential and endpoint")
        self._connection_label = tk.Label(body, text="", bg=Palette.panel,
                                          fg=Palette.text, font=self.app.fonts.small,
                                          anchor="w", justify="left", wraplength=680)
        self._connection_label.pack(fill="x")
        row = tk.Frame(body, bg=Palette.panel)
        row.pack(fill="x", pady=(8, 0))
        ttk.Button(row, text="edit credential…", style="Accent.TButton",
                   command=self._open_credentials).pack(side="left")
        ttk.Button(row, text="test connection", style="Ghost.TButton",
                   command=self._test).pack(side="left", padx=(8, 0))
        self._test_label = tk.Label(row, text="", bg=Palette.panel, fg=Palette.text_faint,
                                    font=self.app.fonts.tiny)
        self._test_label.pack(side="left", padx=(10, 0))

    def _build_models(self) -> None:
        body = self._section("model", "what the agent uses for planning and code")
        row = tk.Frame(body, bg=Palette.panel)
        row.pack(fill="x")
        self._model = ttk.Combobox(row, state="readonly", width=30,
                                   font=self.app.fonts.code_small)
        self._model.pack(side="left")
        ttk.Button(row, text="apply", style="Accent.TButton",
                   command=self._apply_model).pack(side="left", padx=(8, 0))
        ttk.Button(row, text="reload catalogue", style="Ghost.TButton",
                   command=self._reload).pack(side="left", padx=(8, 0))
        self._models_label = tk.Label(body, text="", bg=Palette.panel,
                                      fg=Palette.text_faint, font=self.app.fonts.tiny,
                                      anchor="w")
        self._models_label.pack(fill="x", pady=(6, 0))

    def _build_reasoning(self) -> None:
        body = self._section("reasoning", "how the plan and the review are produced")
        self._think = tk.BooleanVar(value=self.app.ctx.think_mode)
        ttk.Checkbutton(body, text="think mode — tree-search plan + constitutional review",
                        variable=self._think, style="Panel.TCheckbutton",
                        command=self._apply_think).pack(anchor="w")
        line = tk.Frame(body, bg=Palette.panel)
        line.pack(fill="x", pady=(8, 0))
        tk.Label(line, text="max output tokens", bg=Palette.panel, fg=Palette.text_muted,
                 font=self.app.fonts.tiny, width=18, anchor="w").pack(side="left")
        self._max_tokens = ttk.Entry(line, width=8, font=self.app.fonts.code)
        self._max_tokens.insert(0, "900")
        self._max_tokens.pack(side="left")
        ttk.Button(line, text="apply", style="Ghost.TButton",
                   command=self._apply_tokens).pack(side="left", padx=(8, 0))

    def _build_providers(self) -> None:
        body = self._section("providers", "any OpenAI-compatible endpoint")
        self._provider_view = CodeView(body, fonts=self.app.fonts, height=4, wrap=True)
        self._provider_view.pack(fill="x")
        form = tk.Frame(body, bg=Palette.panel)
        form.pack(fill="x", pady=(8, 0))
        self._fields: Dict[str, ttk.Entry] = {}
        for key, label, default in (
            ("base_url", "base url", "https://api.openai.com/v1"),
            ("api_key", "api key", ""),
            ("model", "model", "gpt-4o-mini"),
        ):
            line = tk.Frame(form, bg=Palette.panel)
            line.pack(fill="x", pady=1)
            tk.Label(line, text=label, bg=Palette.panel, fg=Palette.text_muted,
                     font=self.app.fonts.tiny, width=8, anchor="w").pack(side="left")
            entry = ttk.Entry(line, font=self.app.fonts.code_small)
            entry.insert(0, default)
            entry.pack(side="left", fill="x", expand=True)
            self._fields[key] = entry
        ttk.Button(form, text="register provider", style="Ghost.TButton",
                   command=self._add_provider).pack(anchor="w", pady=(6, 0))

    def _build_permissions(self) -> None:
        body = self._section("file changes", "how agent edits are applied")
        row = tk.Frame(body, bg=Palette.panel)
        row.pack(fill="x")
        self._approval_mode = ttk.Combobox(
            row, state="readonly", width=30, font=self.app.fonts.code_small,
            values=("Ask before applying", "Always allow"),
        )
        self._approval_mode.pack(side="left")
        self._approval_mode.bind("<<ComboboxSelected>>", lambda _event: self._apply_approval_mode())
        self._approval_detail = tk.Label(
            body, text="", bg=Palette.panel, fg=Palette.text_faint,
            font=self.app.fonts.tiny, anchor="w", justify="left", wraplength=680,
        )
        self._approval_detail.pack(fill="x", pady=(5, 0))

    def _build_health(self) -> None:
        body = self._section("health", "same battery as gigaforge doctor")
        self._doctor = CodeView(body, fonts=self.app.fonts, height=7, wrap=True)
        self._doctor.pack(fill="x")

    # ── data ─────────────────────────────────────────────────────────────
    def refresh(self) -> None:
        self._refresh()

    def _refresh(self) -> None:
        info = self.app.ctx.describe()
        offline = bool(info.get("offline"))
        models = list(info.get("models_live") or [])
        verify_tls = bool(getattr(self.app.ctx.runtime.settings, "verify_tls", False))
        self._connection_label.configure(
            text=(f"provider: {info.get('provider')}   ·   "
                  f"state: {'read-only (no credential)' if offline else 'runnable'}   ·   "
                  f"base url: {info.get('base_url') or 'default'}\n"
                  f"models served by this account: {len(models)}   ·   "
                  f"TLS verification: {'on' if verify_tls else 'off (API compatibility)'}"),
            fg=Palette.warn if offline else Palette.text,
        )
        mode = self.app.ctx.approval_mode
        self._approval_mode.set("Always allow" if mode == "always_allow" else "Ask before applying")
        self._approval_detail.configure(
            text=("Agent diffs are applied without a confirmation dialog."
                  if mode == "always_allow" else
                  "One confirmation covers the full proposed diff, including every listed file.")
        )
        self._model.configure(values=models)
        current = str((info.get("model_roles") or {}).get("strong") or "")
        if current in models:
            self._model.set(current)
        elif models:
            self._model.set(models[0])
        self._models_label.configure(
            text=", ".join(models[:6]) + (" …" if len(models) > 6 else "")
            if models else "no catalogue yet — reload once a credential is set"
        )
        lines = []
        for row in self.app.ctx.provider_rows():
            if row.get("error"):
                lines.append((f"x {row.get('name')}: {row['error']}", "err"))
            else:
                caps = row.get("capabilities") or {}
                flags = ", ".join(f"{k}={v}" for k, v in caps.items()
                                 if v not in (None, "", 0, False))
                lines.append((f"+ {row.get('name')}: {flags or 'ready'}", "ok"))
        self._provider_view.set_lines(lines or [("no providers", "muted")])

    def _run_doctor(self) -> None:
        def done(rows: Any) -> None:
            lines = []
            for row in rows or []:
                status = str(row.get("status"))
                tag = {"PASS": "ok", "WARN": "warn", "FAIL": "err"}.get(status, "text")
                lines.append((f"[{status}] {row.get('name')}: {row.get('detail')}", tag))
            self._doctor.set_lines(lines or [("no checks", "muted")])

        self.app.worker.submit("doctor", self.app.ctx.doctor_checks, on_done=done)

    # ── actions ──────────────────────────────────────────────────────────
    def _open_credentials(self) -> None:
        CredentialsDialog(self.app, on_saved=self._after_credentials)

    def _after_credentials(self, result: Dict[str, Any]) -> None:
        self._test_label.configure(
            text=f"credential applied · {len(result.get('models') or [])} models",
            fg=Palette.ok,
        )
        self._refresh()
        self.app.workspace.refresh_after_credentials()

    def _test(self) -> None:
        self._test_label.configure(text="testing…", fg=Palette.text_muted)

        def work() -> Any:
            client = self.app.ctx.runtime.client
            if client is None:
                raise RuntimeError("no credential configured")
            return client.list_models()

        def done(models: Any) -> None:
            self._test_label.configure(text=f"ok · {len(models or [])} models",
                                       fg=Palette.ok)
            self._refresh()

        def failed(exc: BaseException) -> None:
            self._test_label.configure(text=f"failed · {str(exc)[:80]}", fg=Palette.err)

        self.app.worker.submit("test", work, on_done=done, on_error=failed)

    def _apply_model(self) -> None:
        name = self._model.get()
        if not name:
            return
        self.app.ctx.set_model(name)
        self.app.workspace.set_model(name)
        self.app.toast(f"model → {name}", "ok")

    def _apply_approval_mode(self) -> None:
        mode = "always_allow" if self._approval_mode.get() == "Always allow" else "ask"
        try:
            self.app.ctx.set_approval_mode(mode)
            self.app.workspace.sync_approval()
        except Exception as exc:  # noqa: BLE001 - surface preference failures in the window
            self.app.toast(f"permission mode could not be saved: {exc}", "err")
            self._refresh()
            return
        self._approval_detail.configure(
            text=("Agent diffs are applied without a confirmation dialog."
                  if mode == "always_allow" else
                  "One confirmation covers the full proposed diff, including every listed file.")
        )
        label = "always allow" if mode == "always_allow" else "ask"
        self.app.toast(f"file change mode → {label}", "ok")

    def _apply_think(self) -> None:
        enabled = bool(self._think.get())
        self.app.ctx.set_think_mode(enabled)
        self.app.workspace.sync_think(enabled)
        self.app.toast("think mode " + ("on" if enabled else "off"), "ok")

    def _apply_tokens(self) -> None:
        try:
            value = max(64, int(self._max_tokens.get()))
        except ValueError:
            self.app.toast("numbers only", "warn")
            return
        self.app.workspace.set_max_tokens(value)
        self.app.toast(f"max output tokens → {value}", "ok")

    def _reload(self) -> None:
        def work() -> Any:
            from gigaforge.core.models import fetch_models

            client = self.app.ctx.runtime.client
            if client is None:
                raise RuntimeError("no credential configured")
            return fetch_models(client, path=Path(self.app.ctx.core_config.db_path).parent
                                / "models.json", force=True)

        def done(catalog: Any) -> None:
            from gigaforge.core.models import apply_roles

            runtime = self.app.ctx.runtime
            runtime.catalog = catalog
            cfg = apply_roles(runtime.core_config, catalog.roles)
            runtime.update_core_config(cfg)
            self.app.toast(f"catalogue: {len(catalog.models)} model(s)", "ok")
            self._refresh()

        def failed(exc: BaseException) -> None:
            self.app.toast(f"probe failed: {exc}", "err")

        self.app.worker.submit("models", work, on_done=done, on_error=failed)

    def _add_provider(self) -> None:
        base = self._fields["base_url"].get().strip()
        key = self._fields["api_key"].get().strip()
        model = self._fields["model"].get().strip()
        if not base or not key:
            self.app.toast("base url and api key are required", "warn")
            return
        try:
            from gigaforge.providers.openai_compat import OpenAICompatProvider
            from gigaforge.providers.registry import default_registry

            env = {"GIGAFORGE_OPENAI_BASE_URL": base,
                   "GIGAFORGE_OPENAI_API_KEY": key,
                   "GIGAFORGE_OPENAI_MODEL": model}
            os.environ.update(env)
            default_registry().register_instance("openai", OpenAICompatProvider())
            try:
                update_env_file(env)
            except OSError as exc:
                self.app.toast(f"provider active for this session, but .env could not be saved: {exc}",
                               "err")
                return
            self.app.toast("provider registered and saved to .env", "ok")
            self._refresh()
        except Exception as exc:  # noqa: BLE001
            self.app.toast(f"provider rejected: {exc}", "err")


class CredentialsDialog(tk.Toplevel):
    """Enter the key once, test it, keep working — no file editing, no restart."""

    def __init__(self, app: Any, *, on_saved: Any = None) -> None:
        super().__init__(app)
        self.app = app
        self.on_saved = on_saved
        self.title("Credential")
        self.configure(bg=Palette.bg)
        self.geometry("620x420")
        self.resizable(False, False)
        self.transient(app)
        self.grab_set()

        head = tk.Frame(self, bg=Palette.bg, padx=18, pady=14)
        head.pack(fill="x")
        tk.Label(head, text="GIGACHAT CREDENTIAL", bg=Palette.bg, fg=Palette.text_muted,
                 font=app.fonts.small_bold).pack(anchor="w")
        tk.Label(head, text="base64(client_id:client_secret) — the value Sber issues "
                            "for the OAuth endpoint. A leading “Basic ” is removed "
                            "automatically.",
                 bg=Palette.bg, fg=Palette.text_faint, font=app.fonts.tiny,
                 justify="left", wraplength=570).pack(anchor="w", pady=(2, 0))

        form = tk.Frame(self, bg=Palette.bg, padx=18)
        form.pack(fill="both", expand=True)
        self._vars: Dict[str, tk.StringVar] = {}
        self._entries: Dict[str, ttk.Entry] = {}
        rows = (
            ("auth_key", "credential", "", True),
            ("base_url", "base url", "https://gigachat.devices.sberbank.ru/api/v1", False),
            ("oauth_url", "oauth url", "https://ngw.devices.sberbank.ru:9443/api/v2/oauth", False),
            ("scope", "scope", "GIGACHAT_API_PERS", False),
        )
        for key, label, default, secret in rows:
            line = tk.Frame(form, bg=Palette.bg)
            line.pack(fill="x", pady=3)
            tk.Label(line, text=label, bg=Palette.bg, fg=Palette.text_muted,
                     font=app.fonts.tiny, width=10, anchor="w").pack(side="left")
            var = tk.StringVar(value=default)
            entry = ttk.Entry(line, textvariable=var, font=app.fonts.code_small,
                              show="•" if secret else "")
            entry.pack(side="left", fill="x", expand=True)
            self._vars[key] = var
            self._entries[key] = entry
            if secret:
                toggle = tk.Label(line, text="show", bg=Palette.bg, fg=Palette.accent,
                                  font=app.fonts.tiny, cursor="hand2")
                toggle.pack(side="left", padx=(6, 0))
                toggle.bind("<Button-1>", lambda _e, k=key: self._toggle(k))

        self._verify = tk.BooleanVar(value=False)
        ttk.Checkbutton(form, text="verify TLS certificates (may fail with the current API chain)",
                        variable=self._verify).pack(anchor="w", pady=(8, 0))

        self._status = tk.Label(form, text="", bg=Palette.bg, fg=Palette.text_faint,
                                font=app.fonts.small, anchor="w", justify="left",
                                wraplength=570)
        self._status.pack(fill="x", pady=(10, 0))

        actions = tk.Frame(self, bg=Palette.bg, padx=18, pady=12)
        actions.pack(fill="x")
        self._save = ttk.Button(actions, text="save & test", style="Accent.TButton",
                                command=self._run)
        self._save.pack(side="right")
        ttk.Button(actions, text="cancel", style="Ghost.TButton",
                   command=self.destroy).pack(side="right", padx=(0, 8))
        self._prefill()
        self.after(120, lambda: self._entries["auth_key"].focus_set())

    def _toggle(self, key: str) -> None:
        entry = self._entries[key]
        entry.configure(show="" if entry.cget("show") else "•")

    def _prefill(self) -> None:
        """Show the current shape of the key, never the key itself."""
        try:
            from gigaforge.config import get_settings
            from gigaforge.env import masked

            s = get_settings()
            self._vars["base_url"].set(s.base_url)
            self._vars["oauth_url"].set(s.auth_url)
            self._vars["scope"].set(s.scope)
            self._verify.set(bool(s.verify_tls))
            if s.auth_key:
                self._vars["auth_key"].set("")
                self._status.configure(text=f"a credential is already set {masked(s.auth_key)}"
                                            " — paste a new one to replace it")
        except Exception:  # noqa: BLE001
            pass

    def _run(self) -> None:
        key = self._vars["auth_key"].get().strip()
        if not key:
            self._status.configure(text="paste the credential first", fg=Palette.warn)
            return
        self._save.configure(state="disabled")
        self._status.configure(text="saving and testing the connection…",
                               fg=Palette.text_muted)

        current = self.app.ctx.runtime.settings
        base = self._vars["base_url"].get().strip() or str(current.base_url)
        oauth = self._vars["oauth_url"].get().strip() or str(current.auth_url)
        scope = self._vars["scope"].get().strip() or str(current.scope)
        verify = bool(self._verify.get())

        def work() -> Any:
            result = self.app.ctx.runtime.apply_credentials(
                auth_key=key, base_url=base, oauth_url=oauth, scope=scope,
                verify_tls=verify, probe=True,
            )
            try:
                update_env_file({
                    "GIGAFORGE_AUTH_KEY": os.environ.get("GIGAFORGE_AUTH_KEY", ""),
                    "GIGAFORGE_BASE_URL": base,
                    "GIGAFORGE_AUTH_URL": oauth,
                    "GIGAFORGE_SCOPE": scope,
                    "GIGAFORGE_TLS_VERIFY_OFF": "0" if verify else "1",
                })
            except OSError as exc:
                result["save_error"] = f"{type(exc).__name__}: {exc}"
            return result

        def done(result: Any) -> None:
            self._save.configure(state="normal")
            models = list((result or {}).get("models") or [])
            warning = str((result or {}).get("warning") or "")
            save_error = str((result or {}).get("save_error") or "")
            if save_error:
                self._status.configure(
                    text=f"active for this session, but .env could not be saved: {save_error[:180]}",
                    fg=Palette.err,
                )
            elif models:
                self._status.configure(
                    text=f"connected · {len(models)} models available · saved to .env",
                    fg=Palette.ok)
            elif warning:
                self._status.configure(text=f"saved, but the probe failed: {warning[:200]}",
                                       fg=Palette.warn)
            else:
                self._status.configure(text="saved · no model catalogue returned",
                                       fg=Palette.warn)
            if self.on_saved is not None:
                self.on_saved(result or {})
            if models and not save_error:
                self.after(900, self.destroy)

        def failed(exc: BaseException) -> None:
            self._save.configure(state="normal")
            self._status.configure(text=f"rejected: {str(exc)[:240]}", fg=Palette.err)

        self.app.worker.submit("credential", work, on_done=done, on_error=failed)
