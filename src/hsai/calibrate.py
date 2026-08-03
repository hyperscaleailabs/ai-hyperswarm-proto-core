"""Close the loop from the quota ledger back into model selection.

The two halves already existed and never touched: :mod:`hsai.models` routes each
task to a tier, and :mod:`hsai.ledger` records what that tier cost. This module
joins them - ledger records to lesson outcomes on ``(iteration, ticket)`` - and
answers the question the routing heuristic could never answer about itself:
*does sending this class of work to the heavy tier actually buy a better
outcome, and what does it cost?*

The pipeline:

1. **Replay** - reconstruct each historical :class:`~hsai.models.Task` and
   re-route it through :func:`hsai.models.decide_tier` under the *current*
   policy, so a proposal is judged by the same code that will enforce it.
2. **Measure** - per-tier success rate, median wall-clock and retry rate, plus a
   regret estimate: heavy runs a one-step cheaper policy would have satisfied,
   and light/standard runs that failed and were retried heavier.
3. **Propose** - a bounded search over threshold and weight deltas that
   maximises estimated success subject to *never increasing heavy-tier share*,
   gated by a min-sample floor and clamped to one step per calibration.
4. **Report** - a dated, Obsidian-ready note under ``knowledge/reports/``. When
   the data is thin it says so - ``insufficient data: policy unchanged`` -
   rather than fitting a number to noise.

The honest caveats are stated in the report, not hidden here: outcomes are
observational (we never re-ran a ticket at another tier), so the estimator
assumes a tier's observed success rate transfers to work rerouted into it. That
assumption is exactly why the clamp, the heavy-share constraint and the
human-reviewable JSON diff exist.

Synthesis: microsoft/JARVIS (routing quality must be benchmarked, not asserted -
TaskBench/EasyTool), assafelovic/gpt-researcher (cost accounting as a tracked
metric), OpenBMB/ChatDev (activate cheaper agents without losing task success),
run-llama/llama_index (committed numeric thresholds, reviewed like code).
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from itertools import product
from pathlib import Path

from . import policy as policy_mod
from .config import CoreConfig
from .knowledge import KnowledgeBase, LessonRecord
from .ledger import LedgerRecord, ledger_path, read_records
from .models import LIGHT_MAX_FILES, Task, decide_tier
from .policy import SelectionPolicy

# Tiers ordered cheap -> expensive (mirrors ledger._TIER_ORDER).
TIERS = ("light", "standard", "heavy")

# The verdict the report must print when the corpus is too small to learn from.
INSUFFICIENT = "insufficient data: policy unchanged"

# Defaults for the calibration gates; overridden from core.yaml's `calibration`.
DEFAULT_CLAMP_STEP = 1          # a threshold may move at most this far, per run
DEFAULT_MIN_SAMPLES_PER_TIER = 8
DEFAULT_MIN_TOTAL_SAMPLES = 20
DEFAULT_MIN_GAIN = 0.02         # ignore improvements smaller than this (noise)
DEFAULT_REPORT_DIR = "knowledge/reports"

# The only dimensions a calibration may move, and the tier whose sample count
# gates each one. Everything else in the policy - and every invariant in
# models.py - is out of reach.
TUNABLE_DIMENSIONS: tuple[tuple[str, str], ...] = (
    ("heavy_threshold", "heavy"),
    ("heavy_signal_weight", "heavy"),
    ("light_threshold", "light"),
    ("light_signal_weight", "light"),
)


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# --- the joined corpus --------------------------------------------------------
@dataclass(frozen=True)
class Sample:
    """One historical iteration: what it cost (ledger) and how it went (lesson)."""

    iteration: int
    ticket: int | None
    kind: str
    tier: str
    model: str
    wall_clock_seconds: float
    attempts: int
    outcome: str          # ledger outcome: merged | recovered | incomplete | ...
    lesson_outcome: str   # lesson outcome: pass | fail
    title: str
    body: str = ""
    labels: tuple[str, ...] = ()

    @property
    def success(self) -> bool:
        """Merged under a green build - the only outcome that counts as a win."""
        return self.outcome == "merged" and self.lesson_outcome == "pass"

    @property
    def retried(self) -> bool:
        return self.attempts > 1

    def task(self) -> Task:
        """Reconstruct the Task the orchestrator routed.

        ``est_files`` is deliberately left at its default: the orchestrator
        never sets it either, so the replay matches production exactly.
        """
        return Task(kind=self.kind, title=self.title, body=self.body, labels=self.labels)


def _ticket_title(lesson_title: str, kind: str) -> str:
    """Strip the ``"{kind}: "`` prefix the loop puts on every lesson title."""
    prefix = f"{kind}: "
    return lesson_title[len(prefix):] if lesson_title.startswith(prefix) else lesson_title


@dataclass
class Corpus:
    """The joined ledger/lesson corpus, plus what could not be joined."""

    samples: list[Sample] = field(default_factory=list)
    unjoined_records: int = 0   # ledger records with no matching lesson
    lessons: int = 0


def join_samples(
    records: list[LedgerRecord],
    lessons: list[LessonRecord],
    *,
    issue_lookup=None,
) -> Corpus:
    """Join ledger records to lesson outcomes on ``(iteration, ticket)``.

    ``issue_lookup(ticket) -> (title, body, labels)`` optionally restores the
    original issue text; without it the replay uses the lesson's title, which is
    what the report's replay-agreement figure measures.
    """
    by_key = {(r.iteration, r.ticket): r for r in lessons}
    corpus = Corpus(lessons=len(lessons))
    for rec in records:
        lesson = by_key.get((rec.iteration, rec.ticket))
        if lesson is None:
            corpus.unjoined_records += 1
            continue
        title = _ticket_title(lesson.title, rec.kind)
        body, labels = "", ()
        if issue_lookup is not None and rec.ticket is not None:
            found = issue_lookup(rec.ticket)
            if found:
                title, body, labels = found[0] or title, found[1], tuple(found[2])
        corpus.samples.append(
            Sample(
                iteration=rec.iteration,
                ticket=rec.ticket,
                kind=rec.kind,
                tier=rec.tier,
                model=rec.model,
                wall_clock_seconds=rec.wall_clock_seconds,
                attempts=rec.attempts,
                outcome=rec.outcome,
                lesson_outcome=lesson.outcome,
                title=title,
                body=body,
                labels=labels,
            )
        )
    return corpus


# --- per-tier confusion view --------------------------------------------------
@dataclass(frozen=True)
class TierStats:
    """What one tier actually delivered, and what it charged for it."""

    tier: str
    n: int = 0
    successes: int = 0
    retries: int = 0
    median_seconds: float = 0.0
    total_seconds: float = 0.0

    @property
    def success_rate(self) -> float:
        return self.successes / self.n if self.n else 0.0

    @property
    def retry_rate(self) -> float:
        return self.retries / self.n if self.n else 0.0


def tier_stats(samples: list[Sample]) -> dict[str, TierStats]:
    """Fold the corpus into one :class:`TierStats` per tier (empty tiers included)."""
    stats: dict[str, TierStats] = {}
    for tier in TIERS:
        rows = [s for s in samples if s.tier == tier]
        seconds = [s.wall_clock_seconds for s in rows]
        stats[tier] = TierStats(
            tier=tier,
            n=len(rows),
            successes=sum(1 for s in rows if s.success),
            retries=sum(1 for s in rows if s.retried),
            median_seconds=round(statistics.median(seconds), 1) if seconds else 0.0,
            total_seconds=round(sum(seconds), 1),
        )
    return stats


@dataclass(frozen=True)
class Regret:
    """Where routing plausibly cost more, or delivered less, than it needed to."""

    heavy_overspend: int = 0            # heavy wins one cheaper step would have kept
    heavy_overspend_seconds: float = 0.0
    under_routed: int = 0               # cheap runs that failed and were retried

    def summary(self) -> str:
        return (
            f"{self.heavy_overspend} heavy run(s) "
            f"({self.heavy_overspend_seconds:.0f}s) a one-step-stricter policy would "
            f"have routed cheaper and that still succeeded first time; "
            f"{self.under_routed} light/standard run(s) that failed and were retried."
        )


def estimate_regret(
    samples: list[Sample],
    policy: SelectionPolicy,
    *,
    clamp_step: int = DEFAULT_CLAMP_STEP,
    default_tier: str = "standard",
) -> Regret:
    """Estimate both directions of routing regret.

    *Overspend*: a heavy run that succeeded on its first attempt and which a
    one-step-higher ``heavy_threshold`` would have routed cheaper - i.e. exactly
    the runs a bounded calibration could reclaim. Size-labelled work is immune
    by construction (the label override is an invariant), so it never counts.

    *Under-routing*: a light/standard run that did not succeed and was either
    retried (``attempts > 1``) or followed by a heavier run on the same ticket.
    """
    stricter = replace(policy, heavy_threshold=policy.heavy_threshold + clamp_step)
    later_heavier: dict[int, bool] = {}
    for s in samples:
        if s.ticket is None:
            continue
        heavier = [
            o for o in samples
            if o.ticket == s.ticket and o.iteration > s.iteration
            and TIERS.index(o.tier) > TIERS.index(s.tier)
        ]
        later_heavier[s.iteration] = bool(heavier)

    overspend = 0
    overspend_seconds = 0.0
    under = 0
    for s in samples:
        if s.tier == "heavy" and s.success and not s.retried:
            _, tier_after, _ = decide_tier(s.task(), stricter, default_tier=default_tier)
            if tier_after != "heavy":
                overspend += 1
                overspend_seconds += s.wall_clock_seconds
        elif s.tier in ("light", "standard") and not s.success:
            if s.retried or later_heavier.get(s.iteration):
                under += 1
    return Regret(
        heavy_overspend=overspend,
        heavy_overspend_seconds=round(overspend_seconds, 1),
        under_routed=under,
    )


# --- bounded policy search ----------------------------------------------------
@dataclass(frozen=True)
class Evaluation:
    """A candidate policy replayed over the corpus."""

    expected_success: float
    heavy_share: float
    assignments: dict[str, int] = field(default_factory=dict)


def evaluate_policy(
    policy: SelectionPolicy,
    samples: list[Sample],
    stats: dict[str, TierStats],
    *,
    default_tier: str = "standard",
) -> Evaluation:
    """Score a policy by replaying every sample through :func:`decide_tier`.

    Observational estimate: a sample rerouted to another tier is credited with
    that tier's observed success rate (the overall rate when a tier has no
    observations). Stated plainly in the report - it is why proposals are
    clamped rather than trusted.
    """
    if not samples:
        return Evaluation(expected_success=0.0, heavy_share=0.0, assignments={})
    overall = sum(1 for s in samples if s.success) / len(samples)
    assignments = dict.fromkeys(TIERS, 0)
    total = 0.0
    for s in samples:
        _, tier, _ = decide_tier(s.task(), policy, default_tier=default_tier)
        assignments[tier] = assignments.get(tier, 0) + 1
        st = stats.get(tier)
        total += st.success_rate if st and st.n else overall
    n = len(samples)
    return Evaluation(
        expected_success=total / n,
        heavy_share=assignments.get("heavy", 0) / n,
        assignments=assignments,
    )


def clamp_violations(
    old: SelectionPolicy, new: SelectionPolicy, *, step: int = DEFAULT_CLAMP_STEP
) -> list[str]:
    """Dimensions ``new`` moved further than ``step`` from ``old`` (empty = ok)."""
    bad: list[str] = []
    for dim, _tier in TUNABLE_DIMENSIONS:
        if abs(getattr(new, dim) - getattr(old, dim)) > step:
            bad.append(dim)
    # Nothing outside the tunable set may move either.
    for dim in ("heavy_signals", "light_signals", "file_buckets", "kind_weights",
                "narrow_docs_delta"):
        if getattr(new, dim) != getattr(old, dim):
            bad.append(dim)
    return bad


@dataclass(frozen=True)
class Proposal:
    """The calibrator's verdict: a bounded policy bump, or an explicit refusal."""

    changed: bool
    verdict: str
    policy: SelectionPolicy                 # what to use going forward
    deltas: tuple[str, ...] = ()
    frozen_dimensions: tuple[str, ...] = ()
    baseline: Evaluation | None = None
    proposed: Evaluation | None = None


def propose_policy(
    policy: SelectionPolicy,
    samples: list[Sample],
    *,
    min_samples_per_tier: int = DEFAULT_MIN_SAMPLES_PER_TIER,
    min_total_samples: int = DEFAULT_MIN_TOTAL_SAMPLES,
    clamp_step: int = DEFAULT_CLAMP_STEP,
    min_gain: float = DEFAULT_MIN_GAIN,
    default_tier: str = "standard",
    created: str = "",
) -> Proposal:
    """Search for a better policy, bounded on every side.

    Guarantees, all enforced here and covered by tests:

    - below ``min_total_samples``, or with every dimension under the per-tier
      floor, nothing changes and the verdict is :data:`INSUFFICIENT`;
    - a dimension whose gating tier has fewer than ``min_samples_per_tier``
      samples is frozen at its current value;
    - no dimension moves more than ``clamp_step`` in one calibration;
    - a candidate that would raise heavy-tier share is rejected outright.
    """
    stats = tier_stats(samples)
    all_dims = tuple(d for d, _ in TUNABLE_DIMENSIONS)

    if len(samples) < min_total_samples:
        return Proposal(
            changed=False,
            verdict=(
                f"{INSUFFICIENT} - {len(samples)} joined sample(s), "
                f"floor is {min_total_samples}"
            ),
            policy=policy,
            frozen_dimensions=all_dims,
        )

    frozen = tuple(
        dim for dim, tier in TUNABLE_DIMENSIONS
        if stats[tier].n < min_samples_per_tier
    )
    if len(frozen) == len(all_dims):
        thin = ", ".join(
            f"{t}={stats[t].n}" for t in sorted({tier for _, tier in TUNABLE_DIMENSIONS})
        )
        return Proposal(
            changed=False,
            verdict=(
                f"{INSUFFICIENT} - every dimension is below the per-tier floor of "
                f"{min_samples_per_tier} ({thin})"
            ),
            policy=policy,
            frozen_dimensions=frozen,
        )

    baseline = evaluate_policy(policy, samples, stats, default_tier=default_tier)
    options = [
        (0,) if dim in frozen else (-clamp_step, 0, clamp_step) for dim in all_dims
    ]

    best: tuple[tuple[float, float, int], SelectionPolicy, Evaluation, tuple[str, ...]] | None
    best = None
    for combo in product(*options):
        if not any(combo):
            continue
        candidate = replace(
            policy, **{dim: getattr(policy, dim) + delta for dim, delta in zip(all_dims, combo)}
        )
        # A candidate we would refuse to load is not a candidate.
        try:
            policy_mod.from_dict(policy_mod.to_dict(candidate))
        except policy_mod.PolicyError:
            continue
        if clamp_violations(policy, candidate, step=clamp_step):
            continue
        ev = evaluate_policy(candidate, samples, stats, default_tier=default_tier)
        # Hard constraint: never buy success with more heavy-tier quota.
        if ev.heavy_share > baseline.heavy_share + 1e-9:
            continue
        if ev.expected_success < baseline.expected_success + min_gain:
            continue
        key = (ev.expected_success, -ev.heavy_share, -sum(abs(c) for c in combo))
        if best is None or key > best[0]:
            deltas = tuple(
                f"{dim}: {getattr(policy, dim)} -> {getattr(candidate, dim)}"
                for dim, delta in zip(all_dims, combo) if delta
            )
            best = (key, candidate, ev, deltas)

    if best is None:
        return Proposal(
            changed=False,
            verdict=(
                "no candidate improved the estimated success rate by "
                f"{min_gain:.0%} without increasing heavy-tier share: policy unchanged"
            ),
            policy=policy,
            frozen_dimensions=frozen,
            baseline=baseline,
        )

    _, candidate, ev, deltas = best
    proposed = replace(
        candidate,
        version=policy.version + 1,
        notes=(
            f"calibrated {created or _today()} from {len(samples)} ledger/lesson "
            f"sample(s): {'; '.join(deltas)}. Estimated success "
            f"{baseline.expected_success:.0%} -> {ev.expected_success:.0%}, heavy share "
            f"{baseline.heavy_share:.0%} -> {ev.heavy_share:.0%}."
        ),
    )
    return Proposal(
        changed=True,
        verdict=(
            f"policy v{policy.version} -> v{proposed.version}: {'; '.join(deltas)}"
        ),
        policy=proposed,
        deltas=deltas,
        frozen_dimensions=frozen,
        baseline=baseline,
        proposed=ev,
    )


# --- report -------------------------------------------------------------------
@dataclass
class CalibrationReport:
    """Everything the architect needs to accept or reject a proposal."""

    created: str
    policy: SelectionPolicy
    corpus: Corpus
    stats: dict[str, TierStats]
    regret: Regret
    proposal: Proposal
    replay_agreement: float
    ledger_file: str
    min_samples_per_tier: int
    min_total_samples: int
    clamp_step: int

    def note_name(self) -> str:
        return f"selection-calibration-{self.created}"

    def governance_note(self) -> str:
        """One line for the block's governance PR and review brief."""
        if self.proposal.changed:
            return (
                f"selection calibration: proposed {self.proposal.verdict} "
                f"from {len(self.corpus.samples)} sample(s) - see "
                f"`{DEFAULT_REPORT_DIR}/{self.note_name()}.md`"
            )
        return (
            f"selection calibration: declined to tune - {self.proposal.verdict} "
            f"(policy v{self.policy.version} kept)"
        )


def replay_agreement(samples: list[Sample], policy: SelectionPolicy, *, default_tier: str) -> float:
    """Share of samples whose replayed tier matches the tier that actually ran."""
    if not samples:
        return 0.0
    agree = sum(
        1 for s in samples
        if decide_tier(s.task(), policy, default_tier=default_tier)[1] == s.tier
    )
    return agree / len(samples)


def _stats_table(stats: dict[str, TierStats]) -> str:
    rows = []
    for tier in TIERS:
        st = stats[tier]
        rows.append(
            f"| {tier} | {st.n} | {st.success_rate:.0%} | {st.median_seconds:.0f}s | "
            f"{st.retry_rate:.0%} | {st.total_seconds:.0f}s |"
        )
    return "\n".join(rows)


def render_report(report: CalibrationReport) -> str:
    """Render the dated calibration note (Obsidian-ready: frontmatter + links)."""
    p = report.policy
    samples = report.corpus.samples
    prop = report.proposal
    if prop.changed:
        verdict_block = (
            f"**Proposed: {prop.verdict}**\n\n"
            + "\n".join(f"- `{d}`" for d in prop.deltas)
            + (
                f"\n\nEstimated success {prop.baseline.expected_success:.0%} -> "
                f"{prop.proposed.expected_success:.0%}; heavy-tier share "
                f"{prop.baseline.heavy_share:.0%} -> {prop.proposed.heavy_share:.0%} "
                "(never increased, by construction)."
                if prop.baseline and prop.proposed else ""
            )
        )
    else:
        verdict_block = f"**{prop.verdict}**"
    frozen = ", ".join(f"`{d}`" for d in prop.frozen_dimensions) or "_none - all dimensions eligible_"
    return f"""---
tags:
  - report
  - calibration
  - model-selection
created: {report.created}
---

# Selection calibration - {report.created}

> Part of [[Knowledge Base MOC]] - produced by `hsai calibrate`

Joins the quota ledger (`{report.ledger_file}`) to [[Lessons MOC|lesson]]
outcomes on `(iteration, ticket)` and replays every historical task through the
active selection policy (**v{p.version}**, `heavy_threshold={p.heavy_threshold}`,
`light_threshold={p.light_threshold}`).

## Sample
| quantity | value |
| --- | --- |
| ledger records joined to a lesson | {len(samples)} |
| ledger records with no lesson | {report.corpus.unjoined_records} |
| lessons on disk | {report.corpus.lessons} |
| replay agreement with the tier that ran | {report.replay_agreement:.0%} |
| min samples per tier (floor) | {report.min_samples_per_tier} |
| min samples total (floor) | {report.min_total_samples} |
| max threshold move per calibration (clamp) | {report.clamp_step} |

## Per-tier outcomes
| tier | n | success | median wall-clock | retry rate | total wall-clock |
| --- | --- | --- | --- | --- | --- |
{_stats_table(report.stats)}

## Regret estimate
{report.regret.summary()}

## Verdict
{verdict_block}

Frozen dimensions (gating tier below the sample floor): {frozen}

## Non-tunable invariants
These are enforced in `hsai/models.py`, outside the policy file, and no
calibration can move them:

- `size:L` routes heavy; `size:M` routes the configured default tier.
- Feature-shaped work never routes light (light needs
  `est_files <= {LIGHT_MAX_FILES}` on top of the score threshold).
- The budget gate's soft-breach demotion (heavy -> standard -> light) is applied
  after the policy decides.

## How to read this
Outcomes here are observational: no ticket was ever re-run at a second tier, so
a rerouted task is credited with the observed success rate of the tier it moves
into. That is a weak estimator, which is why a proposal may never increase
heavy-tier share, may move a threshold by at most {report.clamp_step} step per
calibration, and lands as a reviewable `.ai-swarm/selection-policy.json` diff in
the block's governance PR rather than being applied silently.
"""


# --- entry point --------------------------------------------------------------
@dataclass
class CalibrationResult:
    report: CalibrationReport
    report_path: Path
    policy_path: Path | None = None  # set when a proposal was written to disk


def _setting(cfg: CoreConfig, key: str, default):
    return (cfg.calibration or {}).get(key, default)


def calibrate(
    cfg: CoreConfig,
    *,
    repo_root: str | Path,
    created: str | None = None,
) -> CalibrationReport:
    """Build the calibration report for ``repo_root`` (pure: writes nothing)."""
    root = Path(repo_root)
    stamp = created or _today()
    ledger_file = ledger_path(cfg, root)
    ppath = policy_mod.policy_path(cfg, root)
    active = policy_mod.read_policy(ppath) if ppath.exists() else policy_mod.default_policy()

    corpus = join_samples(
        read_records(ledger_file), KnowledgeBase.from_config(cfg, root).read_lessons()
    )
    stats = tier_stats(corpus.samples)
    clamp_step = int(_setting(cfg, "clamp_step", DEFAULT_CLAMP_STEP))
    regret = estimate_regret(
        corpus.samples, active, clamp_step=clamp_step, default_tier=cfg.default_tier
    )
    proposal = propose_policy(
        active,
        corpus.samples,
        min_samples_per_tier=int(
            _setting(cfg, "min_samples_per_tier", DEFAULT_MIN_SAMPLES_PER_TIER)
        ),
        min_total_samples=int(_setting(cfg, "min_total_samples", DEFAULT_MIN_TOTAL_SAMPLES)),
        clamp_step=clamp_step,
        min_gain=float(_setting(cfg, "min_gain", DEFAULT_MIN_GAIN)),
        default_tier=cfg.default_tier,
        created=stamp,
    )
    return CalibrationReport(
        created=stamp,
        policy=active,
        corpus=corpus,
        stats=stats,
        regret=regret,
        proposal=proposal,
        replay_agreement=replay_agreement(
            corpus.samples, active, default_tier=cfg.default_tier
        ),
        ledger_file=str(ledger_file.relative_to(root)) if ledger_file.is_relative_to(root)
        else str(ledger_file),
        min_samples_per_tier=int(
            _setting(cfg, "min_samples_per_tier", DEFAULT_MIN_SAMPLES_PER_TIER)
        ),
        min_total_samples=int(_setting(cfg, "min_total_samples", DEFAULT_MIN_TOTAL_SAMPLES)),
        clamp_step=clamp_step,
    )


def run_calibration(
    cfg: CoreConfig,
    *,
    repo_root: str | Path,
    apply: bool = True,
    created: str | None = None,
) -> CalibrationResult:
    """Calibrate, write the dated report, and (optionally) the proposed policy.

    Writing the policy is deliberately *not* a merge: it lands as a diff in the
    block's governance PR, where a human reviews it like any other change.
    """
    root = Path(repo_root)
    report = calibrate(cfg, repo_root=root, created=created)

    report_dir = root / _setting(cfg, "report_dir", DEFAULT_REPORT_DIR)
    report_dir.mkdir(parents=True, exist_ok=True)
    path = report_dir / f"{report.note_name()}.md"
    path.write_text(render_report(report), encoding="utf-8")

    written: Path | None = None
    if apply and report.proposal.changed:
        written = policy_mod.write_policy(
            policy_mod.policy_path(cfg, root), report.proposal.policy
        )
    return CalibrationResult(report=report, report_path=path, policy_path=written)
