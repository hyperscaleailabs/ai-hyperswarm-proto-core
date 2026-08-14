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

# Tiers ordered cheap -> expensive; used to find a reviewer tier that is not the
# author's (mirrors ledger._TIER_ORDER, kept local to avoid a circular import).
_TIER_ORDER = ("light", "standard", "heavy")

# Who reviews whom (see :mod:`hsai.review`). Overridable via ``review.tier_policy``
# in core.yaml. Deliberately biased cheap: a review runs on EVERY change, and a
# heavy-tier reviewer would exhaust the block's heavy budget on critique alone
# (OpenBMB/ChatDev runs review phases on cheaper agents for the same reason).
DEFAULT_REVIEWER_TIERS = {
    "heavy": "standard",
    "standard": "light",
    "light": "standard",
}


@dataclass(frozen=True)
class Task:
    """A unit of work the orchestrator is about to hand to a model."""

    kind: str  # heal | implement | improve
    title: str
    body: str = ""
    labels: tuple[str, ...] = ()
    est_files: int = 1
    # Retry context (the "Handoff" - see module docstring below). ``attempt``
    # is 1 on a ticket's first try and increments with every recorded
    # ``attempts:N`` label; ``prior_failure`` is the previous attempt's failure
    # dossier (see :func:`hsai.orchestrator._recover_failed`), fed back in
    # rather than restarting blind (SWE-agent/SWE-agent's trajectory replay).
    attempt: int = 1
    prior_failure: str = ""


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
    text = f"{task.title}\n{task.body}\n{' '.join(task.labels)}\n{task.prior_failure}".lower()
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


def escalate(tier: str, attempt: int) -> str:
    """The tier ``attempt - 1`` rungs heavier than ``tier``, saturating at heavy.

    A first try (``attempt <= 1``) has nothing to escalate from and returns
    ``tier`` unchanged. This is the "Handoff" primitive from openai/swarm
    (an agent transferring control to a better-suited agent) applied to model
    tiers instead of personas: a ticket that a lighter model already failed is
    routed to a heavier one rather than repeating the same tier blind.
    """
    if attempt <= 1 or tier not in _TIER_ORDER:
        return tier
    i = _TIER_ORDER.index(tier) + (attempt - 1)
    return _TIER_ORDER[min(i, len(_TIER_ORDER) - 1)]


def select(task: Task, cfg: CoreConfig, *, demote: bool = False) -> ModelChoice:
    """Pick a tier for ``task`` and resolve it to a concrete model alias.

    Thresholds calibrated to reflect observed task complexity distribution:
    - Heavy (>= 5): Architecture, migrations, hard bugs, large refactors
    - Light (<= -3): Docs, formatting, trivial edits, chores
    - Standard: Everything else (features, small bugfixes, simple refactors)

    ``task.attempt > 1`` escalates one tier heavier per retry (see
    :func:`escalate`), UNLESS ``demote`` is set - the budget gate always wins
    over escalation, so a soft breach demotes exactly as it would on a first
    attempt rather than compounding an escalation on top of a burning budget.
    A hard breach means this function is never called at all: the caller
    (``_implementation_block``) stops starting new work before selecting a
    model for it.

    ``demote`` biases the choice one tier cheaper (heavy->standard->light). The
    budget gate sets it on a soft breach so a block that is burning quota keeps
    making progress on cheaper tiers instead of halting outright.
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

    # Escalation ladder: a retried ticket (attempt > 1) is routed to a heavier
    # tier than the attempt before it - UNLESS a soft budget breach demotes
    # instead (see the `demote` branch below, which always wins).
    if task.attempt > 1 and not demote:
        escalated = escalate(tier, task.attempt)
        if escalated != tier:
            why = f"escalated {tier}->{escalated} on attempt {task.attempt} ({why})"
            tier = escalated

    # Soft budget breach: bias one tier cheaper so the block keeps progressing
    # without burning more heavy-tier quota. Wins over escalation - a block
    # that is already burning budget must not spend even more on a retry.
    if demote:
        from .ledger import demote_tier

        cheaper = demote_tier(tier)
        if cheaper != tier:
            suffix = (
                f"; demoted {tier}->{cheaper} under soft budget breach"
                + (" (wins over escalation)" if task.attempt > 1 else "")
            )
            why = f"{why}{suffix}"
            tier = cheaper

    # Fall back gracefully if a tier is not configured.
    if tier not in cfg.tiers:
        tier = cfg.default_tier

    model = cfg.tiers[tier].model
    rationale = f"score={score} -> {tier} ({why})"
    return ModelChoice(tier=tier, model=model, rationale=rationale, strategy="heuristic-v1")


def _adjacent_tier(tier: str, cfg: CoreConfig) -> str:
    """A configured tier that is NOT ``tier`` - one step cheaper where possible."""
    order = [t for t in _TIER_ORDER if t in cfg.tiers] or [cfg.default_tier]
    if tier not in order:
        return order[0]
    i = order.index(tier)
    if i > 0:
        return order[i - 1]
    return order[1] if len(order) > 1 else tier


def select_reviewer(author: ModelChoice, cfg: CoreConfig) -> ModelChoice:
    """Pick the model that reviews ``author``'s work - never the author's tier.

    The whole point of the review gate is that the critique does not come from
    the author grading itself (FoundationAgents/MetaGPT separates the engineer
    and reviewer roles), so a different tier is an invariant here, not a
    preference: a policy that maps a tier to itself - or to a tier this repo has
    not configured - is discarded in favour of an adjacent one.
    """
    policy = dict(DEFAULT_REVIEWER_TIERS)
    policy.update({str(k): str(v) for k, v in (cfg.review.get("tier_policy") or {}).items()})
    tier = policy.get(author.tier, "")
    why = f"policy {author.tier}->{tier}"
    if tier not in cfg.tiers or tier == author.tier:
        tier = _adjacent_tier(author.tier, cfg)
        why = f"no usable policy for author tier '{author.tier}'; adjacent tier {tier}"
    return ModelChoice(
        tier=tier,
        model=cfg.tiers[tier].model,
        rationale=f"independent review of a `{author.tier}` author ({why})",
        strategy="reviewer-v1",
    )
