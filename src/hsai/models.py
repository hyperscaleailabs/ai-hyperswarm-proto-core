"""Task -> model-size selection.

This is the harness's economic control surface: it decides whether a ticket
burns opus or haiku quota. The orchestrator asks :func:`select` which model to
run for a given task; the returned :class:`ModelChoice` is recorded on the PR
for auditability.

Selection is **pluggable**: a :class:`Strategy` is a named, deterministic
mapping from :class:`Task` to a tier, registered in :data:`STRATEGIES` and
chosen by the ``models.selection_strategy`` config key. That indirection is
what makes the decision *measurable* - :mod:`hsai.replay` grades any registered
strategy offline against the committed corpus in
``knowledge/eval/selection-corpus.jsonl`` instead of trusting a comment.

``heuristic-v1`` is the baseline and is deliberately frozen: its scoring
function and thresholds are preserved verbatim so the benchmark has a fixed
reference point that cannot be silently moved. Any successor must justify each
changed constant with a measured delta recorded under ``knowledge/eval/``.

References:
- microsoft/JARVIS: LLM controller routing sub-tasks to the right model
- SWE-agent: decisions graded against a fixed, committed instance set
- OpenBMB/ChatDev: activate cheaper agents wherever they suffice
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


@dataclass(frozen=True)
class TierDecision:
    """A strategy's verdict, before budget demotion and tier fallback."""

    tier: str
    why: str
    score: int


class Strategy:
    """A named, deterministic mapping from a :class:`Task` to a tier.

    Deterministic and side-effect-free by contract: a strategy may read only
    the task and the configured tiers, never the network or a model. That is
    what lets ``hsai replay`` grade it over a recorded corpus offline.
    """

    name = ""

    def decide(self, task: Task, cfg: CoreConfig) -> TierDecision:
        raise NotImplementedError


class UnknownStrategyError(ValueError):
    """Raised when a configured strategy name is not registered."""


# The frozen reference point every other strategy is measured against.
BASELINE = "heuristic-v1"

STRATEGIES: dict[str, Strategy] = {}


def register(strategy: Strategy) -> Strategy:
    """Add ``strategy`` to the registry (also usable as a class decorator)."""
    if isinstance(strategy, type):
        strategy = strategy()
    if not strategy.name:
        raise ValueError("a strategy must declare a name")
    STRATEGIES[strategy.name] = strategy
    return strategy


def get_strategy(name: str | None) -> Strategy:
    """Look up a registered strategy by name."""
    key = name or BASELINE
    try:
        return STRATEGIES[key]
    except KeyError:
        known = ", ".join(sorted(STRATEGIES)) or "(none)"
        raise UnknownStrategyError(f"unknown selection strategy {key!r}; known: {known}") from None


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


@register
class HeuristicV1(Strategy):
    """The original hand-tuned heuristic - the frozen benchmark baseline.

    Thresholds as originally shipped:
    - Heavy (>= 5): Architecture, migrations, hard bugs, large refactors
    - Light (<= -3): Docs, formatting, trivial edits, chores
    - Standard: Everything else (features, small bugfixes, simple refactors)

    Nothing here may change. It is the fixed point ``hsai replay`` measures
    successors against; a test asserts its per-task verdicts over the committed
    corpus are byte-for-byte what this refactor inherited.
    """

    name = BASELINE

    def decide(self, task: Task, cfg: CoreConfig) -> TierDecision:
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

        return TierDecision(tier=tier, why=why, score=score)


# --- heuristic-v2 -------------------------------------------------------------
# Every rule below exists because a specific instance in
# knowledge/eval/selection-corpus.jsonl showed heuristic-v1 getting it wrong.
# The measured delta is recorded in
# knowledge/eval/2026-08-04-selection-strategy-v1-vs-v2.md and asserted by
# tests/test_replay.py, so none of this can drift unnoticed.

_PREFIX_RE = re.compile(r"^([a-z]+)(?:\([^)]*\))?\s*:")

# Prefixes the loop itself prepends; strip them to reach the ticket's own type.
_LOOP_PREFIXES = ("implement", "improve", "heal")
# Prose work. The subject matter may be security or architecture; the diff is words.
_DOCS_PREFIXES = ("doc", "docs")
# Prefixes that promise new behaviour. No keyword may route these light.
_BUILD_PREFIXES = ("feat", "feature", "implement", "skill")
# Operations whose diff is fully determined by the request - nothing left to decide.
# Deliberately excludes bare "format": `--format` is a flag on a real feature.
_MECHANICAL_OPS = (
    "typo", "whitespace", "reformat", "formatting", "bump", "rename", "reindex",
)
# Failure modes that are expensive to get subtly wrong. Paired with a heal kind -
# the code is already misbehaving - they buy the heavy tier outright, whatever
# the file count says. "flaky" is deliberately absent: it is test hygiene, not a
# product invariant.
_FRAGILE = (
    "race condition", "deadlock", "concurrenc", "parallel", "intermittent",
    "under load", "simultaneous", "security", "secret", "token leak",
    "data loss", "corrupt",
)


def _title_prefix(title: str) -> str:
    """The conventional-commit type of a ticket title (``docs``, ``feat``, ...).

    Tolerates one loop-added wrapper, so ``implement: docs: ...`` reports
    ``docs`` - the ticket's own type, not the orchestrator's verb.
    """
    text = title.strip().lower()
    for _ in range(2):
        m = _PREFIX_RE.match(text)
        if not m:
            return ""
        kind = m.group(1)
        if kind in _LOOP_PREFIXES:
            text = text[m.end():].strip()
            continue
        return kind
    return ""


@register
class HeuristicV2(Strategy):
    """Successor to v1, calibrated against the committed corpus.

    Four changes, each answerable to instances v1 misroutes:

    1. **Correctness before economy.** A migration, or a heal touching a
       fragile invariant (concurrency, secrets, data loss), is heavy no matter
       how few files it touches. v1 under-provisioned every one of these
       because its only route to heavy was accumulating keywords or counting
       files, and a three-file secrets fix does neither.
    2. **A docs prefix is decided by the prefix, not the nouns.** "document the
       security model" is prose about security, not security work; v1 scored
       the nouns and paid mid-tier for a paragraph.
    3. **Mechanical operations are named explicitly.** A rename across nine
       files is nine files of nothing; v1's file-count bucket pushed it the
       wrong way. A ``chore:`` prefix is not itself an operation - the incident
       that produced a code-free diff was labelled ``chore:``.
    4. **The light tier is opt-in, never a residue.** v1 reached light by
       keyword arithmetic falling below -3, which is how a feature ticket
       mentioning ``--format`` and the README bought a haiku worker. v2 routes
       light only on a positive signal from (2) or (3).

    The heavy threshold, the keyword weights, and :func:`_score` itself are
    inherited from v1 unchanged - the delta is in the routing rules, so the
    comparison isolates them.
    """

    name = "heuristic-v2"

    def decide(self, task: Task, cfg: CoreConfig) -> TierDecision:
        score = _score(task)
        text = f"{task.title}\n{task.body}\n{' '.join(task.labels)}".lower()
        prefix = _title_prefix(task.title)

        # The planner's sizing outranks everything: it read the whole ticket.
        if "size:L" in task.labels:
            return TierDecision("heavy", "size:L label - large synthesized change", score)

        # (1) Correctness before economy.
        if "migration" in text:
            return TierDecision(
                "heavy", "migration - a compatibility window that must not be got wrong", score
            )
        if task.kind == "heal":
            fragile = next((w for w in _FRAGILE if w in text), "")
            if fragile:
                return TierDecision(
                    "heavy", f"heal touching a fragile invariant ({fragile})", score
                )

        # (2) A docs prefix means prose, whatever the nouns.
        if prefix in _DOCS_PREFIXES:
            return TierDecision("light", f"{prefix}-prefixed ticket - prose, not code", score)

        # (3) Named mechanical operations - but never on a build-shaped title.
        if prefix not in _BUILD_PREFIXES:
            op = next((w for w in _MECHANICAL_OPS if w in task.title.lower()), "")
            if op:
                return TierDecision("light", f"mechanical operation ({op})", score)

        if "size:M" in task.labels:
            return TierDecision(
                cfg.default_tier, "size:M label - substantial synthesized change", score
            )
        if score >= 5:
            return TierDecision(
                "heavy", "high-complexity signals (architecture, hard bug, large refactor)", score
            )
        # (4) Light is opt-in. Everything unclaimed lands on the default tier.
        return TierDecision(cfg.default_tier, "no mechanical or docs signal; default tier", score)


def select(
    task: Task, cfg: CoreConfig, *, demote: bool = False, strategy: str | None = None
) -> ModelChoice:
    """Pick a tier for ``task`` and resolve it to a concrete model alias.

    The scoring itself lives in a registered :class:`Strategy` (``strategy``,
    defaulting to the configured ``models.selection_strategy``). This function
    owns only the two concerns every strategy shares: the budget demotion and
    the fallback for an unconfigured tier.

    ``demote`` biases the choice one tier cheaper (heavy->standard->light). The
    budget gate sets it on a soft breach so a block that is burning quota keeps
    making progress on cheaper tiers instead of halting outright.
    """
    strat = get_strategy(strategy or cfg.selection_strategy)
    decision = strat.decide(task, cfg)
    tier, why, score = decision.tier, decision.why, decision.score

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
    return ModelChoice(tier=tier, model=model, rationale=rationale, strategy=strat.name)
