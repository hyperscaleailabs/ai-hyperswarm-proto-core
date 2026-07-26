"""Task -> model-size selection.

This is a first-class, deliberately-learnable capability (heuristic-v1). The
orchestrator asks :func:`select` which model to run for a given task; the
returned :class:`ModelChoice` is recorded on the PR for auditability.

The heuristic combines:
- Keyword signals (architecture, security, docs, etc.)
- Task structure (files touched, kind: heal/implement/improve)
- Learned thresholds calibrated over multiple iterations (heuristic-v2, see
  :mod:`hsai.calibration`); heuristic-v1 thresholds are the safe fallback.
- Escalate-on-retry: a retried ticket (attempt > 1) is bumped one tier up,
  because the same tier already failed it once.

Evidence-based and intentionally auditable so improvements can be tracked
via backlog skills rather than hidden magic.

References:
- microsoft/JARVIS: LLM controller routing sub-tasks to the right model
- OpenBMB/ChatDev: a learnable orchestrator tuned to cut compute
- Task complexity signals from top-10 reference projects
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .calibration import TIER_ORDER, CalibrationParams
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
    signal: str = ""  # machine-readable name of the rule that fired


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


def _escalate(tier: str, steps: int) -> str:
    """Bump ``tier`` up ``steps`` levels along light -> standard -> heavy."""
    if steps <= 0 or tier not in TIER_ORDER:
        return tier
    idx = TIER_ORDER.index(tier)
    return TIER_ORDER[min(idx + steps, len(TIER_ORDER) - 1)]


def select(
    task: Task,
    cfg: CoreConfig,
    *,
    attempt: int = 1,
    params: CalibrationParams | None = None,
) -> ModelChoice:
    """Pick a tier for ``task`` and resolve it to a concrete model alias.

    ``params`` carries the learned thresholds (heuristic-v2). When omitted, the
    heuristic-v1 fallback is used verbatim, so callers that pass only
    ``(task, cfg)`` get the original static behavior.

    ``attempt`` is the 1-based try count: a retried ticket (``attempt > 1``)
    escalates one tier per prior failure, because the tier that produced it
    already failed once. Escalation is the primary retry lever.
    """
    params = params or CalibrationParams.fallback()
    score = _score(task)

    # Size labels (set by the synthesis planner) override keyword scoring:
    # substantial tickets must never fall to the light tier.
    if "size:L" in task.labels:
        tier, why, signal = "heavy", "size:L label - large synthesized change", "size-label"
    elif "size:M" in task.labels:
        tier, why, signal = (
            cfg.default_tier,
            "size:M label - substantial synthesized change",
            "size-label",
        )
    # Tier thresholds; calibrated from the lesson corpus (heuristic-v2) and
    # clamped conservatively, with heuristic-v1 as the sparse-data fallback.
    elif score >= params.heavy_threshold:
        tier = "heavy"
        why = f"score>={params.heavy_threshold}: high-complexity signals (architecture, hard bug)"
        signal = "score-heavy"
    elif score <= params.light_threshold and task.est_files <= 2:
        # Light tier is reserved for genuinely mechanical, narrow edits. A
        # haiku worker once "completed" a feature ticket with a code-free
        # diff - broad or feature-shaped work never routes light again.
        tier = "light"
        why = f"score<={params.light_threshold}: low-complexity signals (docs, format, mechanical edit)"
        signal = "score-light"
    else:
        tier = cfg.default_tier
        why = "no strong signal; using default tier"
        signal = "default"

    # Escalate-on-retry: the prior attempt at this tier failed, so try higher.
    if attempt > 1:
        escalated = _escalate(tier, attempt - 1)
        if escalated != tier:
            why = f"escalate-on-retry (attempt {attempt}): {tier}->{escalated}; prior {why}"
            tier = escalated
            signal = "escalate-retry"

    # Fall back gracefully if a tier is not configured.
    if tier not in cfg.tiers:
        tier = cfg.default_tier

    model = cfg.tiers[tier].model
    rationale = f"score={score} -> {tier} [signal={signal}] ({why})"
    return ModelChoice(
        tier=tier, model=model, rationale=rationale,
        strategy=params.strategy, signal=signal,
    )
