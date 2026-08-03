"""Learned model-selection calibration (heuristic-v2).

``models.py`` scores a task and compares that score against two thresholds.
Until now those thresholds were constants: the selector never got better, no
matter how many lessons the loop wrote. This module closes that loop.

- **Parse** - every lesson note already records the tier that ran the task and
  whether it passed. :func:`parse_lesson_outcomes` turns the corpus back into
  labelled ``(kind, tier, outcome, attempt)`` samples.
- **Calibrate** - :func:`calibrate` folds those samples into per-tier success
  rates and derives *clamped* threshold deltas: a tier that keeps failing makes
  the selector reach for a heavier model, a tier that keeps passing makes it
  stay cheap.
- **Persist** - the derived parameters are written to a versioned JSON artifact
  (``.ai-swarm/model-selection.json``) so a selection made months ago can be
  explained from what was on disk at the time.

Two guardrails keep a small or biased corpus from wrecking selection:

1. **Sparse fallback** - fewer than ``min_labeled_lessons`` samples returns
   :data:`HEURISTIC_V1` verbatim, so the loop degrades to the known-good static
   heuristic rather than to noise.
2. **Share clamp** - a tier that already holds more than ``max_tier_share`` of
   the corpus cannot be grown further unless it also clears
   ``share_min_samples``. Nine lessons that all happen to be heavy therefore
   cannot collapse everything to opus (quota), and the mirror case cannot
   collapse everything to haiku (quality).

Synthesis: microsoft/JARVIS (a controller routing each sub-task to the model
that fits its complexity), OpenBMB/ChatDev (a learnable orchestrator optimised
to cut compute), assafelovic/gpt-researcher (explicit, maintained model-family
configuration rather than ad-hoc strings).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from pathlib import Path

from .config import CoreConfig
from .knowledge import KnowledgeBase, LessonRecord

SCHEMA_VERSION = 1
V1 = "heuristic-v1"
V2 = "heuristic-v2"

DEFAULT_PARAMS_FILE = ".ai-swarm/model-selection.json"

# heuristic-v1's constants. These are the safe fallback and the anchor every
# calibrated delta is measured against - never change one without a lesson.
V1_HEAVY_THRESHOLD = 5
V1_LIGHT_THRESHOLD = -3

TIERS = ("light", "standard", "heavy")

# Legacy signal: lessons written before the tier/attempt table rows existed
# still carry "Model `sonnet` (standard) ran the task." in *What happened*.
_TIER_IN_TEXT = re.compile(r"Model\s+`[^`]+`\s+\((light|standard|heavy)\)")


@dataclass(frozen=True)
class LessonOutcome:
    """One labelled sample: which tier ran what kind of work, and how it went."""

    note: str
    kind: str
    tier: str
    outcome: str  # pass | fail
    attempt: int = 1

    @property
    def passed(self) -> bool:
        return self.outcome == "pass"


@dataclass(frozen=True)
class TierStats:
    """Observed reliability of one tier across the lesson corpus."""

    tier: str
    samples: int = 0
    passes: int = 0

    @property
    def success_rate(self) -> float:
        return self.passes / self.samples if self.samples else 0.0

    def to_dict(self) -> dict:
        return {"tier": self.tier, "samples": self.samples, "passes": self.passes}


@dataclass(frozen=True)
class CalibrationParams:
    """The versioned artifact ``models.select()`` reads its thresholds from."""

    strategy: str = V1
    heavy_threshold: int = V1_HEAVY_THRESHOLD
    light_threshold: int = V1_LIGHT_THRESHOLD
    escalate_on_retry: bool = True
    samples: int = 0
    tier_stats: tuple[TierStats, ...] = ()
    notes: tuple[str, ...] = ()
    schema_version: int = SCHEMA_VERSION

    @property
    def calibrated(self) -> bool:
        return self.strategy == V2

    def summary(self) -> str:
        return (
            f"{self.strategy} from {self.samples} labelled lesson(s): "
            f"heavy>={self.heavy_threshold}, light<={self.light_threshold}"
        )

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "strategy": self.strategy,
            "heavy_threshold": self.heavy_threshold,
            "light_threshold": self.light_threshold,
            "escalate_on_retry": self.escalate_on_retry,
            "samples": self.samples,
            "tier_stats": [s.to_dict() for s in self.tier_stats],
            "notes": list(self.notes),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n"

    @classmethod
    def from_dict(cls, data: dict) -> CalibrationParams:
        """Rebuild params from the artifact, ignoring anything unrecognised."""
        stats = tuple(
            TierStats(
                tier=str(s.get("tier", "")),
                samples=int(s.get("samples", 0)),
                passes=int(s.get("passes", 0)),
            )
            for s in data.get("tier_stats", [])
            if s.get("tier") in TIERS
        )
        return cls(
            strategy=str(data.get("strategy", V1)),
            heavy_threshold=int(data.get("heavy_threshold", V1_HEAVY_THRESHOLD)),
            light_threshold=int(data.get("light_threshold", V1_LIGHT_THRESHOLD)),
            escalate_on_retry=bool(data.get("escalate_on_retry", True)),
            samples=int(data.get("samples", 0)),
            tier_stats=stats,
            notes=tuple(str(n) for n in data.get("notes", [])),
            schema_version=int(data.get("schema_version", SCHEMA_VERSION)),
        )


#: heuristic-v1, verbatim. Returned whenever calibration must not fire.
HEURISTIC_V1 = CalibrationParams()


# --- parsing -------------------------------------------------------------------
def parse_lesson_outcomes(records: list[LessonRecord]) -> list[LessonOutcome]:
    """Keep only the lessons that carry a usable ``(tier, outcome)`` label.

    A lesson without a tier (hand-written notes, architect write-ups) or with an
    inconclusive outcome teaches the selector nothing and is dropped rather than
    guessed at.
    """
    out: list[LessonOutcome] = []
    for r in records:
        tier = r.tier or _tier_from_text(r.what_happened)
        if tier not in TIERS or r.outcome not in ("pass", "fail"):
            continue
        out.append(
            LessonOutcome(
                note=r.note_name,
                kind=r.kind,
                tier=tier,
                outcome=r.outcome,
                attempt=max(1, r.attempt),
            )
        )
    return out


def _tier_from_text(text: str) -> str:
    m = _TIER_IN_TEXT.search(text or "")
    return m.group(1) if m else ""


def tier_stats(outcomes: list[LessonOutcome]) -> dict[str, TierStats]:
    """Per-tier sample and pass counts across ``outcomes``."""
    stats = {t: TierStats(tier=t) for t in TIERS}
    for o in outcomes:
        prev = stats[o.tier]
        stats[o.tier] = TierStats(
            tier=o.tier,
            samples=prev.samples + 1,
            passes=prev.passes + (1 if o.passed else 0),
        )
    return stats


# --- calibration ---------------------------------------------------------------
def _clamp(value: int, limit: int) -> int:
    return max(-limit, min(limit, value))


def _settings(cfg: CoreConfig | None) -> dict:
    models = (cfg.raw.get("models", {}) if cfg else {}) or {}
    return models.get("calibration", {}) or {}


def calibrate(outcomes: list[LessonOutcome], settings: dict | None = None) -> CalibrationParams:
    """Derive heuristic-v2 parameters from labelled lesson outcomes.

    The mechanism is deliberately small enough to audit by hand: compare each
    tier's observed success rate against ``target_success_rate`` and move the
    threshold that governs how much work reaches that tier, by at most
    ``max_threshold_delta`` steps.

    Movement is refused - with a note explaining why - when the tier that would
    grow is under-sampled or already over its configured share of the corpus.
    Sparse corpora return :data:`HEURISTIC_V1` unchanged.
    """
    s = settings or {}
    min_labeled = int(s.get("min_labeled_lessons", 10))
    min_per_tier = int(s.get("min_samples_per_tier", 3))
    max_delta = max(0, int(s.get("max_threshold_delta", 2)))
    max_share = float(s.get("max_tier_share", 0.6))
    share_min_samples = int(s.get("share_min_samples", 8))
    target = float(s.get("target_success_rate", 0.8))
    scale = float(s.get("delta_scale", 4.0))
    escalate = bool(s.get("escalate_on_retry", True))

    stats = tier_stats(outcomes)
    ordered = tuple(stats[t] for t in TIERS)
    total = len(outcomes)

    if total < min_labeled:
        return replace(
            HEURISTIC_V1,
            escalate_on_retry=escalate,
            samples=total,
            tier_stats=ordered,
            notes=(
                f"sparse corpus: {total} labelled lesson(s) < min_labeled_lessons="
                f"{min_labeled}; falling back to {V1} verbatim",
            ),
        )

    notes: list[str] = []

    def may_grow(tier: str) -> bool:
        """Guard against a small or biased corpus collapsing onto one tier."""
        n = stats[tier].samples
        share = n / total if total else 0.0
        if share <= max_share:
            return True
        if n >= share_min_samples:
            notes.append(
                f"{tier} holds {share:.0%} of the corpus (> {max_share:.0%}) but has "
                f"{n} samples (>= {share_min_samples}); growth allowed"
            )
            return True
        notes.append(
            f"clamped: {tier} already holds {share:.0%} of the corpus "
            f"(> {max_share:.0%}) on only {n} samples (< {share_min_samples}); "
            "refusing to grow it further"
        )
        return False

    def delta_for(tier: str, grows_when_negative: str, grows_when_positive: str) -> int:
        """Signed threshold move driven by ``tier``'s observed success rate."""
        st = stats[tier]
        if st.samples < min_per_tier:
            notes.append(
                f"{tier}: {st.samples} sample(s) < min_samples_per_tier={min_per_tier}; "
                f"leaving its threshold at the {V1} value"
            )
            return 0
        raw = _clamp(round((target - st.success_rate) * scale), max_delta)
        delta = -raw
        if delta == 0:
            notes.append(
                f"{tier}: success {st.success_rate:.0%} over {st.samples} sample(s) "
                f"is within tolerance of target {target:.0%}; no move"
            )
            return 0
        grown = grows_when_negative if delta < 0 else grows_when_positive
        if not may_grow(grown):
            return 0
        notes.append(
            f"{tier}: success {st.success_rate:.0%} over {st.samples} sample(s) vs "
            f"target {target:.0%} -> {delta:+d} (grows {grown})"
        )
        return delta

    # Standard's reliability governs how much work escalates to heavy: when the
    # workhorse tier keeps failing, lower the bar for heavy; when it keeps
    # passing, raise it and keep the quota on sonnet.
    heavy_threshold = V1_HEAVY_THRESHOLD + delta_for("standard", "heavy", "standard")
    # Light's reliability governs how much work drops to light.
    light_threshold = V1_LIGHT_THRESHOLD + delta_for("light", "standard", "light")

    # Never let the bands invert or touch - there must always be room for the
    # default tier between them.
    if heavy_threshold - light_threshold < 2:
        notes.append(
            f"clamped: thresholds heavy={heavy_threshold}/light={light_threshold} left no "
            f"room for the default tier; reverting to the {V1} band"
        )
        heavy_threshold, light_threshold = V1_HEAVY_THRESHOLD, V1_LIGHT_THRESHOLD

    return CalibrationParams(
        strategy=V2,
        heavy_threshold=heavy_threshold,
        light_threshold=light_threshold,
        escalate_on_retry=escalate,
        samples=total,
        tier_stats=ordered,
        notes=tuple(notes),
    )


# --- persistence ---------------------------------------------------------------
def params_path(cfg: CoreConfig | None, root: str | Path = ".") -> Path:
    """Resolve the versioned parameter artifact under ``root``."""
    rel = _settings(cfg).get("params_file", DEFAULT_PARAMS_FILE)
    return Path(root) / rel


def load_params(cfg: CoreConfig | None, root: str | Path = ".") -> CalibrationParams:
    """Read the persisted parameters; fall back to heuristic-v1 on any problem.

    A missing, unreadable, or malformed artifact must never stop the loop: the
    static heuristic is always a correct answer.
    """
    path = params_path(cfg, root)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return HEURISTIC_V1
    if not isinstance(data, dict):
        return HEURISTIC_V1
    try:
        return CalibrationParams.from_dict(data)
    except (TypeError, ValueError):
        return HEURISTIC_V1


def save_params(params: CalibrationParams, cfg: CoreConfig | None, root: str | Path = ".") -> Path:
    path = params_path(cfg, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(params.to_json(), encoding="utf-8")
    return path


def calibrate_repo(cfg: CoreConfig, root: str | Path = ".") -> CalibrationParams:
    """Calibrate from the lesson corpus on disk (does not write anything)."""
    kb = KnowledgeBase.from_config(cfg, root)
    return calibrate(parse_lesson_outcomes(kb.read_lessons()), _settings(cfg))


def recalibrate(cfg: CoreConfig, root: str | Path = ".") -> CalibrationParams:
    """Calibrate from the corpus on disk and persist the artifact."""
    params = calibrate_repo(cfg, root)
    save_params(params, cfg, root)
    return params
