"""Outcome-calibrated model routing: fit the router's thresholds from the ledger.

``src/hsai/models.py`` picked a tier from hand-tuned constants that no evidence
ever revised, while ``knowledge/ledger/iterations.jsonl`` quietly recorded the
tier, the wall-clock, the attempts and the outcome of every iteration. Nothing
read that back into the decision, so model-size selection - declared a learnable
skill in core.yaml - was in fact frozen.

This module closes that loop, and does so *cautiously*:

- :func:`tier_stats` folds records into the observed success rate
  (``outcome == "merged"``) and mean wall-clock per tier.
- :func:`fit` searches a small integer grid of ``(heavy, light)`` thresholds,
  replaying each labelled record's routing features through the very rule
  :func:`hsai.models.tier_for` will later apply, and maximises
  **merged-outcomes-per-heavy-iteration** subject to a floor on the overall
  success rate. Below :data:`DEFAULT_MIN_SAMPLES` labelled records it returns an
  explicit insufficient-data result carrying **no thresholds at all** - never a
  fitted guess off three data points.
- :func:`disagreement` measures how often the shadow strategy would have routed
  differently from the active one, which is the number that tells a human
  whether promoting the fit is a big change or a rounding error.

Nothing here has a side effect on routing. A fit becomes a recommendation in a
committed article (:func:`write_article`) and in the block review brief; it only
routes work once a human pins it into ``models.calibration`` and flips
``models.selection_strategy``.

Pure and dependency-free: no I/O beyond writing the article the CLI asks for, no
network, no quota.

Synthesis: microsoft/JARVIS (route each sub-task to the model that fits it),
OpenBMB/ChatDev (an orchestrator learned from outcomes to cut compute while
keeping quality), assafelovic/gpt-researcher (an explicit, auditable cost
accounting the system can reason about), run-llama/llama_index (turn a measured
number into a pinned, versioned gate rather than a vibe).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import CoreConfig
from .ledger import LedgerRecord
from .models import V1_THRESHOLDS, RoutingFeatures, Thresholds, tier_for

# A fit needs evidence. Below this many *labelled* records (records carrying the
# routing features written since routing-feature capture landed) `fit` refuses
# to emit thresholds at all.
DEFAULT_MIN_SAMPLES = 20

# The integer grid searched. Deliberately small and centred on the v1 constants:
# this is a calibration, not a hyperparameter sweep, and every candidate has to
# stay explainable to the architect reading the article.
HEAVY_GRID = tuple(range(2, 11))
LIGHT_GRID = tuple(range(-8, 0))

MERGED = "merged"

ARTICLE_STEM = "model-routing-calibration"
ARTICLES_DIR = "knowledge/articles"


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def min_samples_for(cfg: CoreConfig) -> int:
    """``models.calibration.min_samples``, or the default.

    Tolerates a missing or malformed calibration block: an unreadable sample
    floor must fall back to the conservative default, never to zero.
    """
    block = cfg.models.get("calibration")
    if not isinstance(block, dict):
        return DEFAULT_MIN_SAMPLES
    try:
        return int(block.get("min_samples", DEFAULT_MIN_SAMPLES))
    except (TypeError, ValueError):
        return DEFAULT_MIN_SAMPLES


# --- reading the ledger back as training data --------------------------------

def features_of(record: LedgerRecord) -> RoutingFeatures | None:
    """Reconstruct the routing features of one record, or ``None`` if unlabelled.

    A record written before this capability existed carries no
    ``complexity_score``; it still parses, it just cannot be replayed through a
    candidate threshold pair, so it is not a training example.
    """
    if record.complexity_score is None:
        return None
    return RoutingFeatures(
        complexity_score=int(record.complexity_score),
        est_files=int(record.est_files or 0),
        heavy_signals=int(record.heavy_signals or 0),
        light_signals=int(record.light_signals or 0),
        size_label=record.size_label or "",
        kind=record.kind,
    )


def labelled(records: list[LedgerRecord]) -> list[tuple[LedgerRecord, RoutingFeatures]]:
    """Every record that can serve as a training example, with its features."""
    pairs = []
    for r in records:
        feats = features_of(r)
        if feats is not None:
            pairs.append((r, feats))
    return pairs


# --- per-tier observation ------------------------------------------------------

@dataclass(frozen=True)
class TierStats:
    """What one tier actually delivered, per the ledger."""

    tier: str
    count: int
    merged: int
    success_rate: float
    mean_seconds: float

    def line(self) -> str:
        return (
            f"| `{self.tier}` | {self.count} | {self.merged} | "
            f"{self.success_rate:.0%} | {self.mean_seconds:.0f}s |"
        )


def tier_stats(records: list[LedgerRecord]) -> tuple[TierStats, ...]:
    """Observed success rate and mean wall-clock per tier, cheapest tier first."""
    by_tier: dict[str, list[LedgerRecord]] = {}
    for r in records:
        by_tier.setdefault(r.tier, []).append(r)
    order = {"light": 0, "standard": 1, "heavy": 2}
    stats = [
        TierStats(
            tier=tier,
            count=len(items),
            merged=sum(1 for r in items if r.outcome == MERGED),
            success_rate=sum(1 for r in items if r.outcome == MERGED) / len(items),
            mean_seconds=sum(r.wall_clock_seconds for r in items) / len(items),
        )
        for tier, items in by_tier.items()
    ]
    stats.sort(key=lambda s: (order.get(s.tier, 99), s.tier))
    return tuple(stats)


def render_tier_stats(stats: tuple[TierStats, ...]) -> str:
    if not stats:
        return "_no ledger records_"
    lines = [
        "| tier | iterations | merged | success rate | mean wall-clock |",
        "| --- | --- | --- | --- | --- |",
    ]
    lines.extend(s.line() for s in stats)
    return "\n".join(lines)


# --- the fit ------------------------------------------------------------------

@dataclass(frozen=True)
class Candidate:
    """One evaluated point of the threshold grid."""

    thresholds: Thresholds
    heavy_iterations: int
    expected_merged: float
    success_rate: float

    @property
    def objective(self) -> float:
        """Merged outcomes per heavy-tier iteration - what the search maximises.

        A candidate that routes nothing heavy is scored against a denominator of
        one rather than dividing by zero; the success-rate floor, not the
        denominator, is what stops the search from simply abolishing the heavy
        tier.
        """
        return self.expected_merged / max(1, self.heavy_iterations)


@dataclass(frozen=True)
class Fit:
    """The result of a calibration attempt - possibly an explicit refusal."""

    ok: bool
    reason: str
    sample_size: int
    min_samples: int
    stats: tuple[TierStats, ...] = ()
    # Populated only when ok: a refusal never carries thresholds.
    thresholds: Thresholds | None = None
    objective: float = 0.0
    baseline_objective: float = 0.0
    heavy_iterations: int = 0
    baseline_heavy_iterations: int = 0
    success_rate: float = 0.0
    success_floor: float = 0.0
    changed_records: int = 0

    @property
    def changes_routing(self) -> bool:
        """Would the fitted thresholds actually route anything differently?"""
        return bool(self.ok and self.thresholds != V1_THRESHOLDS)

    def recommendation(self) -> str:
        """One sentence an architect can act on, or decline to act on."""
        if not self.ok:
            return (
                f"**No recommendation.** {self.reason} Keep "
                "`models.selection_strategy: heuristic-v1` and let the loop "
                "accumulate more labelled iterations."
            )
        if not self.changes_routing:
            return (
                "**Keep heuristic-v1.** The fit reproduces the current thresholds "
                f"({self.thresholds.label()}), so promoting heuristic-v2 would "
                "change no routing decision."
            )
        return (
            f"**Consider pinning `{self.thresholds.label()}`** into "
            "`models.calibration` in `.ai-swarm/core.yaml`. It projects "
            f"{self.objective:.2f} merged outcomes per heavy iteration versus "
            f"{self.baseline_objective:.2f} today ({self.baseline_heavy_iterations} "
            f"heavy iterations -> {self.heavy_iterations}) while holding the "
            f"success rate at {self.success_rate:.0%} (floor {self.success_floor:.0%}). "
            f"It would have routed {self.changed_records} of {self.sample_size} "
            "recorded iterations differently. Shadow mode means nothing moves "
            "until `models.selection_strategy` is flipped to `heuristic-v2`."
        )

    def render(self) -> str:
        """Markdown summary shared by the CLI, the article and the review brief."""
        if not self.ok:
            return (
                f"_Insufficient data: {self.reason}_ "
                f"(labelled samples: {self.sample_size}/{self.min_samples})\n\n"
                + render_tier_stats(self.stats)
            )
        return (
            f"Fitted thresholds: **{self.thresholds.label()}** "
            f"(baseline {V1_THRESHOLDS.label()}) from {self.sample_size} labelled "
            f"iteration(s).\n\n"
            f"- merged per heavy iteration: {self.baseline_objective:.2f} -> "
            f"{self.objective:.2f}\n"
            f"- heavy iterations: {self.baseline_heavy_iterations} -> "
            f"{self.heavy_iterations}\n"
            f"- projected success rate: {self.success_rate:.0%} "
            f"(floor {self.success_floor:.0%})\n"
            f"- routing changes on {self.changed_records}/{self.sample_size} "
            "recorded iteration(s)\n\n"
            + render_tier_stats(self.stats)
        )


def _evaluate(
    pairs: list[tuple[LedgerRecord, RoutingFeatures]],
    thresholds: Thresholds,
    rates: dict[str, float],
    default_tier: str,
) -> Candidate:
    """Replay every labelled record through ``thresholds``.

    Counterfactual outcomes are estimated with the *observed* success rate of the
    tier a record would have been routed to. That is a deliberately modest
    estimator - it assumes only that a tier performs as it has performed - and it
    is exactly why the output is a recommendation for a human rather than a
    routing change.
    """
    expected = 0.0
    heavy = 0
    for _record, feats in pairs:
        tier, _why = tier_for(feats, thresholds, default_tier)
        expected += rates.get(tier, 0.0)
        if tier == "heavy":
            heavy += 1
    n = len(pairs)
    return Candidate(
        thresholds=thresholds,
        heavy_iterations=heavy,
        expected_merged=expected,
        success_rate=expected / n if n else 0.0,
    )


def fit(
    records: list[LedgerRecord],
    *,
    min_samples: int = DEFAULT_MIN_SAMPLES,
    success_floor: float | None = None,
    default_tier: str = "standard",
) -> Fit:
    """Fit routing thresholds from ledger history, or refuse for want of data.

    ``success_floor`` defaults to the sample's *observed* overall success rate:
    a cheaper routing is only worth recommending if it does not deliver less.
    Pass an explicit float to hold a different bar.

    Deterministic: ties in the objective break toward fewer heavy iterations,
    then toward the thresholds closest to :data:`hsai.models.V1_THRESHOLDS`, so
    a fit is stable across runs and reviewable as a diff.
    """
    stats = tier_stats(records)
    pairs = labelled(records)
    n = len(pairs)
    if n < min_samples:
        return Fit(
            ok=False,
            reason=(
                f"only {n} labelled ledger record(s); {min_samples} required before "
                "thresholds can be fitted"
            ),
            sample_size=n,
            min_samples=min_samples,
            stats=stats,
        )

    rates = {s.tier: s.success_rate for s in stats}
    observed = sum(1 for r, _f in pairs if r.outcome == MERGED) / n
    floor = observed if success_floor is None else float(success_floor)

    baseline = _evaluate(pairs, V1_THRESHOLDS, rates, default_tier)
    feasible = [
        cand
        for cand in (
            _evaluate(pairs, Thresholds(heavy=h, light=lo), rates, default_tier)
            for h in HEAVY_GRID
            for lo in LIGHT_GRID
        )
        if cand.success_rate >= floor
    ]
    # The v1 point is always admissible: refusing to consider the status quo
    # would let an empty feasible set masquerade as "no fit possible".
    if not feasible:
        feasible = [baseline]

    def _key(cand: Candidate) -> tuple:
        t = cand.thresholds
        return (
            -round(cand.objective, 9),
            cand.heavy_iterations,
            abs(t.heavy - V1_THRESHOLDS.heavy) + abs(t.light - V1_THRESHOLDS.light),
            t.heavy,
            t.light,
        )

    best = min(feasible, key=_key)
    changed = sum(
        1
        for _r, feats in pairs
        if tier_for(feats, best.thresholds, default_tier)[0]
        != tier_for(feats, V1_THRESHOLDS, default_tier)[0]
    )
    return Fit(
        ok=True,
        reason=f"fitted over {n} labelled ledger record(s)",
        sample_size=n,
        min_samples=min_samples,
        stats=stats,
        thresholds=best.thresholds,
        objective=best.objective,
        baseline_objective=baseline.objective,
        heavy_iterations=best.heavy_iterations,
        baseline_heavy_iterations=baseline.heavy_iterations,
        success_rate=best.success_rate,
        success_floor=floor,
        changed_records=changed,
    )


# --- shadow-vs-active disagreement ---------------------------------------------

@dataclass(frozen=True)
class Disagreement:
    """How often the shadow strategy would have routed differently."""

    observed: int      # records carrying a shadow tier at all
    disagreed: int
    examples: tuple[str, ...] = ()

    @property
    def rate(self) -> float:
        return self.disagreed / self.observed if self.observed else 0.0

    def render(self) -> str:
        if not self.observed:
            return "_no shadow-tier records yet_"
        detail = "; ".join(self.examples)
        return (
            f"Shadow disagreement: {self.disagreed}/{self.observed} "
            f"({self.rate:.0%}) of recorded iterations"
            + (f" - e.g. {detail}" if detail else "")
        )


def disagreement(records: list[LedgerRecord], *, max_examples: int = 3) -> Disagreement:
    """Compare each record's active tier against the shadow tier it recorded."""
    observed = [r for r in records if r.shadow_tier]
    disagreed = [r for r in observed if r.shadow_tier != r.tier]
    examples = tuple(
        f"iteration {r.iteration}: `{r.tier}` -> `{r.shadow_tier}`"
        for r in disagreed[:max_examples]
    )
    return Disagreement(
        observed=len(observed), disagreed=len(disagreed), examples=examples
    )


# --- the committed report -------------------------------------------------------

def article_name(date: str = "") -> str:
    return f"{date or _today()}-{ARTICLE_STEM}"


def render_article(fit_result: Fit, dis: Disagreement, *, date: str = "") -> str:
    """The committed calibration report: the fit, the evidence, the recommendation."""
    stamp = date or _today()
    return f"""---
tags:
  - article
  - calibration
  - routing
created: {stamp}
---

# Model-routing calibration - {stamp}

Produced by `hsai calibrate` from `knowledge/ledger/iterations.jsonl`. Reading
only: this report changes no configuration and routes no work. Thresholds take
effect when a human pins them into `models.calibration` in `.ai-swarm/core.yaml`
and flips `models.selection_strategy` to `heuristic-v2`.

## Sample

- Labelled ledger records (sample size): **{fit_result.sample_size}**
- Minimum required for a fit: **{fit_result.min_samples}**
- Status: **{"fitted" if fit_result.ok else "insufficient data"}** - {fit_result.reason}

## Fit

{fit_result.render()}

## Shadow evaluation

{dis.render()}

## Recommendation

{fit_result.recommendation()}
"""


def write_article(
    repo_root: str | Path, fit_result: Fit, dis: Disagreement, *, date: str = ""
) -> Path:
    """Write the calibration report under ``knowledge/articles/`` and return its path.

    The only write this module ever performs, and the only file ``hsai
    calibrate`` touches.
    """
    stamp = date or _today()
    out = Path(repo_root) / ARTICLES_DIR / f"{article_name(stamp)}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_article(fit_result, dis, date=stamp), encoding="utf-8")
    return out
