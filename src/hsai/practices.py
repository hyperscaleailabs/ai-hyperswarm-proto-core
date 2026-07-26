"""Practices extracted from the reference set.

This module tracks concrete practices we've observed in the top-10 reference
projects and helps recommend which ones to adopt next.

Inspired by semantic-kernel's skill registry and gpt-researcher's explicit
plan-execute-verify structure.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Practice:
    """A concrete practice borrowed from a reference project."""

    id: str  # unique identifier
    source_repo: str  # which project this came from
    title: str  # short name
    description: str  # what it is and why it matters


# Practices we've identified in the reference set.
# When adding a new practice here, add a corresponding lesson documenting why
# we adopted it and how it improves the core.

REFERENCE_PRACTICES = [
    Practice(
        id="issue-to-pr",
        source_repo="SWE-agent/SWE-agent",
        title="Turn GitHub issues directly into validated PRs",
        description="Every change flows through: issue -> ticket -> PR -> green -> merge. "
        "No direct commits to main. This is the spine of our loop.",
    ),
    Practice(
        id="keep-core-tiny",
        source_repo="openai/swarm",
        title="Keep the core orchestration tiny and pure",
        description="Decision logic stays pure functions; side effects isolated in wrappers. "
        "The harness is <2K LOC by design.",
    ),
    Practice(
        id="config-as-crew",
        source_repo="crewAIInc/crewAI",
        title="Declarative config as the single source of truth",
        description="core.yaml defines mission, goals, execution limits, and model routing. "
        "Everything else flows from this declaration.",
    ),
    Practice(
        id="model-routing-by-task",
        source_repo="microsoft/JARVIS",
        title="Route tasks to the right model based on complexity",
        description="Light tasks (docs, formatting) use Haiku; standard work uses Sonnet; "
        "hard tasks (design, architecture) use Opus. Learned over time.",
    ),
    Practice(
        id="plan-execute-verify",
        source_repo="assafelovic/gpt-researcher",
        title="Structure work as explicit plan-then-execute-then-verify phases",
        description="Make each step observable: plan what you'll do, do it, verify it worked. "
        "Gates between phases make recovery easier.",
    ),
    Practice(
        id="phased-workflow-with-gates",
        source_repo="OpenBMB/ChatDev",
        title="Phased workflow (Design -> Code -> Test -> Review) with gates",
        description="Multi-agent software development broken into clear phases. "
        "Each phase produces explicit artifacts that feed the next.",
    ),
    Practice(
        id="explicit-task-outcomes",
        source_repo="crewAIInc/crewAI",
        title="Every task produces a structured outcome (result + explanation)",
        description="Tasks are not silent. Each produces a result object that captures "
        "what was done, why, and what evidence supports it.",
    ),
    Practice(
        id="pluggable-components",
        source_repo="run-llama/llama_index",
        title="Pluggable retrieval/context assembly",
        description="Core logic doesn't assume a specific retrieval strategy. "
        "Storage, indexing, and retrieval are swappable.",
    ),
    Practice(
        id="role-based-agents",
        source_repo="FoundationAgents/MetaGPT",
        title="Role-based agents with explicit hand-off artifacts",
        description="Each agent has a clear role (planner, coder, reviewer) and produces "
        "typed outputs (plans, code, reviews) that others consume.",
    ),
    Practice(
        id="clean-adapter-pattern",
        source_repo="langchain-ai/langchain",
        title="Clean separation of orchestration from tool/provider adapters",
        description="Routing logic is separate from provider-specific code. "
        "Adding a new provider doesn't require orchestration changes.",
    ),
]


def get_practice(practice_id: str) -> Practice | None:
    """Look up a practice by ID."""
    for p in REFERENCE_PRACTICES:
        if p.id == practice_id:
            return p
    return None


def practice_by_source(source_repo: str) -> list[Practice]:
    """All practices from a given source repo."""
    return [p for p in REFERENCE_PRACTICES if p.source_repo == source_repo]
