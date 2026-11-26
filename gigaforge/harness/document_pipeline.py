"""The document pipeline: outline, then evidence, then sections, then refinement.

**Why a document is not a long answer.**  A single call asked for a 2000-word
document produces a plausible shape with nothing in it — the failure the live
runs caught, where every section read "This section provides an overview of X.
It covers Y and Z."  The text was structurally perfect and empty, and the
placeholder check had to be written to catch it after the fact.

PaperOrchestra's answer is to make the document a *pipeline* rather than a
prompt: an outline decided first, evidence gathered per section, each section
written against its own evidence, and a refinement pass that reads the whole
thing.  Each stage has a small, checkable output, so a failure is localised
instead of becoming a vague "the document is bad".

**What is ported and what is not.**  The mechanism — four stages, each with a
deterministic check — is ported.  The LaTeX CVPR/ICLR templates are not: they
are a venue's formatting, not a technique, and GigaForge writes Markdown.

**What the harness computes itself.**  The outline's *shape* (how many sections,
which are required) is derived from the goal, not asked of the model.  The
section count is a number the goal states or the harness picks; asking a model
to decide it is asking it to guess at a computation.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "Outline",
    "Section",
    "DocumentPlan",
    "plan_document",
    "section_prompt",
    "refinement_issues",
    "assemble",
]


#: Words that mean "this section is a placeholder", not content.  The same
#: failure the live runs shipped: a sentence that describes its own section
#: instead of saying anything about the subject.
_PLACEHOLDER_PATTERNS: tuple[str, ...] = (
    r"this section (provides|gives|covers|describes|explains) an? (overview|summary|introduction)",
    r"in this section,? (we|you) will (discuss|cover|explore|look at)",
    r"this (part|chapter) (will )?(discuss|cover|explore)",
    r"here (we|you) (will )?(discuss|cover|explore)",
    r"lorem ipsum",
    r"\[insert[^\]]*\]",
    r"tbd|todo:",
)

#: A section shorter than this is a heading with no content.
_MIN_SECTION_CHARS = 120


@dataclass
class Section:
    """One section of the outline, with the evidence it will be written from."""

    title: str
    purpose: str = ""
    evidence: list[str] = field(default_factory=list)
    body: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "purpose": self.purpose,
            "evidence": list(self.evidence),
            "chars": len(self.body),
        }


@dataclass
class Outline:
    """The document's shape, decided before any prose is written."""

    title: str
    sections: list[Section] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "sections": [s.to_dict() for s in self.sections],
        }


@dataclass
class DocumentPlan:
    """The outline plus the checks the finished document must pass."""

    outline: Outline
    min_chars: int = 0
    required_titles: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "outline": self.outline.to_dict(),
            "min_chars": self.min_chars,
            "required_titles": list(self.required_titles),
        }


#: Section titles the goal may name explicitly, in the order a document reads.
_NAMED_SECTION_RE = re.compile(
    r"(?:sections?|разделы?|главы?)\s*(?:for|:)?\s*([^.]+)", re.IGNORECASE
)


def _named_sections(goal: str) -> list[str]:
    """Section names the goal states, e.g. "with sections for intake, plan, act".

    Two shapes are natural and both are read: the explicit ``sections for a, b``
    and a trailing colon list — ``… how the harness works: Intake, Plan, Act``.
    The colon form was missed, so a goal that named its four sections got the
    default Overview/Details/Considerations/Conclusion instead, and the document
    was written to a shape the user had not asked for.
    """
    match = _NAMED_SECTION_RE.search(goal or "")
    raw = match.group(1) if match else ""
    if not raw:
        # A trailing colon list: everything after the last colon, when it reads
        # as a list of short titles rather than a sentence.
        tail = (goal or "").rsplit(":", 1)
        if len(tail) == 2:
            candidate = tail[1].strip().rstrip(".")
            parts = re.split(r",|\band\b|\bи\b|;", candidate)
            if 2 <= len(parts) <= 8 and all(
                0 < len(p.strip(" .;:")) <= 40 for p in parts
            ):
                raw = candidate
    if not raw:
        return []
    parts = re.split(r",|\band\b|\bи\b|;", raw)
    titles: list[str] = []
    for part in parts:
        title = part.strip(" .;:")
        if not title or len(title) > 60:
            continue
        titles.append(title[:1].upper() + title[1:])
    return titles


def plan_document(goal: str, *, default_sections: int = 4) -> DocumentPlan:
    """Derive the document's shape from the goal.

    **The harness decides the shape, not the model.**  A goal that names its
    sections gets exactly those; a goal that does not gets a small default.  The
    model is asked to *fill* sections, never to invent the structure — structure
    is a computation, and a model asked for it produces a different answer each
    time, which makes the document unmeasurable.
    """
    named = _named_sections(goal)
    if named:
        sections = [Section(title=t, purpose=f"Cover {t.lower()}.") for t in named]
    else:
        sections = [
            Section(title="Overview", purpose="What this document is about."),
            Section(title="Details", purpose="The substance."),
            Section(title="Considerations", purpose="Trade-offs and limits."),
            Section(title="Conclusion", purpose="What to take away."),
        ][:max(1, default_sections)]
    title = _document_title(goal)
    return DocumentPlan(
        outline=Outline(title=title, sections=sections),
        min_chars=max(400, 200 * len(sections)),
        required_titles=[s.title for s in sections],
    )


def _document_title(goal: str) -> str:
    """A title from the goal, without asking a model to invent one."""
    text = (goal or "").strip()
    text = re.sub(r"^(write|draft|create|produce|напиши|составь)\s+", "", text,
                  flags=re.IGNORECASE)
    text = re.sub(r"^(a|an|the|short|краткий)\s+", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^(document|report|guide|article|документ|отчёт|отчет)\s+",
                  "", text, flags=re.IGNORECASE)
    text = re.split(r"[.,;:]", text)[0].strip()
    if not text:
        return "Document"
    return text[:1].upper() + text[1:]


def section_prompt(section: Section, *, document_title: str = "") -> str:
    """The prompt for one section: its title, its purpose, and its evidence.

    **Why per-section and not one big prompt.**  A single call for the whole
    document lets the model spend its budget on the first section and pad the
    rest.  One call per section gives each the same attention, and a section
    that fails is re-asked alone instead of regenerating everything.
    """
    lines = [f"Write the section {section.title!r} of the document "
             f"{document_title!r}."]
    if section.purpose:
        lines.append(f"Its purpose: {section.purpose}")
    if section.evidence:
        lines.append("Use only these facts:")
        lines.extend(f"- {item}" for item in section.evidence[:12])
    lines.append(
        "Write prose that says something about the subject. Do not describe the "
        "section itself, and do not write a placeholder."
    )
    return "\n".join(lines)


def refinement_issues(document: str, plan: DocumentPlan) -> list[str]:
    """What is wrong with the finished document, deterministically.

    Four checks, each one a failure that has actually shipped:

    * a required section is missing;
    * a section is a heading with no content;
    * a section is a placeholder that describes itself;
    * the document is shorter than its plan asked for.
    """
    issues: list[str] = []
    text = document or ""
    for title in plan.required_titles:
        if not re.search(rf"^#+\s*{re.escape(title)}\s*$", text, re.MULTILINE | re.IGNORECASE):
            issues.append(f"missing section: {title}")
    for section in plan.outline.sections:
        body = _section_body(text, section.title)
        if body is None:
            continue
        if len(body.strip()) < _MIN_SECTION_CHARS:
            issues.append(
                f"section {section.title!r} has {len(body.strip())} chars of "
                f"content, expected at least {_MIN_SECTION_CHARS}"
            )
        for pattern in _PLACEHOLDER_PATTERNS:
            if re.search(pattern, body, re.IGNORECASE):
                issues.append(
                    f"section {section.title!r} is a placeholder: it describes "
                    "itself instead of the subject"
                )
                break
    if plan.min_chars and len(text) < plan.min_chars:
        issues.append(
            f"the document is {len(text)} chars, the plan asked for at least "
            f"{plan.min_chars}"
        )
    return issues


def _section_body(document: str, title: str) -> str | None:
    """The text under a heading, up to the next heading of the same or higher level."""
    pattern = re.compile(
        rf"^(#+)\s*{re.escape(title)}\s*$(.*?)(?=^#+\s|\Z)",
        re.MULTILINE | re.IGNORECASE | re.DOTALL,
    )
    match = pattern.search(document or "")
    return match.group(2) if match else None


def assemble(plan: DocumentPlan, bodies: Mapping[str, str]) -> str:
    """Join the sections into one document, in the plan's order.

    The order comes from the plan, not from the dict — a mapping has no order a
    reader can rely on, and a document whose sections shuffle between runs is
    not the same document.
    """
    parts = [f"# {plan.outline.title}", ""]
    for section in plan.outline.sections:
        body = (bodies.get(section.title) or "").strip()
        parts.append(f"## {section.title}")
        parts.append("")
        parts.append(body or "_No content was produced for this section._")
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"
