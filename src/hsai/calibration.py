"""Learned calibration of the model-selection heuristic (heuristic-v2).

heuristic-v1 (:mod:`hsai.models`) is a static keyword + structure score with
fixed thresholds. This module *learns* adjusted thresholds from the repo's own
lesson corpus: every merged PR records the tier it used and whether it passed,
so per-tier success rates are observable. We fold those signals into bounded
threshold deltas - persisted as heuristic-v2 parameters - and fall back to
heuristic-v1 verbatim whenever the data is too sparse to trust.

Guardrails, so a tiny or biased corpus cannot collapse routing onto one tier:
- a global min-sample gate: below it, v1 is returned unchanged;
- per-tier min-sample gates: a tier's success rate only moves its own boundary
  once enough labeled samples exist;
- clamped deltas: no threshold moves more than a fixed step away from v1;
- a share clamp: no tier may exceed a configured share of a representative score
  spread unless it meets its own min-sample gate.

References:
- OpenBMB/ChatDev: a learnable orchestrator tuned to cut compute while keeping
  quality (the puppeteer paradigm).
- assafelovic/gpt-researcher: disciplined, allowlisted model-family config.
- microsoft/JARVIS: an LLM controller routing each sub-task to a fitting model.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .config import CoreConfig

# heuristic-v1 thresholds - the safe fallback the calibration steers away from.
V1_HEAVY_THRESHOLD = 5
V1_LIGHT_THRESHOLD = -3

# Tiers ordered from lightest to heaviest; escalation walks this list.
TIER_ORDER = ("light", "standard", "heavy")

# A representative spread of complexity scores used only to estimate how much of
# the routing a given threshold pair would send to each tier (the share clamp).
_GRID_LOW = -8
_GRID_HIGH = 8


@dataclass(frozen=True)
class CalibrationConfig:
    """Conservative knobs bounding how far the learned selector may move.

    Defaults are intentionally cautious; they may be overridden per-repo via the
    ``models.calibration`` block in ``.ai-swarm/core.yaml`` so the parameters
    stay versioned and auditable.
    """

    min_labeled: int = 5        # global gate: labeled lessons before v2 engages
    min_per_tier: int = 3       # per-tier gate before that boundary may move
    max_delta: int = 2          # clamp: a threshold moves at most this far from v1
    max_share: float = 0.7      # no tier beyond this routing share w/o its gate
    target_success: float = 0.8  # per-tier success rate the calibration steers to
    delta_scale: float = 4.0    # maps a success gap to an integer threshold delta

    @classmethod
    def from_config(cls, cfg: CoreConfig) -> CalibrationConfig:
        raw = ((cfg.raw.get("models") or {}).get("calibration") or {})
        d = cls()
        return cls(
            min_labeled=int(raw.get("min_labeled", d.min_labeled)),
            min_per_tier=int(raw.get("min_per_tier", d.min_per_tier)),
            max_delta=int(raw.get("max_delta", d.max_delta)),
            max_share=float(raw.get("max_share", d.max_share)),
            target_success=float(raw.get("target_success", d.target_success)),
            delta_scale=float(raw.get("delta_scale", d.delta_scale)),
        )


@dataclass(frozen=True)
class Outcome:
    """One labeled data point mined from a lesson: which tier, pass or fail."""

    tier: str
    passed: bool


@dataclass(frozen=True)
class CalibrationParams:
    """The learned (or fallback) thresholds the selector reads, plus an audit trail."""

    heavy_threshold: int
    light_threshold: int
    strategy: str  # "heuristic-v1" (fallback) | "heuristic-v2" (learned)
    n_labeled: int = 0
    tier_counts: dict[str, int] = field(default_factory=dict)
    tier_success: dict[str, float] = field(default_factory=dict)
    notes: tuple[str, ...] = ()

    @classmethod
    def fallback(cls, *, n_labeled: int = 0, notes: tuple[str, ...] = ()) -> CalibrationParams:
        """heuristic-v1 verbatim: the safe answer when data is sparse or absent."""
        return cls(
            heavy_threshold=V1_HEAVY_THRESHOLD,
            light_threshold=V1_LIGHT_THRESHOLD,
            strategy="heuristic-v1",
            n_labeled=n_labeled,
            notes=notes,
        )


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def model_to_tier(cfg: CoreConfig) -> dict[str, str]:
    """Invert the configured tiers into a ``model alias -> tier name`` map."""
    return {t.model: name for name, t in cfg.tiers.items()}


def outcomes_from_records(records, cfg: CoreConfig) -> list[Outcome]:
    """Mine labeled outcomes from parsed lesson records.

    A lesson counts only if it has a definite pass/fail outcome AND a model that
    maps to a known tier - human/`n/a` entries are ignored on purpose.
    """
    m2t = model_to_tier(cfg)
    out: list[Outcome] = []
    for r in records:
        if r.outcome not in ("pass", "fail"):
            continue
        tier = m2t.get((r.model or "").strip())
        if tier not in TIER_ORDER:
            continue
        out.append(Outcome(tier=tier, passed=(r.outcome == "pass")))
    return out


def _grid_tier(score: int, heavy: int, light: int) -> str:
    if score >= heavy:
        return "heavy"
    if score <= light:
        return "light"
    return "standard"


def _tier_shares(heavy: int, light: int) -> dict[str, float]:
    """Fraction of a representative score grid each tier would receive."""
    counts = {t: 0 for t in TIER_ORDER}
    grid = range(_GRID_LOW, _GRID_HIGH + 1)
    for s in grid:
        counts[_grid_tier(s, heavy, light)] += 1
    total = sum(counts.values()) or 1
    return {t: counts[t] / total for t in TIER_ORDER}


def _apply_share_clamp(
    heavy: int, light: int, counts: dict[str, int], cal: CalibrationConfig
) -> tuple[int, int, list[str]]:
    """Walk over-represented, under-sampled tiers back toward v1.

    Only movement *into* a tier (heavy pushed below v1, or light pushed above v1)
    can be clamped - a tier that meets its per-tier gate has earned its share.
    """
    notes: list[str] = []
    while (
        heavy < V1_HEAVY_THRESHOLD
        and counts.get("heavy", 0) < cal.min_per_tier
        and _tier_shares(heavy, light)["heavy"] > cal.max_share
    ):
        heavy += 1
        notes.append(
            f"share clamp: heavy over {cal.max_share:.0%} and under-sampled; "
            f"heavy_threshold -> {heavy}"
        )
    while (
        light > V1_LIGHT_THRESHOLD
        and counts.get("light", 0) < cal.min_per_tier
        and _tier_shares(heavy, light)["light"] > cal.max_share
    ):
        light -= 1
        notes.append(
            f"share clamp: light over {cal.max_share:.0%} and under-sampled; "
            f"light_threshold -> {light}"
        )
    return heavy, light, notes


def _boundary_delta(success: float, cal: CalibrationConfig) -> int:
    """Signed threshold delta from a tier's success gap, clamped to +/- max_delta.

    Struggling (success below target) yields a positive delta that *lowers* the
    boundary (escalating more work); over-performing yields a negative delta that
    *raises* it (reclaiming quota).
    """
    gap = cal.target_success - success
    return _clamp(round(gap * cal.delta_scale), -cal.max_delta, cal.max_delta)


def calibrate(
    outcomes: list[Outcome], cfg: CoreConfig, cal: CalibrationConfig | None = None
) -> CalibrationParams:
    """Derive heuristic-v2 thresholds from labeled outcomes, or fall back to v1."""
    cal = cal or CalibrationConfig.from_config(cfg)
    n = len(outcomes)
    if n < cal.min_labeled:
        return CalibrationParams.fallback(
            n_labeled=n,
            notes=(
                f"sparse data ({n} < {cal.min_labeled} labeled lessons); "
                "heuristic-v1 fallback",
            ),
        )

    counts = {t: 0 for t in TIER_ORDER}
    passes = {t: 0 for t in TIER_ORDER}
    for o in outcomes:
        counts[o.tier] += 1
        passes[o.tier] += 1 if o.passed else 0
    success = {t: (passes[t] / counts[t] if counts[t] else 0.0) for t in TIER_ORDER}

    notes: list[str] = []
    heavy = V1_HEAVY_THRESHOLD
    light = V1_LIGHT_THRESHOLD

    # The standard-tier success rate governs the standard/heavy boundary.
    if counts["standard"] >= cal.min_per_tier:
        delta = _boundary_delta(success["standard"], cal)
        heavy = V1_HEAVY_THRESHOLD - delta
        if delta:
            notes.append(
                f"standard success={success['standard']:.2f} -> "
                f"heavy_threshold {V1_HEAVY_THRESHOLD}->{heavy}"
            )
    else:
        notes.append(
            f"standard under-sampled ({counts['standard']} < {cal.min_per_tier}); "
            "heavy_threshold held at v1"
        )

    # The light-tier success rate governs the light/standard boundary.
    if counts["light"] >= cal.min_per_tier:
        delta = _boundary_delta(success["light"], cal)
        light = V1_LIGHT_THRESHOLD - delta
        if delta:
            notes.append(
                f"light success={success['light']:.2f} -> "
                f"light_threshold {V1_LIGHT_THRESHOLD}->{light}"
            )
    else:
        notes.append(
            f"light under-sampled ({counts['light']} < {cal.min_per_tier}); "
            "light_threshold held at v1"
        )

    heavy, light, clamp_notes = _apply_share_clamp(heavy, light, counts, cal)
    notes.extend(clamp_notes)

    return CalibrationParams(
        heavy_threshold=heavy,
        light_threshold=light,
        strategy="heuristic-v2",
        n_labeled=n,
        tier_counts=counts,
        tier_success=success,
        notes=tuple(notes),
    )


def load_params(cfg: CoreConfig, root: str | Path) -> CalibrationParams:
    """Read the lesson corpus under ``root`` and calibrate heuristic-v2 params."""
    from .knowledge import KnowledgeBase

    kb = KnowledgeBase.from_config(cfg, root)
    records = kb.read_lessons()
    return calibrate(outcomes_from_records(records, cfg), cfg)
