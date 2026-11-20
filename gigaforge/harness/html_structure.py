"""Structural audit and deterministic repair of generated HTML.

**Why this module exists.**  A live goal run asked for a cyberpunk landing page
and produced a file that passed every check the harness had: cyan and magenta in
real CSS declarations, a hero, a glitch keyframe, a grid, a footer, self
contained, 5495 bytes.  Rendered in a browser it was broken:

* the entire stylesheet appeared **twice** — once inside ``<style>`` and once as
  raw text in ``<body>``, so the page opened with 2000 characters of CSS
  printed as prose;
* a stray ``</style>`` sat in the body;
* the feature grid and the footer were each emitted **twice**, so the page had
  six cards where the brief asked for three.

The verifier counted ``<html>`` roots, ``<body>`` elements, duplicate ``id``
values and duplicate ``<script src>``.  None of those fire on this page: there
is one root, one body, no ids, no scripts.  The defect is *text where markup
belongs* and *the same block twice*, and nothing was looking for either.

The harness's job is the part with a deterministically correct answer.  "Is
this stylesheet sitting in the body as text?" is a string question.  "Is this
section emitted twice?" is a comparison.  Neither needs a model, and asking a
model to notice it is how the page shipped broken.

Two functions, deliberately separate:

* :func:`audit_html_structure` — reports what is wrong, for the verifier;
* :func:`repair_html_structure` — fixes what has exactly one correct fix, for
  the pipeline, so a repairable page is repaired instead of costing a cycle.

The repair is conservative by construction: it only moves CSS that is provably
CSS, only deletes blocks that are byte-identical after whitespace normalisation,
and returns the list of what it did so the run can report it rather than
silently rewriting the model's work.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

#: Elements whose content is code, not prose.  A ``{`` inside one of these is
#: not a leaked stylesheet, and rewriting it would corrupt the page.
_OPAQUE_TAGS = ("script", "style", "pre", "code", "textarea", "svg")

#: Block-level elements that a generator can plausibly emit twice by accident.
_BLOCK_TAGS = ("section", "footer", "header", "article", "aside", "nav", "main")

_TAG_RE = re.compile(r"<[^>]+>")
_STYLE_BLOCK_RE = re.compile(r"<style\b[^>]*>(.*?)</style\s*>", re.S | re.I)
_STRAY_STYLE_TAG_RE = re.compile(r"</?style\b[^>]*>", re.I)
_BODY_RE = re.compile(r"<body\b[^>]*>(.*?)</body\s*>", re.S | re.I)
_HEAD_RE = re.compile(r"<head\b[^>]*>(.*?)</head\s*>", re.S | re.I)
_TEXT_NODE_RE = re.compile(r">([^<>]+)<", re.S)


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def looks_like_css(text: str) -> bool:
    """True when *text* is a stylesheet that ended up outside ``<style>``.

    The test is deliberately strict, because the cost of a false positive is
    deleting a paragraph of the user's copy.  A stylesheet has braces, at least
    two declarations, and a declaration is ``property: value;``.  Prose that
    happens to contain a brace does not.
    """
    body = (text or "").strip()
    # 30, not 40: a minimal but real rule set — ``body { color: #fff;
    # background: #000; }`` — is 39 characters, and a threshold above it
    # rejected the smallest genuine stylesheet.
    if len(body) < 30:
        return False
    if "{" not in body or "}" not in body:
        return False
    declarations = re.findall(r"[a-zA-Z-]+\s*:\s*[^;{}]{1,120};", body)
    if len(declarations) < 2:
        return False
    # A stylesheet is mostly punctuation and property names; prose is mostly
    # words.  Requiring a high punctuation density keeps a sentence that
    # mentions "color: red;" from being mistaken for a stylesheet.
    punctuation = sum(body.count(ch) for ch in "{};:")
    return punctuation / max(1, len(body)) > 0.02


def _strip_opaque(html: str) -> str:
    """Blank out script/style/pre/code bodies so their braces are not counted."""
    out = html
    for tag in _OPAQUE_TAGS:
        out = re.sub(
            rf"<{tag}\b[^>]*>.*?</{tag}\s*>", " ", out, flags=re.S | re.I
        )
    return out


def visible_text(html: str) -> str:
    """The text a reader sees: no tags, no script, no style."""
    return _normalise(_TAG_RE.sub(" ", _strip_opaque(html)))


def _text_nodes(html: str) -> list[tuple[int, int, str]]:
    """Every run of text between tags, as ``(start, end, text)`` offsets.

    The first version of this used ``>([^<>]+)<``, which only matches text that
    has a tag on *both* sides.  The live page put its leaked stylesheet at the
    very start of ``<body>``, before any tag, so the regex never saw it and the
    audit reported the page as clean while a browser printed 2000 characters of
    CSS as prose.  A text node is bounded by a tag or by the end of the string;
    this walks the tags and takes what lies between them.
    """
    nodes: list[tuple[int, int, str]] = []
    cursor = 0
    for match in _TAG_RE.finditer(html):
        if match.start() > cursor:
            nodes.append((cursor, match.start(), html[cursor:match.start()]))
        cursor = match.end()
    if cursor < len(html):
        nodes.append((cursor, len(html), html[cursor:]))
    return nodes


def leaked_css_text(html: str) -> list[str]:
    """CSS runs that are sitting in the body as text instead of in ``<style>``."""
    body_match = _BODY_RE.search(html)
    body = body_match.group(1) if body_match else html
    body = _STRAY_STYLE_TAG_RE.sub(" ", body)
    leaked: list[str] = []
    for _start, _end, candidate in _text_nodes(body):
        if looks_like_css(candidate):
            leaked.append(candidate.strip())
    return leaked


def _block_spans(html: str, tag: str) -> list[tuple[int, int, str]]:
    """Spans of every ``<tag>...</tag>`` block, honouring nesting.

    A regex cannot match nested blocks, and a generated page that repeats a
    ``<section>`` may well nest one.  This walks the tags and keeps a depth
    counter, so the span returned is the whole outer block.
    """
    open_re = re.compile(rf"<{tag}\b[^>]*>", re.I)
    close_re = re.compile(rf"</{tag}\s*>", re.I)
    spans: list[tuple[int, int, str]] = []
    position = 0
    while True:
        start_match = open_re.search(html, position)
        if not start_match:
            break
        depth = 0
        cursor = start_match.start()
        end = -1
        while cursor < len(html):
            next_open = open_re.search(html, cursor)
            next_close = close_re.search(html, cursor)
            if not next_close:
                break
            if next_open and next_open.start() < next_close.start():
                depth += 1
                cursor = next_open.end()
                continue
            depth -= 1
            cursor = next_close.end()
            if depth == 0:
                end = next_close.end()
                break
        if end < 0:
            break
        spans.append((start_match.start(), end, html[start_match.start():end]))
        position = end
    return spans


def duplicate_blocks(html: str) -> list[tuple[str, int]]:
    """``(tag, count)`` for block elements emitted more than once identically."""
    found: list[tuple[str, int]] = []
    for tag in _BLOCK_TAGS:
        seen: dict[str, int] = {}
        for _start, _end, block in _block_spans(html, tag):
            key = _normalise(_TAG_RE.sub(" ", block))
            if len(key) < 20:
                continue
            seen[key] = seen.get(key, 0) + 1
        worst = max(seen.values(), default=0)
        if worst > 1:
            found.append((tag, worst))
    return found


def duplicate_style_blocks(html: str) -> int:
    """How many ``<style>`` blocks repeat content already present."""
    seen: set[str] = set()
    repeats = 0
    for match in _STYLE_BLOCK_RE.finditer(html):
        key = _normalise(match.group(1))
        if not key:
            continue
        if key in seen:
            repeats += 1
        seen.add(key)
    return repeats


@dataclass
class StructureReport:
    """What is structurally wrong with a generated page."""

    leaked_css: list[str] = field(default_factory=list)
    duplicate_blocks: list[tuple[str, int]] = field(default_factory=list)
    duplicate_styles: int = 0
    stray_style_tags: int = 0

    @property
    def ok(self) -> bool:
        return not (
            self.leaked_css
            or self.duplicate_blocks
            or self.duplicate_styles
            or self.stray_style_tags
        )

    def issues(self) -> list[str]:
        out: list[str] = []
        if self.leaked_css:
            chars = sum(len(chunk) for chunk in self.leaked_css)
            out.append(
                f"stylesheet text leaked into <body> ({len(self.leaked_css)} run(s), "
                f"{chars} chars) — the page renders CSS as prose"
            )
        if self.stray_style_tags:
            out.append(f"{self.stray_style_tags} stray <style> tag(s) outside <head>")
        if self.duplicate_styles:
            out.append(f"{self.duplicate_styles} duplicate <style> block(s)")
        for tag, count in self.duplicate_blocks:
            out.append(f"<{tag}> emitted {count} times with identical content")
        return out


def audit_html_structure(html: str) -> StructureReport:
    """Report structural defects that a browser would show but tags do not."""
    body_match = _BODY_RE.search(html)
    body = body_match.group(1) if body_match else ""
    stray = len(_STRAY_STYLE_TAG_RE.findall(body)) if body else 0
    return StructureReport(
        leaked_css=leaked_css_text(html),
        duplicate_blocks=duplicate_blocks(html),
        duplicate_styles=duplicate_style_blocks(html),
        stray_style_tags=stray,
    )


def _remove_spans(html: str, spans: list[tuple[int, int]]) -> str:
    """Delete non-overlapping spans, last first so offsets stay valid."""
    out = html
    for start, end in sorted(spans, key=lambda item: item[0], reverse=True):
        out = out[:start] + out[end:]
    return out


def repair_html_structure(html: str) -> tuple[str, list[str]]:
    """Fix the structural defects that have exactly one correct repair.

    Returns ``(repaired_html, actions)``.  ``actions`` is empty when nothing was
    wrong, so a caller can tell "already fine" from "repaired" without diffing.

    What it will do:

    * move a stylesheet that leaked into ``<body>`` back into ``<style>`` — or
      drop it when the same rules are already there, which is the common case
      (the model emitted the sheet twice);
    * delete a stray ``<style>``/``</style>`` tag in the body;
    * delete a block element that repeats an earlier one byte-for-byte after
      whitespace normalisation.

    What it will not do: reorder, restyle, rewrite copy, or touch anything it
    cannot prove is a duplicate.  A page it cannot repair is left exactly as it
    was, and the verifier reports it.
    """
    actions: list[str] = []
    out = html

    # 1. Stray style tags in the body.
    body_match = _BODY_RE.search(out)
    if body_match:
        body = body_match.group(1)
        if _STRAY_STYLE_TAG_RE.search(body):
            cleaned = _STRAY_STYLE_TAG_RE.sub(" ", body)
            out = out[: body_match.start(1)] + cleaned + out[body_match.end(1):]
            actions.append("removed stray <style> tag(s) from <body>")

    # 2. Stylesheet text sitting in the body.
    leaked = leaked_css_text(out)
    if leaked:
        existing = " ".join(
            _normalise(match.group(1)) for match in _STYLE_BLOCK_RE.finditer(out)
        )
        body_match = _BODY_RE.search(out)
        body = body_match.group(1) if body_match else out
        spans: list[tuple[int, int]] = []
        novel: list[str] = []
        for start, end, candidate in _text_nodes(body):
            if not looks_like_css(candidate):
                continue
            spans.append((start, end))
            if _normalise(candidate) not in existing:
                novel.append(candidate.strip())
        if spans:
            cleaned = _remove_spans(body, spans)
            out = out[: body_match.start(1)] + cleaned + out[body_match.end(1):]
            actions.append(
                f"removed {len(spans)} stylesheet run(s) from <body>"
            )
        if novel:
            # Rules that were not already in a <style> block are real content:
            # put them where the browser will apply them instead of dropping them.
            extra = "\n".join(novel)
            style_match = _STYLE_BLOCK_RE.search(out)
            if style_match:
                out = (
                    out[: style_match.end(1)]
                    + "\n"
                    + extra
                    + out[style_match.end(1):]
                )
                actions.append("moved leaked stylesheet rules into <style>")
            else:
                head_match = _HEAD_RE.search(out)
                if head_match:
                    out = (
                        out[: head_match.end(1)]
                        + f"\n<style>\n{extra}\n</style>\n"
                        + out[head_match.end(1):]
                    )
                    actions.append("created <style> for leaked stylesheet rules")

    # 3. Duplicate <style> blocks.
    seen_styles: set[str] = set()
    style_spans: list[tuple[int, int]] = []
    for match in _STYLE_BLOCK_RE.finditer(out):
        key = _normalise(match.group(1))
        if not key:
            continue
        if key in seen_styles:
            style_spans.append((match.start(), match.end()))
        seen_styles.add(key)
    if style_spans:
        out = _remove_spans(out, style_spans)
        actions.append(f"removed {len(style_spans)} duplicate <style> block(s)")

    # 4. Duplicate block elements.
    for tag in _BLOCK_TAGS:
        seen_blocks: set[str] = set()
        spans: list[tuple[int, int]] = []
        for start, end, block in _block_spans(out, tag):
            key = _normalise(_TAG_RE.sub(" ", block))
            if len(key) < 20:
                continue
            if key in seen_blocks:
                spans.append((start, end))
            seen_blocks.add(key)
        if spans:
            out = _remove_spans(out, spans)
            actions.append(f"removed {len(spans)} duplicate <{tag}> block(s)")

    return out, actions
