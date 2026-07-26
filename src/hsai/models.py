"""Task -> model-size selection.

This is a first-class, deliberately-learnable capability (heuristic-v1). The
orchestrator asks :func:`select` which model to run for a given task; the
returned :class:`ModelChoice` is recorded on the PR for auditability.

The heuristic combines:
- Keyword signals (architecture, security, docs, etc.)
- Task structure (files touched, kind: heal/implement/improve)
- Learned thresholds calibrated over multiple iterations

Evidence-based and intentionally auditable so improvements can be tracked
via backlog skills rather than hidden magic.

References:
- microsoft/JARVIS: LLM controller routing sub-tasks to the right model
- Task complexity signals from top-10 reference projects
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .config import CoreConfig

# Signal words weighted by complexity impact.
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
    "refactor",
    "breaking",
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
    "whitespace",
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
    strategy: str = "heuristic-v1"


def _score(task: Task) -> int:
    """Complexity score: positive => heavier, negative => lighter.

    Combines keyword signals, structural signals, and task kind into a
    unified score. Calibrated to distinguish light/standard/heavy across
    a range of task types.

    Score ranges (see thresholds in select()):
    - [-inf, -3]: Light tasks (docs, trivial edits, formatting)
    - (-3, 5): Standard tasks (features, small bugfixes, simple refactors)
    - [5, inf]: Heavy tasks (architecture, hard bugs, migrations)
    """
    text = f"{task.title}\n{task.body}\n{' '.join(task.labels)}".lower()
    score = 0

    # Keyword-based signals: moderate weight to allow structural signals
    # to shift the tier in edge cases.
    for w in _HEAVY_SIGNALS:
        if w in text:
            score += 2

    for w in _LIGHT_SIGNALS:
        if w in text:
            score -= 2

    # Structural signals: file count is a strong proxy for complexity.
    # Calibrated from observed patterns:
    # - Single-file changes are usually light (docs, single function)
    # - 2-3 files are standard (typical feature/bugfix)
    # - 4-7 files indicate refactor or moderate redesign
    # - 8+ files suggest architectural change or large refactor
    if task.est_files >= 8:
        score += 3
    elif task.est_files >= 4:
        score += 1
    elif task.est_files >= 2:
        score += 0
    else:
        score -= 1

    # Task kind: heal (failing CI) requires careful reasoning.
    if task.kind == "heal":
        score += 2
    elif task.kind == "improve":
        score += 1

    # Context-aware adjustment: narrow docs tasks (single file) bump down.
    if re.search(r"\b(doc|docs|readme|comment)\b", text) and task.est_files <= 1:
        score -= 1

    return score


def select(task: Task, cfg: CoreConfig) -> ModelChoice:
    """Pick a tier for ``task`` and resolve it to a concrete model alias.

    Thresholds calibrated to reflect observed task complexity distribution:
    - Heavy (>= 5): Architecture, migrations, hard bugs, large refactors
    - Light (<= -3): Docs, formatting, trivial edits, chores
    - Standard: Everything else (features, small bugfixes, simple refactors)
    """
    score = _score(task)

    # Size labels (set by the synthesis planner) override keyword scoring:
    # substantial tickets must never fall to the light tier.
    if "size:L" in task.labels:
        tier, why = "heavy", "size:L label - large synthesized change"
    elif "size:M" in task.labels:
        tier, why = cfg.default_tier, "size:M label - substantial synthesized change"
    # Tier thresholds; calibrated by iterating and comparing against
    # actual task complexity over multiple runs.
    elif score >= 5:
        tier = "heavy"
        why = "high-complexity signals (architecture, hard bug, large refactor)"
    elif score <= -3 and task.est_files <= 2:
        # Light tier is reserved for genuinely mechanical, narrow edits. A
        # haiku worker once "completed" a feature ticket with a code-free
        # diff - broad or feature-shaped work never routes light again.
        tier = "light"
        why = "low-complexity signals (docs, format, mechanical edit)"
    else:
        tier = cfg.default_tier
        why = "no strong signal; using default tier"

    # Fall back gracefully if a tier is not configured.
    if tier not in cfg.tiers:
        tier = cfg.default_tier

    model = cfg.tiers[tier].model
    rationale = f"score={score} -> {tier} ({why})"
    return ModelChoice(tier=tier, model=model, rationale=rationale, strategy="heuristic-v1")
