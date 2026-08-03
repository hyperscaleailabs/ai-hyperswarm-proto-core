"""Task -> model-size selection.

This is a first-class, deliberately-learnable capability. The orchestrator asks
:func:`select` which model to run for a given task; the returned
:class:`ModelChoice` carries both a human-readable rationale and a
machine-readable signal list, and both are recorded on the PR for auditability.

The selector combines:
- Keyword signals (architecture, security, docs, etc.)
- Task structure (files touched, kind: heal/implement/improve)
- Thresholds that are *learned* from the lesson corpus (see
  :mod:`hsai.calibration`); heuristic-v1's constants remain the fallback
- Escalate-on-retry: a second attempt at a ticket gets a heavier model, so a
  retry is not spent on the tier that already failed

Scoring stays pure and threshold-driven so a selection can be replayed from the
score, the params artifact, and the attempt number alone.

References:
- microsoft/JARVIS: LLM controller routing sub-tasks to the right model
- OpenBMB/ChatDev: an orchestrator optimised to activate the cheapest agent that
  still does the job
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from .calibration import HEURISTIC_V1, CalibrationParams
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
    """The selected tier plus everything needed to explain it after the fact."""

    tier: str
    model: str
    rationale: str  # human-readable
    strategy: str = "heuristic-v1"
    score: int = 0
    attempt: int = 1
    signals: tuple[str, ...] = ()  # machine-readable: which signals fired, in order

    def as_dict(self) -> dict:
        return {
            "tier": self.tier,
            "model": self.model,
            "strategy": self.strategy,
            "score": self.score,
            "attempt": self.attempt,
            "signals": list(self.signals),
        }

    def rationale_json(self) -> str:
        """Compact, stable JSON - what gets recorded on the PR for machines."""
        return json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))


def _score(task: Task) -> int:
    """Complexity score: positive => heavier, negative => lighter.

    Combines keyword signals, structural signals, and task kind into a
    unified score. Calibrated to distinguish light/standard/heavy across
    a range of task types.

    The score is deliberately stable: what *moves* between strategy versions are
    the thresholds it is compared against (see :func:`select`), not the scoring
    itself, so an old selection can still be replayed from its recorded score.

    Score ranges under heuristic-v1's thresholds:
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


def select(
    task: Task,
    cfg: CoreConfig,
    *,
    demote: bool = False,
    attempt: int = 1,
    params: CalibrationParams | None = None,
) -> ModelChoice:
    """Pick a tier for ``task`` and resolve it to a concrete model alias.

    ``params`` carries the learned thresholds (see :mod:`hsai.calibration`); the
    orchestrator loads them from the repo's versioned artifact and passes them
    in. Omitting them selects with heuristic-v1's constants, which is always a
    correct - if unlearned - answer:
    - Heavy (score >= 5): architecture, migrations, hard bugs, large refactors
    - Light (score <= -3): docs, formatting, trivial edits, chores
    - Standard: everything else (features, small bugfixes, simple refactors)

    ``attempt`` is the ticket's 1-based try count. A retry escalates the tier by
    one level per prior attempt, because re-running the model that already
    failed the ticket mostly buys another failure before ``max_ticket_attempts``
    marks it blocked.

    ``demote`` biases the choice one tier cheaper (heavy->standard->light). The
    budget gate sets it on a soft breach so a block that is burning quota keeps
    making progress on cheaper tiers instead of halting outright. It is applied
    *after* escalation: a quota ceiling outranks a retry.
    """
    from .ledger import demote_tier, promote_tier

    p = params or HEURISTIC_V1
    score = _score(task)
    signals: list[str] = []

    # Size labels (set by the synthesis planner) override keyword scoring:
    # substantial tickets must never fall to the light tier.
    if "size:L" in task.labels:
        tier, why = "heavy", "size:L label - large synthesized change"
        signals.append("label:size:L")
    elif "size:M" in task.labels:
        tier, why = cfg.default_tier, "size:M label - substantial synthesized change"
        signals.append("label:size:M")
    elif score >= p.heavy_threshold:
        tier = "heavy"
        why = "high-complexity signals (architecture, hard bug, large refactor)"
        signals.append(f"score>={p.heavy_threshold}")
    elif score <= p.light_threshold and task.est_files <= 2:
        # Light tier is reserved for genuinely mechanical, narrow edits. A
        # haiku worker once "completed" a feature ticket with a code-free
        # diff - broad or feature-shaped work never routes light again.
        tier = "light"
        why = "low-complexity signals (docs, format, mechanical edit)"
        signals.append(f"score<={p.light_threshold}")
    else:
        tier = cfg.default_tier
        why = "no strong signal; using default tier"
        signals.append("default-tier")

    # Escalate-on-retry: the primary lever against burning a ticket's remaining
    # attempts on a model that has already failed it.
    if attempt > 1 and p.escalate_on_retry:
        heavier = promote_tier(tier, steps=attempt - 1)
        if heavier != tier:
            why = f"{why}; escalated {tier}->{heavier} on attempt {attempt}"
            signals.append(f"retry-escalation:attempt={attempt}")
            tier = heavier
        else:
            signals.append(f"retry-escalation:capped-at-{tier}")

    # Soft budget breach: bias one tier cheaper so the block keeps progressing
    # without burning more heavy-tier quota.
    if demote:
        cheaper = demote_tier(tier)
        if cheaper != tier:
            why = f"{why}; demoted {tier}->{cheaper} under soft budget breach"
            signals.append("budget-demotion")
            tier = cheaper

    # Fall back gracefully if a tier is not configured.
    if tier not in cfg.tiers:
        signals.append(f"unconfigured-tier:{tier}")
        tier = cfg.default_tier

    model = cfg.tiers[tier].model
    rationale = f"score={score} -> {tier} ({why}) [attempt={attempt}]"
    return ModelChoice(
        tier=tier,
        model=model,
        rationale=rationale,
        strategy=p.strategy,
        score=score,
        attempt=attempt,
        signals=tuple(signals),
    )
