"""GigaForge advanced TUI — a multi-pane interactive shell over the harness.

``gigaforge ui`` launches a textual application with:

- a **dashboard** pane: budget remaining, recent metrics, repo map summary.
- a **chat** pane: conversational task entry (multi-turn; session-persistent).
- a **notes** pane: free-form scratchpad saved to `.gigaforge/notes.md`.
- a **pipeline** pane: live view of the current run's steps + diff + tests.
- a **command palette** (Ctrl+P): invoke ANY cli command (run/chat/report/
  models/index/mcp/bench/smoke/doctor) with args; output streams into a log pane.
- **session state**: the chat history + notes persist across runs in
  `.gigaforge/session.json` so a developer can resume a multi-step coding
  session.

Designed for **session-based, multimodal** development — not just one-shot
fixes. The developer can: draft a task in chat, jot findings in notes, run
the pipeline, inspect the diff, iterate, and keep a running log — all in one
terminal, no context switching.

The TUI is a THIN SHELL over the existing CLI/runtime: it calls the same
``Pipeline.run`` / cli commands the user would type, capturing rich output.
No business logic is duplicated.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.reactive import reactive
from textual.widgets import (
    Footer,
    Header,
    Input,
    RichLog,
    Static,
    TabbedContent,
    TabPane,
    TextArea,
)

from gigaforge.config import get_settings

__all__ = ["GigaForgeTUI", "run_tui"]


# ---------------------------------------------------------------------------
# session persistence — chat history + notes survive across runs
# ---------------------------------------------------------------------------


def _session_file() -> Path:
    s = get_settings()
    d = s.db_path.parent / "session.json"
    d.parent.mkdir(parents=True, exist_ok=True)
    return d


def _load_session() -> dict[str, Any]:
    f = _session_file()
    if f.exists():
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {"chat": [], "notes": ""}
    return {"chat": [], "notes": ""}


def _save_session(data: dict[str, Any]) -> None:
    _session_file().write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# status banner widget
# ---------------------------------------------------------------------------


class StatusBanner(Static):
    """A compact one-line status: provider · model · budget · repo."""

    def render(self) -> str:
        try:
            s = get_settings()
            provider = "giga"  # default; the registry could override
            repo = s.sandbox_cwd.name or "(no repo)"
            return (
                f"[bold cyan]GigaForge[/] · provider=[yellow]{provider}[/] · "
                f"base=[blue]{s.base_url}[/] · repo=[green]{repo}[/]"
            )
        except Exception as exc:  # noqa: BLE001
            return f"[red]config error:[/] {exc}"


# ---------------------------------------------------------------------------
# the TUI app
# ---------------------------------------------------------------------------


class GigaForgeTUI(App):
    """GigaForge advanced interactive shell."""

    CSS = """
    Screen {
        layout: vertical;
    }
    #main-tabs {
        height: 1fr;
    }
    #command-bar {
        height: 3;
        dock: bottom;
        border-top: solid cyan;
    }
    #status {
        height: 1;
        dock: top;
        background: $surface;
        color: $text;
    }
    .pane {
        padding: 0 1;
    }
    """

    BINDINGS = [
        Binding("ctrl+p", "command_palette", "Command palette"),
        Binding("ctrl+n", "focus('command-input')", "Focus command"),
        Binding("ctrl+t", "focus('tabs')", "Focus tabs"),
        Binding("ctrl+s", "save_session", "Save session"),
        Binding("ctrl+q", "quit", "Quit"),
        Binding("f1", "toggle_help", "Help"),
    ]

    TITLE = "GigaForge"
    SUB_TITLE = "context is budget · diff is contract · cache is memory"

    chat_log: reactive[list[str]] = reactive(list)
    notes_text: reactive[str] = reactive("")

    def __init__(self, repo_root: Path | None = None) -> None:
        super().__init__()
        self.repo_root = repo_root or Path.cwd()
        self._session = _load_session()
        self.chat_log = self._session.get("chat", [])
        self.notes_text = self._session.get("notes", "")

    # --- layout -------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header()
        yield StatusBanner(id="status")
        with TabbedContent(id="tabs"):
            with TabPane("💬 Chat", id="chat-tab"):
                yield VerticalScroll(
                    Static(self._render_chat(), id="chat-display"),
                    id="chat-scroll",
                )
                yield Input(placeholder="Task or message (Enter to send)…", id="chat-input")
            with TabPane("📝 Notes", id="notes-tab"):
                yield TextArea.code_editor(self.notes_text, id="notes-area")
            with TabPane("📊 Dashboard", id="dash-tab"):
                yield VerticalScroll(Static(self._render_dashboard(), id="dash-display"))
            with TabPane("🔧 Pipeline", id="pipe-tab"):
                yield RichLog(id="pipe-log", highlight=True, markup=True)
            with TabPane("📜 Log", id="log-tab"):
                yield RichLog(id="cmd-log", highlight=True, markup=True)
        yield Input(placeholder="Command palette: e.g.  run --repo . --function x --task '...'   (Ctrl+P to focus)", id="command-input")
        yield Footer()

    def on_mount(self) -> None:
        self._refresh_dashboard()

    # --- renderers ----------------------------------------------------------

    def _render_chat(self) -> str:
        if not self.chat_log:
            return "[dim]No messages yet. Type a task below to start a coding session.\n\nTip: use the Command palette (Ctrl+P) to run `chat --auto --task '...'`[/]"
        return "\n\n".join(self.chat_log[-50:])

    def _render_dashboard(self) -> str:
        try:
            from gigaforge.core.metrics import MetricsCollector

            s = get_settings()
            coll = MetricsCollector(settings=s)
            agg = coll.aggregate()
            lines = [
                "# GigaForge Dashboard\n",
                f"- **Provider:** giga (default) · base `{s.base_url}`",
                f"- **Repo:** `{self.repo_root}`",
                f"- **Budget caps:** plan {s.budget_plan_in}/{s.budget_plan_out} · "
                f"code {s.budget_code_in}/{s.budget_code_out} · "
                f"fixloop {s.budget_fixloop_iters}× · task-cap {s.budget_task_cap}",
                f"- **Models:** L1={s.models['plan']} · L2={s.models['code']} · "
                f"L0=tree-sitter",
                "\n## Session metrics\n",
                f"- Total LLM calls: **{agg.get('total_calls', 0)}**",
                f"- Tokens in/out: {agg.get('total_tokens_in', 0)}/{agg.get('total_tokens_out', 0)}",
                f"- Cache hits: {agg.get('cache_hits', 0)} · "
                f"Reject rate: {agg.get('reject_rate', 0):.1%}",
            ]
            by_phase = agg.get("by_phase", {})
            if by_phase:
                lines.append("\n## By phase\n")
                for ph, d in by_phase.items():
                    lines.append(
                        f"- `{ph}`: {d['calls']} calls · "
                        f"{d['tokens_in']+d['tokens_out']} tokens · "
                        f"{d.get('avg_latency_ms', 0)}ms avg"
                    )
            return "\n".join(lines)
        except Exception as exc:  # noqa: BLE001
            return f"[red]dashboard error:[/] {exc}"

    def _refresh_dashboard(self) -> None:
        try:
            self.query_one("#dash-display", Static).update(self._render_dashboard())
            self.query_one("#status", StatusBanner).refresh()
        except Exception:  # noqa: BLE001
            pass

    # --- chat ---------------------------------------------------------------

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "chat-input":
            text = event.value.strip()
            if not text:
                return
            self.chat_log.append(f"[bold cyan]you>[/] {text}")
            event.input.value = ""
            self._render_chat_refresh()
            # Echo a hint: how to run this as a task.
            self.chat_log.append(
                f"[dim]↳ to execute, open the command palette (Ctrl+P) and run:[/]\n"
                f"[green]chat --repo {self.repo_root} --auto --task \"{text[:80]}\" --yes[/]"
            )
            self._render_chat_refresh()
            self._persist()
        elif event.input.id == "command-input":
            self._run_cli_command(event.value.strip())
            event.input.value = ""

    def _render_chat_refresh(self) -> None:
        try:
            self.query_one("#chat-display", Static).update(self._render_chat())
        except Exception:  # noqa: BLE001
            pass

    # --- notes --------------------------------------------------------------

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        if event.text_area.id == "notes-area":
            self.notes_text = event.text_area.text
            self._persist()

    # --- persistence --------------------------------------------------------

    def _persist(self) -> None:
        self._session["chat"] = self.chat_log
        self._session["notes"] = self.notes_text
        _save_session(self._session)

    def action_save_session(self) -> None:
        self._persist()
        self._log(f"[green]✓[/] session saved to {_session_file()}")

    # --- command palette ----------------------------------------------------

    def action_command_palette(self) -> None:
        self.query_one("#command-input", Input).focus()

    def action_toggle_help(self) -> None:
        self._log(
            "[bold]GigaForge TUI — keys[/]\n"
            "  Ctrl+P  command palette (run any cli command)\n"
            "  Ctrl+N  focus command input\n"
            "  Ctrl+T  focus tabs\n"
            "  Ctrl+S  save session (chat + notes → .gigaforge/session.json)\n"
            "  Ctrl+Q  quit\n"
            "  F1      this help\n\n"
            "[dim]Commands: run, chat, report, models, index, mcp, bench, smoke, doctor[/]"
        )

    # --- cli invocation -----------------------------------------------------

    def _log(self, msg: str) -> None:
        try:
            log = self.query_one("#cmd-log", RichLog)
            log.write(msg)
        except Exception:  # noqa: BLE001 — UI not ready
            pass

    def _run_cli_command(self, cmdline: str) -> None:
        """Invoke a gigaforge subcommand as a subprocess, streaming output."""
        if not cmdline:
            return
        # Built-in pseudo-commands handled in-process:
        if cmdline in ("clear", "cls"):
            try:
                self.query_one("#cmd-log", RichLog).clear()
            except Exception:  # noqa: BLE001
                pass
            return
        if cmdline == "help":
            self.action_toggle_help()
            return
        # Route to the installed `gigaforge` console script OR `python -m gigaforge.cli`.
        # Prefer the module form (works in dev without install).
        argv = ["python3", "-m", "gigaforge.cli"] + cmdline.split()
        self._log(f"[bold cyan]$ gigaforge {cmdline}[/]")
        self._stream_subprocess(argv)

    @work(exclusive=True, name="cli-stream")
    async def _stream_subprocess(self, argv: list[str]) -> None:
        """Run argv, stream stdout+stderr into the log pane."""
        import os

        env = dict(os.environ)
        # Ensure the auth key is present (CRIT-01 requirement).
        if "GIGAFORGE_AUTH_KEY" not in env:
            self._log("[yellow]warning:[/] GIGAFORGE_AUTH_KEY not set — doctor may flag this.")
        log_widget: RichLog
        try:
            log_widget = self.query_one("#cmd-log", RichLog)
        except Exception:  # noqa: BLE001
            return
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
                cwd=str(self.repo_root),
            )
        except FileNotFoundError as exc:
            log_widget.write(f"[red]spawn failed:[/] {exc}")
            return
        assert proc.stdout is not None
        async for raw in proc.stdout:
            try:
                line = raw.decode("utf-8", errors="replace").rstrip()
            except Exception:  # noqa: BLE001
                continue
            if line:
                log_widget.write(line)
        rc = await proc.wait()
        log_widget.write(f"[dim]── exit {rc} ──[/]")
        # Refresh dashboard after metrics-affecting commands.
        if any(k in " ".join(argv) for k in ("run", "chat", "bench")):
            self._refresh_dashboard()

    # --- key handlers -------------------------------------------------------

    def action_focus_command(self) -> None:
        self.query_one("#command-input", Input).focus()


def run_tui(repo_root: Path | None = None) -> int:
    """Entry point for `gigaforge ui`. Returns an exit code."""
    app = GigaForgeTUI(repo_root=repo_root or Path.cwd())
    app.run()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(run_tui())
