"""Собери 原型/index.html из каркаса и фрагментов экранов.

Каркас: 原型/_shell.html (маркеры /*BUILD:CSS*/, /*BUILD:SCREENCSS*/, <!--BUILD:SCREENS-->, <!--BUILD:CTX-->).
Стили: 原型/live.css — единственный источник токенов и общих классов.
Экраны: 原型/screens/*.html — каждый содержит <style>, <section class="screen"> и <template class="ctx-set">.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]
PROTO = ROOT / "原型"
SHELL = PROTO / "_shell.html"
CSS = PROTO / "live.css"
SCREENS = PROTO / "screens"
OUT = PROTO / "index.html"

ORDER = [
    "overview",
    "forge",
    "explore",
    "orchestrate",
    "files",
    "agents",
    "monitoring",
    "settings",
]


def split_fragment(text: str) -> tuple[str, str, str]:
    styles = "\n".join(re.findall(r"<style>(.*?)</style>", text, flags=re.S))
    body = re.sub(r"<style>.*?</style>", "", text, flags=re.S)
    ctx = re.search(r'<template class="ctx-set".*?</template>', body, flags=re.S)
    ctx_html = ctx.group(0) if ctx else ""
    body = body.replace(ctx_html, "")
    return styles.strip(), body.strip(), ctx_html.strip()


def main() -> int:
    if not SHELL.is_file():
        print(f"нет каркаса: {SHELL}")
        return 1
    shell = SHELL.read_text(encoding="utf-8")
    css = CSS.read_text(encoding="utf-8")

    styles: list[str] = []
    markup: list[str] = []
    ctxs: list[str] = []
    missing: list[str] = []

    for name in ORDER:
        path = SCREENS / f"{name}.html"
        if not path.is_file():
            missing.append(name)
            markup.append(
                f'<section class="screen" id="s-{name}"><div class="page"><div class="empty">'
                f"Экран «{name}» собирается…</div></div></section>"
            )
            continue
        style, body, ctx = split_fragment(path.read_text(encoding="utf-8"))
        if style:
            styles.append(f"/* ── {name} ───────────────────────────── */\n{style}")
        markup.append(body)
        if ctx:
            ctxs.append(ctx)

    html = (
        shell.replace("/*BUILD:CSS*/", css)
        .replace("/*BUILD:SCREENCSS*/", "\n".join(styles))
        .replace("<!--BUILD:SCREENS-->", "\n".join(markup))
        .replace("<!--BUILD:CTX-->", "\n".join(ctxs))
    )
    if "BUILD:" in html:
        print("остались незаполненные маркеры:", re.findall(r"BUILD:[A-Z]+", html))
        return 1

    OUT.write_text(html, encoding="utf-8")
    print(f"собрано: {OUT} · {len(html) / 1024:.1f} КБ · экранов: {len(markup)}")
    if missing:
        print("нет фрагментов:", ", ".join(missing))
    return 0


if __name__ == "__main__":
    sys.exit(main())
