"""Task -> model-size selection.

This is a first-class, deliberately-learnable capability (heuristic-v0). The
orchestrator asks :func:`select` which model to run for a given task; the
returned :class:`ModelChoice` is recorded on the PR for auditability.

The heuristic is intentionally simple and evidence-based so it can be improved
by a tracked backlog skill (see the seeded issues) rather than hidden magic.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .config import CoreConfig

# Signal words that pull a task toward a heavier or lighter tier.
_HEAVY_SIGNALS = (
    "architecture",
    "redesign",
    "large refactor",
    "rearchitect",
    "hard bug",
    "race condition",
    "concurrency",
    "security",
    "design",
    "migration",
)
_LIGHT_SIGNALS = (
    "typo",
    "docs",
    "documentation",
    "readme",
    "format",
    "lint",
    "rename",
    "comment",
    "index",
    "chore",
    "bump",
)


@dataclass(frozen=True)
class Task:
    """A unit of work the orchestrator is about to hand to a model."""

    kind: str  # heal | implement | improve
    title: str
    body: str = ""
    labels: tuple[str, ...] = ()
    est_files: int = 1


@dataclass(frozen=True)
class ModelChoice:
    tier: str
    model: str
    rationale: str
    strategy: str = "heuristic-v0"


def _score(task: Task) -> int:
    """Positive => heavier, negative => lighter."""
    text = f"{task.title}\n{task.body}\n{' '.join(task.labels)}".lower()
    score = 0
    for w in _HEAVY_SIGNALS:
        if w in text:
            score += 2
    for w in _LIGHT_SIGNALS:
        if w in text:
            score -= 2
    # Structural signals.
    if task.est_files >= 8:
        score += 2
    elif task.est_files >= 3:
        score += 1
    if task.kind == "heal":
        score += 1  # failing CI usually needs careful reasoning
    if re.search(r"\b(doc|docs|readme|comment)\b", text) and task.est_files <= 1:
        score -= 1
    return score


def select(task: Task, cfg: CoreConfig) -> ModelChoice:
    """Pick a tier for ``task`` and resolve it to a concrete model alias."""
    score = _score(task)
    if score >= 3:
        tier = "heavy"
        why = "high-complexity signals / broad change surface"
    elif score <= -2:
        tier = "light"
        why = "low-complexity / mechanical change"
    else:
        tier = cfg.default_tier
        why = "no strong complexity signal; default tier"

    # Fall back gracefully if a tier is not configured.
    if tier not in cfg.tiers:
        tier = cfg.default_tier
    model = cfg.tiers[tier].model
    rationale = f"score={score} -> {tier} ({why})"
    return ModelChoice(tier=tier, model=model, rationale=rationale)
