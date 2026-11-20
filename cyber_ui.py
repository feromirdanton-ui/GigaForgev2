"""GigaForge Cyber UI — colored animated banner and menu helpers.

Pure stdlib — no rich, no typer — so it works on a stock Python install and
also from ``start.bat`` before the deps are pulled in. The visuals lean on
ANSI escape sequences; on Windows 10+ they are enabled by default, on older
hosts ``start.bat`` does the ``<EnableANSI.cmd>`` equivalent via
``set TERM=xterm-256color``.

The launcher prints a banner, then a menu of modes that mirrors the bat file
1:1. Selecting a mode spawns the right ``gigaforge`` subcommand.

Run directly::

    python cyber_ui.py banner          # print the banner and exit
    python cyber_ui.py menu            # interactive menu
    python cyber_ui.py <mode>          # launch <mode> immediately
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Sequence


def _force_utf8() -> None:
    """Make stdout/stderr UTF-8 even when the console codepage is cp1251.

    Falls back silently if reconfiguration isn't supported (PyInstaller, IDLE…).
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:  # pragma: no cover - legacy Python
            pass


_force_utf8()

# ── ANSI palette (cyber / neon) ──────────────────────────────────────────────
ESC = "\x1b["
RESET = ESC + "0m"
BOLD = ESC + "1m"
DIM = ESC + "2m"
CYAN = ESC + "96m"
NEON_BLUE = ESC + "94m"
NEON_GREEN = ESC + "92m"
NEON_PINK = ESC + "95m"
NEON_YELLOW = ESC + "93m"
GREY = ESC + "90m"
RED = ESC + "91m"


def _enable_ansi() -> None:
    """Best-effort: enable ANSI on legacy Windows consoles."""
    if os.name != "nt":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        # ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x4
        kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 0x07 | 0x04)
    except Exception:  # pragma: no cover - best-effort
        pass


def _tty() -> bool:
    return sys.stdout.isatty()


def color(text: str, *codes: str) -> str:
    if not _tty():
        return text
    return "".join(codes) + text + RESET


# ── Banner ───────────────────────────────────────────────────────────────────
BANNER = r"""
   ____ _ _    ____                         _
  / ___(_) |_ / ___|_   _ _ __ ___  _ __ ___ (_)_______  _ __
 | |  _| | __| |  _| | | | '_ ` _ \| '_ ` _ \| |_  / _ \| '__|
 | |_| | | |_| |_| | |_| | | | | | | | | | | | |/ /  __/| |
  \____|_|\__|\____|\__,_|_| |_| |_|_| |_| |_|_/___\___||_|
                                          v{version}  ·  GigaChat harness
"""


def banner(version: str = "2.0.0", *, animate: bool = True) -> None:
    _enable_ansi()
    lines = BANNER.format(version=version).rstrip("\n").split("\n")
    palette = (NEON_BLUE, CYAN, NEON_GREEN, NEON_PINK, NEON_YELLOW)
    for i, line in enumerate(lines):
        hue = palette[i % len(palette)]
        if animate and _tty():
            sys.stdout.write(color(line, BOLD, hue))
            sys.stdout.flush()
            time.sleep(0.04)
            sys.stdout.write("\n")
        else:
            sys.stdout.write(color(line, BOLD, hue) + "\n")
    sys.stdout.write("\n")
    sys.stdout.flush()


# ── Frame / progress ─────────────────────────────────────────────────────────
def frame(label: str, *, ok: bool | None = None) -> None:
    """Print a status line with a coloured left frame.

    ok=True  -> green check, ok=False -> red cross, None -> cyan arrow.
    """
    _enable_ansi()
    glyph_map = {True: ("✓", NEON_GREEN), False: ("✗", RED), None: ("›", CYAN)}
    glyph, hue = glyph_map[ok if ok is not None else None]
    line = f"  {color(glyph, BOLD, hue)}  {label}"
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def hr(char: str = "─", width: int = 64) -> None:
    sys.stdout.write(color(char * width, DIM, GREY) + "\n")


# ── Modes / menu ────────────────────────────────────────────────────────────
MODES: list[tuple[str, str, str]] = [
    # key,        title,                              subtitle
    ("web",       "🌐  WEB CONTROL ROOM",              "Live HTML dashboard on http://127.0.0.1:8787"),
    ("tui",       "📺  TERMINAL DASHBOARD",            "Rich live KPI / steps / diff — refresh with --watch"),
    ("chat",      "💬  AGENTIC CHAT",                  "Talk to the harness: type a goal or a /command"),
    ("run",       "⚙   RUN PIPELINE (ONE-SHOT)",       "Execute a single task end-to-end"),
    ("panel",     "📄  RENDER HTML PANEL",             "Write a self-contained gigaforge-panel.html"),
    ("smoke",     "🩺  LIVE SMOKE TEST",               "OAuth + models + 1 chat round-trip (needs creds)"),
    ("doctor",    "🩹  DOCTOR (ENV CHECK)",            "Diagnose credentials / deps / paths"),
    ("seed",      "🌱  SEED DEMO TELEMETRY",           "Populate .gigaforge/ with synthetic run data"),
    ("bench",     "📊  OFFLINE BENCHMARK",             "Run the bench suite and print the report"),
    ("models",    "🧠  LIST GIGACHAT MODELS",          "Live API call — needs GIGAFORGE_AUTH_KEY"),
    ("env",       "🔐  EDIT .env",                     "Open .env in the default editor"),
    ("paths",     "📁  SHOW TELEMETRY PATHS",          "DB / trace / L0 / chat locations"),
    ("install",   "📦  INSTALL / UPGRADE DEPS",        "pip install -e .[dev]"),
    ("quit",      "⏏   EXIT",                          "Close the launcher"),
]


def render_menu() -> None:
    hr("═")
    sys.stdout.write(color("  GIGAFORGE · COMMAND DECK", BOLD, CYAN) + "\n")
    sys.stdout.write(color("  Select a module to engage:", DIM) + "\n")
    hr("─")
    width = max(len(k) for k, *_ in MODES) + 2
    for i, (key, title, subtitle) in enumerate(MODES, start=1):
        n = color(f"  [{i:>2}]", BOLD, NEON_BLUE)
        k = color(key.ljust(width), BOLD, CYAN)
        t = color(title, BOLD)
        s = color(subtitle, DIM, GREY)
        sys.stdout.write(f"{n}  {k}  {t}   {s}\n")
    hr("═")


# ── Mode handlers ───────────────────────────────────────────────────────────
def _find_gigaforge() -> list[str]:
    """Return the argv prefix that runs ``gigaforge`` reliably.

    Prefer the on-PATH entry point; fall back to the most likely Windows
    Scripts location; finally fall back to ``python -m gigaforge``.
    """
    exe = shutil.which("gigaforge")
    if exe:
        return [exe]
    scripts_guesses = []
    pyexe = Path(sys.executable)
    scripts_guesses.append(pyexe.parent / "Scripts" / "gigaforge.exe")
    scripts_guesses.append(Path.home() / "AppData" / "Roaming" / "Python" / f"Python{sys.version_info.major}{sys.version_info.minor}" / "Scripts" / "gigaforge.exe")
    for cand in scripts_guesses:
        if cand.exists():
            return [str(cand)]
    return [sys.executable, "-m", "gigaforge.cli"]


def _spawn(args: Sequence[str], *, cwd: Path | None = None) -> int:
    return subprocess.call(list(args), cwd=str(cwd) if cwd else None)


def launch(mode: str, *, root: Path | None = None) -> int:
    """Dispatch a single mode and return its exit code."""
    root = root or Path.cwd()
    gf = _find_gigaforge()

    if mode == "web":
        frame("Starting GigaForge web control room on :8787 — Ctrl-C to stop")
        return _spawn([*gf, "web", "--serve", "--port", "8787", "--open"], cwd=root)
    if mode == "tui":
        frame("Opening live terminal dashboard (Ctrl-C to exit)")
        return _spawn([*gf, "ui", "--watch", "--interval", "1"], cwd=root)
    if mode == "chat":
        frame("Starting agentic chat — type /help for commands, Ctrl-C to exit")
        return _spawn([*gf, "chat"], cwd=root)
    if mode == "run":
        goal = _prompt("Goal for the harness (one line)")
        if not goal:
            frame("aborted: empty goal", ok=False)
            return 2
        return _spawn([*gf, "run", goal, "--dashboard"], cwd=root)
    if mode == "panel":
        out = root / "gigaforge-panel.html"
        frame(f"Rendering panel -> {out}")
        rc = _spawn([*gf, "web", "--out", str(out)], cwd=root)
        if rc == 0:
            frame(f"open it: file:///{out}".replace("\\", "/"), ok=True)
        return rc
    if mode == "smoke":
        return _spawn([*gf, "smoke", "--verbose"], cwd=root)
    if mode == "doctor":
        return _spawn([*gf, "doctor", "--verbose"], cwd=root)
    if mode == "seed":
        from examples.smoke_ui import main as _smoke_main

        sys.path.insert(0, str(root))
        return _smoke_main()
    if mode == "bench":
        return _spawn([*gf, "bench"], cwd=root)
    if mode == "models":
        return _spawn([*gf, "models", "--verbose"], cwd=root)
    if mode == "env":
        return _edit_env(root)
    if mode == "paths":
        return _spawn([*gf, "paths"], cwd=root)
    if mode == "install":
        return _spawn([sys.executable, "-m", "pip", "install", "-e", ".[dev]"], cwd=root)
    if mode in {"quit", "exit", "q"}:
        frame("bye.", ok=True)
        return 0
    frame(f"unknown mode: {mode!r}", ok=False)
    return 2


def _prompt(label: str) -> str:
    sys.stdout.write(color(f"  {label}: ", BOLD, CYAN))
    sys.stdout.flush()
    try:
        return input().strip()
    except EOFError:
        return ""


def _edit_env(root: Path) -> int:
    env = root / ".env"
    example = root / "env.example"
    if not env.exists() and example.exists():
        frame(f".env missing — bootstrapping from env.example to {env}")
        env.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
    if not env.exists():
        frame("no .env and no env.example found", ok=False)
        return 2
    editor = os.environ.get("EDITOR") or ("notepad" if os.name == "nt" else "vi")
    frame(f"opening {env} in {editor}")
    return _spawn([editor, str(env)], cwd=root)


# ── Interactive REPL ─────────────────────────────────────────────────────────
KEY_TO_MODE = {key: key for key, *_ in MODES}
INDEX_TO_MODE = {i + 1: key for i, (key, *_) in enumerate(MODES)}


def repl(root: Path | None = None) -> int:
    root = root or Path.cwd()
    while True:
        banner(animate=_tty())
        render_menu()
        choice = _prompt("pick")
        if not choice:
            continue
        if choice in {"quit", "exit", "q", "0"}:
            return 0
        if choice.isdigit():
            mode = INDEX_TO_MODE.get(int(choice))
        else:
            mode = KEY_TO_MODE.get(choice.lower())
        if not mode:
            frame(f"unknown selection: {choice!r}", ok=False)
            continue
        if mode == "quit":
            return 0
        try:
            rc = launch(mode, root=root)
        except KeyboardInterrupt:
            frame("interrupted", ok=False)
            continue
        if rc not in (0, None):
            frame(f"mode {mode} exited with code {rc}", ok=False)
        if mode in {"web", "tui", "chat"}:
            return int(rc or 0)


# ── CLI entry point ─────────────────────────────────────────────────────────
def main(argv: Sequence[str] | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    if not argv or argv[0] in {"-h", "--help"}:
        print(__doc__)
        return 0
    cmd = argv[0]
    if cmd == "banner":
        banner(animate=("--no-animate" not in argv))
        return 0
    if cmd == "menu":
        banner()
        render_menu()
        return 0
    if cmd == "list":
        for key, title, _ in MODES:
            print(f"{key}\t{title}")
        return 0
    root = Path.cwd()
    if cmd == "repl":
        return repl(root)
    return launch(cmd, root=root)


if __name__ == "__main__":
    raise SystemExit(main())
