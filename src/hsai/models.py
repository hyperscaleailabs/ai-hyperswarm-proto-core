"""Task -> model-size selection.

This is a first-class, deliberately-learnable capability (heuristic-v2). The
orchestrator asks :func:`select` which model to run for a given task; the
returned :class:`ModelChoice` is recorded on the PR for auditability.

The heuristic combines:
- Keyword signals (architecture, security, docs, etc.)
- Task structure (files touched, kind: heal/implement/improve)
- Thresholds that are now *data*: every weight, bucket and threshold lives in
  the versioned :class:`~hsai.policy.SelectionPolicy` committed at
  ``.ai-swarm/selection-policy.json`` and is calibrated against the quota ledger
  by ``hsai calibrate``.

Two surfaces, kept apart on purpose:
- **Tunable** - the policy file. A calibration may move it, bounded and
  reviewable as a JSON diff in the governance PR.
- **Invariant** - the ``size:L``/``size:M`` overrides, the
  "feature-shaped work never routes light" guard, and the budget-gate demotion.
  These are enforced here, below the policy, and no calibration can reach them.

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
from .policy import SelectionPolicy, load_policy

STRATEGY = "heuristic-v2"

# INVARIANT (not tunable): the light tier is reserved for genuinely mechanical,
# narrow edits. A haiku worker once "completed" a feature ticket with a
# code-free diff - broad or feature-shaped work never routes light again.
LIGHT_MAX_FILES = 2

# INVARIANT (not tunable): synthesis-planner size labels outrank keyword scoring.
SIZE_L_LABEL = "size:L"
SIZE_M_LABEL = "size:M"

_NARROW_DOCS_RE = re.compile(r"\b(doc|docs|readme|comment)\b")


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
    strategy: str = STRATEGY


def _score(task: Task, policy: SelectionPolicy | None = None) -> int:
    """Complexity score: positive => heavier, negative => lighter.

    Combines keyword signals, structural signals, and task kind into a
    unified score using ``policy``'s weights (heuristic-v1's values by default).

    Score ranges (see the policy's thresholds, applied in :func:`decide_tier`):
    - at or below ``light_threshold``: light tasks (docs, trivial edits)
    - between the thresholds: standard tasks (features, small bugfixes)
    - at or above ``heavy_threshold``: heavy tasks (architecture, hard bugs)
    """
    p = policy or load_policy()
    text = f"{task.title}\n{task.body}\n{' '.join(task.labels)}".lower()
    score = 0

    # Keyword-based signals: moderate weight to allow structural signals
    # to shift the tier in edge cases.
    for w in p.heavy_signals:
        if w in text:
            score += p.heavy_signal_weight

    for w in p.light_signals:
        if w in text:
            score += p.light_signal_weight

    # Structural signals: file count is a strong proxy for complexity.
    # Calibrated from observed patterns:
    # - Single-file changes are usually light (docs, single function)
    # - 2-3 files are standard (typical feature/bugfix)
    # - 4-7 files indicate refactor or moderate redesign
    # - 8+ files suggest architectural change or large refactor
    score += p.file_delta(task.est_files)

    # Task kind: heal (failing CI) requires careful reasoning.
    score += p.kind_weight(task.kind)

    # Context-aware adjustment: narrow docs tasks (single file) bump down.
    if _NARROW_DOCS_RE.search(text) and task.est_files <= 1:
        score += p.narrow_docs_delta

    return score


def decide_tier(
    task: Task, policy: SelectionPolicy, *, default_tier: str = "standard"
) -> tuple[int, str, str]:
    """Pure tier decision: ``(score, tier, why)``.

    The single code path for routing - :func:`select` uses it for live work and
    :mod:`hsai.calibrate` replays historical work through it, so a proposed
    policy is always evaluated under the same invariants that will enforce it.
    """
    score = _score(task, policy)

    # Size labels (set by the synthesis planner) override keyword scoring:
    # substantial tickets must never fall to the light tier. INVARIANT.
    if SIZE_L_LABEL in task.labels:
        return score, "heavy", "size:L label - large synthesized change"
    if SIZE_M_LABEL in task.labels:
        return score, default_tier, "size:M label - substantial synthesized change"

    # Tier thresholds; tunable via the policy file, calibrated against the ledger.
    if score >= policy.heavy_threshold:
        return score, "heavy", "high-complexity signals (architecture, hard bug, large refactor)"
    if score <= policy.light_threshold and task.est_files <= LIGHT_MAX_FILES:
        return score, "light", "low-complexity signals (docs, format, mechanical edit)"
    return score, default_tier, "no strong signal; using default tier"


def select(
    task: Task,
    cfg: CoreConfig,
    *,
    demote: bool = False,
    policy: SelectionPolicy | None = None,
) -> ModelChoice:
    """Pick a tier for ``task`` and resolve it to a concrete model alias.

    Thresholds come from the active :class:`~hsai.policy.SelectionPolicy`
    (``.ai-swarm/selection-policy.json``); the version that produced the routing
    is reported in :attr:`ModelChoice.strategy` and lands in the PR body.

    ``demote`` biases the choice one tier cheaper (heavy->standard->light). The
    budget gate sets it on a soft breach so a block that is burning quota keeps
    making progress on cheaper tiers instead of halting outright. INVARIANT: the
    demotion path is enforced here, outside the tunable policy surface.
    """
    p = policy or load_policy()
    score, tier, why = decide_tier(task, p, default_tier=cfg.default_tier)

    # Soft budget breach: bias one tier cheaper so the block keeps progressing
    # without burning more heavy-tier quota.
    if demote:
        from .ledger import demote_tier

        cheaper = demote_tier(tier)
        if cheaper != tier:
            why = f"{why}; demoted {tier}->{cheaper} under soft budget breach"
            tier = cheaper

    # Fall back gracefully if a tier is not configured.
    if tier not in cfg.tiers:
        tier = cfg.default_tier

    model = cfg.tiers[tier].model
    rationale = f"score={score} -> {tier} ({why})"
    return ModelChoice(
        tier=tier, model=model, rationale=rationale, strategy=f"{STRATEGY} ({p.label()})"
    )
