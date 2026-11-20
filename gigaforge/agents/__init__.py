"""GigaForge agents — the reasoning units that sit on top of the DAG.

Three families, distinguished by what they cost and what they return:

**core trio** (planner / coder / reviewer) — one model call each, a parsed plan,
a unified diff or a verdict back.  The V2 pipeline drives these directly.

**reasoning strategies** (:mod:`tot`, :mod:`react`, :mod:`constitutional`,
:mod:`reflection`, :mod:`self_consistency`, :mod:`self_debug`) — higher-order
algorithms that *compose* model calls: a K-branch plan search, a bounded
tool-use loop, a principle gate, a critique pass, a k-sample vote, a failure
diagnosis.  In the V2 engine these are reachable as pluggable stages
(:mod:`gigaforge.core.strategies`) rather than as a parallel pipeline.

**context helpers** (:mod:`memory`, :mod:`localizer`, :mod:`retrieval`,
:mod:`summarizer`) — zero-token or single-call units that decide *what the model
sees*: episodic recall, symbol localisation, snippet ranking and the durable
learnings extract.

Everything is reachable from this package, and ``AGENT_CATALOG`` is the single
description both the CLI and the desktop window render — so the two front ends
cannot disagree about which agents exist.

Imports of the advanced families are **lazy**: ``import gigaforge.agents`` stays
cheap (the core trio only), and ``gigaforge.agents.ToTPlanner`` resolves the
module on first attribute access.  That keeps a cold start of the desktop window
from pulling in tree-sitter, the sandbox and the worktree machinery.
"""
from __future__ import annotations

from typing import Any

from gigaforge.agents.coder import CoderAgent, is_diff
from gigaforge.agents.planner import (
    ALLOWED_TOOLS,
    PlanEscalation,
    PlannerAgent,
    json_repair,
    parse_plan_json,
)
from gigaforge.agents.reviewer import ReviewerAgent, ReviewVerdict
from gigaforge.agents.subagents import AgentFactory, SubAgentRunner, SubAgentSpawn

#: Human-readable catalog of every agent in the harness.
AGENT_CATALOG: list[dict[str, str]] = [
    {
        "name": "planner",
        "class": "PlannerAgent",
        "family": "core",
        "role": "Decomposes the goal into ordered, tool-shaped steps (cheap model).",
        "role_ru": "Раскладывает задачу на шаги и проверяемые условия приёмки.",
        "stage": "plan",
    },
    {
        "name": "coder",
        "class": "CoderAgent",
        "family": "core",
        "role": "Authors a unified diff against the context slice (strong model).",
        "role_ru": "Пишет единый дифф по срезу контекста.",
        "stage": "act",
    },
    {
        "name": "reviewer",
        "class": "ReviewerAgent",
        "family": "core",
        "role": "Judges whether the diff solves the goal; fail-safe to reject.",
        "role_ru": "Решает, решает ли дифф задачу; при сомнении отклоняет.",
        "stage": "review",
    },
    {
        "name": "tot",
        "class": "ToTPlanner",
        "family": "reasoning",
        "role": "Tree-of-thought: K candidate plans, heuristic scoring, optional judge.",
        "role_ru": "Ветвит план: несколько вариантов, оценка, судья по требованию.",
        "stage": "plan (GIGAFORGE_PLAN_STRATEGY=tot)",
    },
    {
        "name": "constitutional",
        "class": "ConstitutionalReviewer",
        "family": "reasoning",
        "role": "Principle gate: deterministic blockers plus one soft principle judged by the model.",
        "role_ru": "Детерминированные блокеры плюс один мягкий принцип.",
        "stage": "review (GIGAFORGE_REVIEW_STRATEGY=constitutional)",
    },
    {
        "name": "react",
        "class": "ReActRunner",
        "family": "reasoning",
        "role": "Bounded observe/act/observe loop over allowlisted tools.",
        "role_ru": "Цикл наблюдение–действие в рамках разрешённых инструментов.",
        "stage": "exploration",
    },
    {
        "name": "reflection",
        "class": "ReflectorAgent",
        "family": "reasoning",
        "role": "Critiques a rejected diff and turns the critique into the next prompt.",
        "role_ru": "Разбирает отклонённый дифф и делает разбор следующим промптом.",
        "stage": "fix",
    },
    {
        "name": "self_consistency",
        "class": "SelfConsistencyCoder",
        "family": "reasoning",
        "role": "Samples k diffs and votes per hunk; needs a worktree to try each sample.",
        "role_ru": "Пишет несколько диффов и голосует по фрагментам.",
        "stage": "act (opt-in)",
    },
    {
        "name": "self_debug",
        "class": "SelfDebugger",
        "family": "reasoning",
        "role": "Diagnoses a failing test into a precise repair instruction.",
        "role_ru": "Превращает падение теста в точную инструкцию правки.",
        "stage": "fix",
    },
    {
        "name": "localizer",
        "class": "LocalizerAgent",
        "family": "context",
        "role": "Picks the files and symbols a goal touches, deterministically first.",
        "role_ru": "Находит файлы и символы, которых касается задача.",
        "stage": "slice",
    },
    {
        "name": "retrieval",
        "class": "Retriever",
        "family": "context",
        "role": "Ranks snippets out of the symbol index for a query (zero model calls).",
        "role_ru": "Подбирает срезы кода по индексу символов.",
        "stage": "slice",
    },
    {
        "name": "summarizer",
        "class": "SummarizerAgent",
        "family": "context",
        "role": "Writes the user-facing summary and extracts durable learnings.",
        "role_ru": "Сжимает историю и большие файлы до бюджета токенов.",
        "stage": "summarize",
    },
    {
        "name": "memory",
        "class": "MemoryRetriever",
        "family": "context",
        "role": "Episodic recall of earlier runs' learnings for the same task family.",
        "role_ru": "Держит долгую память проекта между прогонами.",
        "stage": "intake",
    },
    {
        "name": "subagents",
        "class": "SubAgentRunner",
        "family": "delegation",
        "role": "Fans work into git worktrees with isolated context, merges in spawn order.",
        "role_ru": "Раздаёт циклы подагентам с тем же финальным гейтом.",
        "stage": "fan_out",
    },
]

#: ``public name -> "module:attribute"`` for the lazily imported families.
_LAZY: dict[str, str] = {
    "CandidatePlan": "gigaforge.agents.tot:CandidatePlan",
    "Constitution": "gigaforge.agents.constitutional:Constitution",
    "ConstitutionalReviewer": "gigaforge.agents.constitutional:ConstitutionalReviewer",
    "ContextBag": "gigaforge.agents.retrieval:ContextBag",
    "Critique": "gigaforge.agents.reflection:Critique",
    "Diagnosis": "gigaforge.agents.self_debug:Diagnosis",
    "LocalizationResult": "gigaforge.agents.localizer:LocalizationResult",
    "LocalizerAgent": "gigaforge.agents.localizer:LocalizerAgent",
    "MemoryRetriever": "gigaforge.agents.memory:MemoryRetriever",
    "Principle": "gigaforge.agents.constitutional:Principle",
    "PrincipleResult": "gigaforge.agents.constitutional:PrincipleResult",
    "ReActResult": "gigaforge.agents.react:ReActResult",
    "ReActRunner": "gigaforge.agents.react:ReActRunner",
    "ReflectorAgent": "gigaforge.agents.reflection:ReflectorAgent",
    "Retriever": "gigaforge.agents.retrieval:Retriever",
    "SelfConsistencyCoder": "gigaforge.agents.self_consistency:SelfConsistencyCoder",
    "SelfConsistencyResult": "gigaforge.agents.self_consistency:SelfConsistencyResult",
    "SelfDebugger": "gigaforge.agents.self_debug:SelfDebugger",
    "Snippet": "gigaforge.agents.retrieval:Snippet",
    "SummarizerAgent": "gigaforge.agents.summarizer:SummarizerAgent",
    "ToTPlanner": "gigaforge.agents.tot:ToTPlanner",
    "ToTResult": "gigaforge.agents.tot:ToTResult",
    "ToolRegistry": "gigaforge.agents.react:ToolRegistry",
    "load_constitution": "gigaforge.agents.constitutional:load_constitution",
    "score_plan_heuristic": "gigaforge.agents.tot:score_plan_heuristic",
}


def __getattr__(name: str) -> Any:
    """Resolve a lazily exported agent on first access (PEP 562)."""
    target = _LAZY.get(name)
    if target is None:
        raise AttributeError(f"module 'gigaforge.agents' has no attribute {name!r}")
    module_name, attribute = target.split(":")
    from importlib import import_module

    module = import_module(module_name)
    value = getattr(module, attribute)
    globals()[name] = value  # cache so the next access is a plain dict lookup
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_LAZY) | set(_CORE_EXPORTS))


_CORE_EXPORTS = [
    "AGENT_CATALOG",
    "ALLOWED_TOOLS",
    "AgentFactory",
    "CoderAgent",
    "PlanEscalation",
    "PlannerAgent",
    "ReviewerAgent",
    "ReviewVerdict",
    "SubAgentRunner",
    "SubAgentSpawn",
    "is_diff",
    "json_repair",
    "parse_plan_json",
]

__all__ = _CORE_EXPORTS + sorted(_LAZY)
