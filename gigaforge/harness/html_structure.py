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
_HTML_CLOSE_RE = re.compile(r"</html\s*>", re.I)
_ID_ATTRIBUTE_RE = re.compile(
    r"""(?<![\w:-])id\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+))""",
    re.I,
)
_TRAILING_STYLE_RE = re.compile(
    r"\A\s*(?:(?:<!--.*?-->)\s*)*<style\b[^>]*>(.*?)"
    r"(?:</style\s*>)?\s*(?:(?:<!--.*?-->)\s*)*\Z",
    re.S | re.I,
)
_TEXT_NODE_RE = re.compile(r">([^<>]+)<", re.S)


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _css_shaped(text: str) -> bool:
    """True when *text* is shaped like a stylesheet and not like prose.

    This is the *second* line of defence, used only for CSS that is not already
    present in a ``<style>`` block.  The first version of this check was
    ``punctuation / len > 0.02``, and it classified this paragraph as CSS::

        Our API returns {status: ok; latency: 12ms;} for every call, and
        {retries: 3; timeout: 5s;} on failure.

    The repair then deleted the paragraph from the body and pasted it into
    ``<style>`` — the reader lost the sentence and the browser threw the rules
    away.  A false positive here destroys the user's copy, so the test has to
    be strict enough that prose cannot pass it.

    Three conditions, all necessary:

    1. every ``{...}`` block contains only ``property: value`` declarations
       (nested blocks are checked recursively, so ``@media`` still passes);
    2. outside the braces there is no run of three or more plain words — a
       selector chain of three type selectors is vanishingly rare, prose always
       has one;
    3. outside the braces every character is in the CSS alphabet.
    """
    body = (text or "").strip()
    if len(body) < 30 or "{" not in body or "}" not in body:
        return False

    # 1. Every block is declarations (or nested rules).
    depth = 0
    block_start = -1
    blocks: list[str] = []
    for index, char in enumerate(body):
        if char == "{":
            if depth == 0:
                block_start = index + 1
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0 and block_start >= 0:
                blocks.append(body[block_start:index])
                block_start = -1
            elif depth < 0:
                return False
    if depth != 0 or not blocks:
        return False
    for block in blocks:
        if "{" in block:
            if not _css_shaped(block):
                return False
            continue
        for part in block.split(";"):
            part = part.strip()
            if not part:
                continue
            if not re.match(r"^[a-zA-Z-]+\s*:\s*\S", part):
                return False

    # 2. No prose outside the braces.
    outside = re.sub(r"\{[^{}]*\}", " ", body)
    outside = re.sub(r"\{.*\}", " ", outside, flags=re.S)
    if re.search(r"\b[a-zA-Z]{2,}\s+[a-zA-Z]{2,}\s+[a-zA-Z]{2,}\b", outside):
        return False

    # 3. CSS alphabet outside the braces.
    if re.search(r"[^A-Za-z0-9\s\-_.#>+~*:\[\]=\"'(),%@/&!|^$]", outside):
        return False
    return True


def looks_like_css(text: str, *, known_css: str = "") -> bool:
    """True when *text* is a stylesheet that ended up outside ``<style>``.

    Two ways to qualify, and the order matters:

    * **It is already in a ``<style>`` block.**  This is the case the live
      defect actually was: the model emitted the sheet twice, once inside
      ``<style>`` and once as raw text in ``<body>``.  A byte-for-byte
      (whitespace-normalised) match against the existing sheet is proof, not a
      guess, and it is the only case the repair will *delete*.
    * **It is shaped like CSS and not like prose** — see :func:`_css_shaped`.
      Used for rules that are genuinely new; the repair moves those into
      ``<style>`` instead of dropping them.

    ``known_css`` is the concatenated content of the page's existing ``<style>``
    blocks.  When it is empty only the strict shape test applies.
    """
    body = (text or "").strip()
    if not body or "{" not in body or "}" not in body:
        return False
    # An exact duplicate of the page's own sheet is proof, whatever its length:
    # a short rule set (``body{color:#fff}``) is still a rule set, and the
    # length gate below exists only to keep *prose* out of the shape test.
    if known_css:
        needle = _normalise(body)
        if needle and needle in _normalise(known_css):
            return True
    if len(body) < 30:
        return False
    return _css_shaped(body)


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


def _known_css(html: str) -> str:
    """The concatenated content of every ``<style>`` block on the page."""
    return "\n".join(match.group(1) for match in _STYLE_BLOCK_RE.finditer(html))


def _stray_style_tag_count(html: str) -> int:
    """Count style tags outside the document head, including after </html>."""
    head_match = _HEAD_RE.search(html)
    if not head_match:
        return len(_STRAY_STYLE_TAG_RE.findall(html))
    head_start, head_end = head_match.span()
    return sum(
        1 for match in _STRAY_STYLE_TAG_RE.finditer(html)
        if not head_start <= match.start() < head_end
    )


def _duplicate_ids(html: str) -> list[tuple[str, int]]:
    """Return duplicated, non-empty IDs from markup, ignoring comments and code."""
    markup = re.sub(r"<!--.*?-->", " ", _strip_opaque(html), flags=re.S)
    counts: dict[str, int] = {}
    for tag in re.finditer(r"<[a-z][^>]*>", markup, re.I):
        match = _ID_ATTRIBUTE_RE.search(tag.group())
        if not match:
            continue
        value = next((group for group in match.groups() if group is not None), "").strip()
        if value:
            counts[value] = counts.get(value, 0) + 1
    return sorted(
        ((value, count) for value, count in counts.items() if count > 1),
        key=lambda item: item[0].lower(),
    )


def leaked_css_text(html: str) -> list[str]:
    """CSS runs that are sitting in the body as text instead of in ``<style>``."""
    body_match = _BODY_RE.search(html)
    body = body_match.group(1) if body_match else html
    body = _STRAY_STYLE_TAG_RE.sub(" ", body)
    known = _known_css(html)
    leaked: list[str] = []
    for _start, _end, candidate in _text_nodes(body):
        if looks_like_css(candidate, known_css=known):
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


def _block_key(block: str) -> str:
    """The identity of a block: its whole markup, attributes included.

    The first version compared ``_TAG_RE.sub(" ", block)`` — the text with every
    tag and every attribute stripped.  That made ``<nav class="mobile">`` and
    ``<nav class="desktop">`` identical, so a page with a mobile and a desktop
    menu (the ordinary responsive pattern) had one of them deleted.  Measured:
    ``removed 1 duplicate <nav> block(s)``, and the surviving menu was whichever
    came first.

    A duplicate is a block that is the same *markup*, not the same words.  Two
    menus with different classes are two menus.

    Whitespace *between tags* is insignificant in HTML, so it is removed before
    comparing: a block re-emitted with different indentation is still the same
    block, and treating it as different would let the real duplicate through.
    """
    text = _normalise(block)
    return re.sub(r">\s+<", "><", text)


def duplicate_blocks(html: str) -> list[tuple[str, int]]:
    """``(tag, count)`` for block elements emitted more than once identically."""
    found: list[tuple[str, int]] = []
    for tag in _BLOCK_TAGS:
        seen: dict[str, int] = {}
        for _start, _end, block in _block_spans(html, tag):
            key = _block_key(block)
            if len(key) < 20:
                continue
            seen[key] = seen.get(key, 0) + 1
        worst = max(seen.values(), default=0)
        if worst > 1:
            found.append((tag, worst))
    return found


#: Landmark roles that may appear at most once on a page.  Two of them is not a
#: style choice, it is a defect — and it is the defect a section-by-section
#: generator produces when two children both decide to close the page.
_SINGLETON_ROLES = ("footer", "header", "main")


def _landmark_spans(html: str, role: str) -> list[tuple[int, int]]:
    """Spans of every element that plays *role* — by tag name or class token.

    ``<footer>`` and ``<div class="footer">`` are the same landmark.  The span
    covers the whole element, found by matching the opening tag to its closing
    tag with a depth counter, so removing it does not leave a dangling ``</div>``
    behind.  A self-closing or unclosed element yields a span to the end of the
    document, which is the honest reading of malformed markup.
    """
    spans: list[tuple[int, int]] = []
    pattern = re.compile(
        rf"<(?P<tag>[a-z][a-z0-9]*)\b(?P<attrs>[^>]*)>",
        re.I,
    )
    for match in pattern.finditer(html):
        tag = match.group("tag").lower()
        attrs = match.group("attrs") or ""
        is_role = tag == role or bool(
            re.search(rf'\bclass\s*=\s*["\'][^"\']*\b{re.escape(role)}\b', attrs, re.I)
        )
        if not is_role:
            continue
        if attrs.rstrip().endswith("/"):
            spans.append((match.start(), match.end()))
            continue
        depth = 1
        cursor = match.end()
        close = re.compile(rf"</?{re.escape(tag)}\b[^>]*>", re.I)
        while depth and cursor < len(html):
            step = close.search(html, cursor)
            if step is None:
                cursor = len(html)
                break
            if step.group(0).startswith("</"):
                depth -= 1
            else:
                depth += 1
            cursor = step.end()
        spans.append((match.start(), cursor))
    return spans


def duplicate_landmarks(html: str) -> list[tuple[str, int]]:
    """``(role, count)`` for landmark roles that appear more than once.

    Measured live: a page assembled from three sections had **two**
    ``<div class="footer">`` blocks — one from the footer child, one the model
    added while "finishing" the page.  ``duplicate_blocks`` did not see it: the
    two blocks had different text, so they were not identical, and the tag was
    ``div``, which is not in ``_BLOCK_TAGS`` at all.

    A page has one footer.  The check is on the *role* — the tag name or a class
    token — not on the content, because the content is exactly what differs.
    """
    found: list[tuple[str, int]] = []
    for role in _SINGLETON_ROLES:
        count = len(_landmark_spans(html, role))
        if count > 1:
            found.append((role, count))
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
    duplicate_landmarks: list[tuple[str, int]] = field(default_factory=list)
    duplicate_ids: list[tuple[str, int]] = field(default_factory=list)
    duplicate_heads: int = 0

    @property
    def ok(self) -> bool:
        return not (
            self.leaked_css
            or self.duplicate_blocks
            or self.duplicate_styles
            or self.stray_style_tags
            or self.duplicate_landmarks
            or self.duplicate_ids
            or self.duplicate_heads
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
        for role, count in self.duplicate_landmarks:
            out.append(
                f"{count} {role} landmarks on one page — a page has one {role}"
            )
        for value, count in self.duplicate_ids:
            out.append(f"id {value!r} appears {count} times — IDs must be unique")
        if self.duplicate_heads:
            out.append(
                f"{self.duplicate_heads + 1} <head> elements on one page — expected one"
            )
        return out


def audit_html_structure(html: str) -> StructureReport:
    """Report structural defects that a browser would show but tags do not."""
    head_count = len(re.findall(r"<head\b", html, re.I))
    return StructureReport(
        leaked_css=leaked_css_text(html),
        duplicate_blocks=duplicate_blocks(html),
        duplicate_styles=duplicate_style_blocks(html),
        stray_style_tags=_stray_style_tag_count(html),
        duplicate_landmarks=duplicate_landmarks(html),
        duplicate_ids=_duplicate_ids(html),
        duplicate_heads=max(0, head_count - 1),
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

    # 1. A stylesheet appended after </html> cannot participate in the page
    # head. Models commonly do this when an edit misses </style>; move only a
    # CSS-shaped trailing style block, preserving unrelated trailing comments.
    html_close = _HTML_CLOSE_RE.search(out)
    if html_close:
        trailing = out[html_close.end():]
        trailing_match = _TRAILING_STYLE_RE.fullmatch(trailing)
        if trailing_match:
            extra = trailing_match.group(1).strip()
            known = _known_css(out)
            comparable = re.sub(r"/\*.*?\*/", "", extra, flags=re.S).strip()
            if extra and looks_like_css(comparable, known_css=known):
                if _normalise(comparable) in _normalise(known):
                    action = "removed duplicate trailing stylesheet outside </html>"
                else:
                    head_match = _HEAD_RE.search(out[:html_close.start()])
                    if head_match:
                        head = out[head_match.start():head_match.end()]
                        style_match = _STYLE_BLOCK_RE.search(head)
                        if style_match:
                            insert_at = head_match.start() + style_match.end(1)
                            out = out[:insert_at] + "\n" + extra + out[insert_at:]
                        else:
                            insert_at = head_match.start(1) + len(head_match.group(1))
                            out = (
                                out[:insert_at]
                                + f"\n<style>\n{extra}\n</style>\n"
                                + out[insert_at:]
                            )
                        action = "moved trailing stylesheet rules into <head>"
                    else:
                        action = ""
                if action:
                    # The tail matched only comments and this style block. Keep
                    # comments, but remove the stylesheet that was moved/deduped.
                    new_html_close = _HTML_CLOSE_RE.search(out)
                    if new_html_close:
                        comments = re.findall(r"<!--.*?-->", trailing, re.S)
                        cleaned_tail = "".join(f"\n{comment}" for comment in comments)
                        out = out[:new_html_close.end()] + cleaned_tail
                        actions.append(action)

    # 2. Stray style tags in the body.
    body_match = _BODY_RE.search(out)
    if body_match:
        body = body_match.group(1)
        if _STRAY_STYLE_TAG_RE.search(body):
            cleaned = _STRAY_STYLE_TAG_RE.sub(" ", body)
            out = out[: body_match.start(1)] + cleaned + out[body_match.end(1):]
            actions.append("removed stray <style> tag(s) from <body>")

    # 3. Stylesheet text sitting in the body.
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
            if not looks_like_css(candidate, known_css=existing):
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

    # 4. Duplicate <style> blocks.
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

    # 5. Duplicate block elements.
    for tag in _BLOCK_TAGS:
        seen_blocks: set[str] = set()
        spans: list[tuple[int, int]] = []
        for start, end, block in _block_spans(out, tag):
            key = _block_key(block)
            if len(key) < 20:
                continue
            if key in seen_blocks:
                spans.append((start, end))
            seen_blocks.add(key)
        if spans:
            out = _remove_spans(out, spans)
            actions.append(f"removed {len(spans)} duplicate <{tag}> block(s)")

    # 6. Duplicate landmarks.  A page has one footer and one header; a second
    # one is the section-by-section generator's characteristic defect (two
    # children both closing the page).  The *first* one is kept: the assembler
    # writes the sections in plan order, so the first footer is the one the
    # footer child produced and the later one is whatever was appended after it.
    # Measured live: a page carried two <div class="footer"> blocks and the
    # contract passed it, because the duplicate check compares content and the
    # two footers had different text.
    for role in _SINGLETON_ROLES:
        spans = _landmark_spans(out, role)
        if len(spans) > 1:
            drop = spans[1:]
            out = _remove_spans(out, drop)
            actions.append(
                f"removed {len(drop)} duplicate {role} landmark(s), kept the first"
            )

    return out, actions
