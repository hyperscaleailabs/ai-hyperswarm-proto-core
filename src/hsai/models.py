"""Task -> model-size selection.

This is a first-class, deliberately-learnable capability. The orchestrator asks
:func:`select` which model to run for a given task; the returned
:class:`ModelChoice` is recorded on the PR (and in the quota ledger) for
auditability.

Two strategies live here, and :func:`select` always computes **both**:

- ``heuristic-v1`` - the hand-tuned thresholds this loop started with
  (heavy at score >= 5, light at score <= -3). Frozen: no evidence revises it.
- ``heuristic-v2`` - the *same* scoring function read against thresholds fitted
  from the quota ledger by :mod:`hsai.calibrate` and pinned into
  ``models.calibration`` in ``.ai-swarm/core.yaml``. With no calibration block
  present (or one below the sample floor) it falls back to the v1 thresholds
  and is therefore identical to v1.

Only the strategy named by ``models.selection_strategy`` is *active*; the other
runs in **shadow mode** - its tier is computed, recorded on
:attr:`ModelChoice.shadow_tier`, written to the ledger and printed on the PR,
but it never routes anything. A learned artifact only takes effect when a human
edits the pinned strategy, which is the whole point: the fit is measured,
versioned and reviewable before it can spend a single token differently.

The scoring heuristic combines:
- Keyword signals (architecture, security, docs, etc.)
- Task structure (files touched, kind: heal/implement/improve)
- Size labels set by the synthesis planner

References:
- microsoft/JARVIS: LLM controller routing sub-tasks to the right model
- OpenBMB/ChatDev: an orchestrator that learns routing from outcomes
- run-llama/llama_index: a measured number becomes a pinned, versioned gate
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

# Selection strategies. Exactly one is active (``models.selection_strategy``);
# the other is computed in shadow mode on every call.
V1 = "heuristic-v1"
V2 = "heuristic-v2"
STRATEGIES = (V1, V2)

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


@dataclass(frozen=True)
class Thresholds:
    """The two integers that turn a complexity score into a tier.

    ``heavy``: score at or above this routes heavy. ``light``: score at or
    below this (on a narrow change) routes light. Everything between is the
    configured default tier.
    """

    heavy: int
    light: int

    def label(self) -> str:
        return f"heavy>={self.heavy}, light<={self.light}"


# The hand-tuned thresholds this loop shipped with. heuristic-v1 is frozen on
# these by definition, and heuristic-v2 falls back to them when there is no
# usable calibration - so "no calibration block" means "no behaviour change".
V1_THRESHOLDS = Thresholds(heavy=5, light=-3)


@dataclass(frozen=True)
class RoutingFeatures:
    """Everything the router looked at, so a ledger record is a labelled example.

    Written to :class:`hsai.ledger.LedgerRecord` alongside the outcome, which is
    what lets :mod:`hsai.calibrate` replay a candidate threshold pair over real
    history instead of guessing.
    """

    complexity_score: int
    est_files: int
    heavy_signals: int
    light_signals: int
    size_label: str  # "L" | "M" | "" (no size label on the ticket)
    kind: str


@dataclass(frozen=True)
class ModelChoice:
    tier: str
    model: str
    rationale: str
    strategy: str = V1
    # Shadow evaluation: what the *other* strategy would have picked. Recorded
    # and printed, never routed - see the module docstring.
    shadow_tier: str = ""
    shadow_strategy: str = ""
    features: RoutingFeatures | None = None
    demoted: bool = False

    @property
    def shadow_disagrees(self) -> bool:
        return bool(self.shadow_tier) and self.shadow_tier != self.tier


def _signal_counts(text: str) -> tuple[int, int]:
    """How many heavy / light keyword signals ``text`` matched."""
    heavy = sum(1 for w in _HEAVY_SIGNALS if w in text)
    light = sum(1 for w in _LIGHT_SIGNALS if w in text)
    return heavy, light


def size_label(labels: tuple[str, ...]) -> str:
    """The planner's size label (``L``/``M``), or ``""`` if the ticket has none."""
    for size in ("L", "M"):
        if f"size:{size}" in labels:
            return size
    return ""


def features(task: Task) -> RoutingFeatures:
    """Complexity score (positive => heavier, negative => lighter) + its inputs.

    Combines keyword signals, structural signals, and task kind into a
    unified score. Calibrated to distinguish light/standard/heavy across
    a range of task types.

    Score ranges (against :data:`V1_THRESHOLDS`):
    - [-inf, -3]: Light tasks (docs, trivial edits, formatting)
    - (-3, 5): Standard tasks (features, small bugfixes, simple refactors)
    - [5, inf]: Heavy tasks (architecture, hard bugs, migrations)
    """
    text = f"{task.title}\n{task.body}\n{' '.join(task.labels)}".lower()
    heavy_signals, light_signals = _signal_counts(text)

    # Keyword-based signals: moderate weight to allow structural signals
    # to shift the tier in edge cases.
    score = 2 * heavy_signals - 2 * light_signals

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

    return RoutingFeatures(
        complexity_score=score,
        est_files=task.est_files,
        heavy_signals=heavy_signals,
        light_signals=light_signals,
        size_label=size_label(task.labels),
        kind=task.kind,
    )


def tier_for(
    feats: RoutingFeatures, thresholds: Thresholds, default_tier: str
) -> tuple[str, str]:
    """Apply ``thresholds`` to ``feats``: the whole routing rule, pure.

    Shared verbatim by both strategies (they differ only in ``thresholds``) and
    replayed over ledger history by :mod:`hsai.calibrate`, so a fitted threshold
    pair is evaluated against exactly the rule it will later drive.
    """
    # Size labels (set by the synthesis planner) override keyword scoring:
    # substantial tickets must never fall to the light tier.
    if feats.size_label == "L":
        return "heavy", "size:L label - large synthesized change"
    if feats.size_label == "M":
        return default_tier, "size:M label - substantial synthesized change"
    if feats.complexity_score >= thresholds.heavy:
        return "heavy", "high-complexity signals (architecture, hard bug, large refactor)"
    if feats.complexity_score <= thresholds.light and feats.est_files <= 2:
        # Light tier is reserved for genuinely mechanical, narrow edits. A
        # haiku worker once "completed" a feature ticket with a code-free
        # diff - broad or feature-shaped work never routes light again.
        return "light", "low-complexity signals (docs, format, mechanical edit)"
    return default_tier, "no strong signal; using default tier"


def active_strategy(cfg: CoreConfig) -> str:
    """Which strategy actually routes work (``models.selection_strategy``).

    Unknown or missing values pin v1: an unrecognised name must never silently
    hand routing to something nobody reviewed.
    """
    name = str(cfg.models.get("selection_strategy") or V1)
    return name if name in STRATEGIES else V1


def calibrated_thresholds(cfg: CoreConfig) -> tuple[Thresholds | None, str]:
    """Thresholds pinned under ``models.calibration``, plus why they were/weren't used.

    Returns ``(None, reason)`` when the block is absent or malformed - the
    caller then falls back to :data:`V1_THRESHOLDS`, so heuristic-v2 degrades to
    heuristic-v1 rather than to a guess.
    """
    block = cfg.models.get("calibration")
    if not isinstance(block, dict):
        return None, "no models.calibration block; falling back to v1 thresholds"
    raw = block.get("thresholds")
    if not isinstance(raw, dict):
        return None, "models.calibration has no thresholds; falling back to v1"
    try:
        thresholds = Thresholds(heavy=int(raw["heavy"]), light=int(raw["light"]))
    except (KeyError, TypeError, ValueError):
        return None, "models.calibration.thresholds is malformed; falling back to v1"
    version = block.get("version", "?")
    n = block.get("sample_size", "?")
    return thresholds, f"calibration v{version} ({thresholds.label()}, n={n})"


def _resolve(
    feats: RoutingFeatures, thresholds: Thresholds, cfg: CoreConfig, *, demote: bool
) -> tuple[str, str, bool]:
    """``tier_for`` + the soft-budget demotion + the unconfigured-tier fallback."""
    tier, why = tier_for(feats, thresholds, cfg.default_tier)
    demoted = False

    # Soft budget breach: bias one tier cheaper so the block keeps progressing
    # without burning more heavy-tier quota.
    if demote:
        from .ledger import demote_tier

        cheaper = demote_tier(tier)
        if cheaper != tier:
            why = f"{why}; demoted {tier}->{cheaper} under soft budget breach"
            tier, demoted = cheaper, True

    # Fall back gracefully if a tier is not configured.
    if tier not in cfg.tiers:
        tier = cfg.default_tier
    return tier, why, demoted


def select(task: Task, cfg: CoreConfig, *, demote: bool = False) -> ModelChoice:
    """Pick a tier for ``task`` and resolve it to a concrete model alias.

    Both strategies are evaluated on every call. The tier returned is the one
    the *config-pinned* strategy chose (``models.selection_strategy``, default
    ``heuristic-v1``); the other strategy's tier rides along on
    :attr:`ModelChoice.shadow_tier` purely as evidence. With no
    ``models.calibration`` block in core.yaml the two are identical, so this
    changes no routing decision at all until a human pins a fitted calibration.

    ``demote`` biases the choice one tier cheaper (heavy->standard->light) - for
    both strategies, since the budget gate is orthogonal to which one is active.
    The gate sets it on a soft breach so a block that is burning quota keeps
    making progress on cheaper tiers instead of halting outright.
    """
    feats = features(task)
    calibration, _ = calibrated_thresholds(cfg)
    resolved = {
        V1: _resolve(feats, V1_THRESHOLDS, cfg, demote=demote),
        V2: _resolve(feats, calibration or V1_THRESHOLDS, cfg, demote=demote),
    }

    active = active_strategy(cfg)
    shadow = V2 if active == V1 else V1
    tier, why, demoted = resolved[active]

    model = cfg.tiers[tier].model
    rationale = f"score={feats.complexity_score} -> {tier} ({why})"
    return ModelChoice(
        tier=tier,
        model=model,
        rationale=rationale,
        strategy=active,
        shadow_tier=resolved[shadow][0],
        shadow_strategy=shadow,
        features=feats,
        demoted=demoted,
    )


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
