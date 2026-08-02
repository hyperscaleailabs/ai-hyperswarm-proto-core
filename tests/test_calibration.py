"""Learned model-selection calibration: parsing, math, clamps, and fallback."""
from __future__ import annotations

import json

from hsai import calibration
from hsai.calibration import (
    HEURISTIC_V1,
    V1,
    V1_HEAVY_THRESHOLD,
    V1_LIGHT_THRESHOLD,
    V2,
    CalibrationParams,
    LessonOutcome,
    calibrate,
    parse_lesson_outcomes,
    tier_stats,
)
from hsai.config import load_config
from hsai.knowledge import KnowledgeBase, Lesson, LessonRecord

# Permissive settings: the gates under test are switched on case by case so a
# test that is not about a gate is never accidentally blocked by one.
OPEN = {
    "min_labeled_lessons": 4,
    "min_samples_per_tier": 2,
    "max_threshold_delta": 2,
    "max_tier_share": 1.0,
    "share_min_samples": 0,
    "target_success_rate": 0.8,
    "delta_scale": 4.0,
}


def _outcomes(tier: str, *, passes: int, fails: int, kind: str = "implement"):
    out = [
        LessonOutcome(note=f"{tier}-pass-{i}", kind=kind, tier=tier, outcome="pass")
        for i in range(passes)
    ]
    out += [
        LessonOutcome(note=f"{tier}-fail-{i}", kind=kind, tier=tier, outcome="fail")
        for i in range(fails)
    ]
    return out


class TestParsingLessonOutcomes:
    """Lessons on disk are the only training signal - parse them exactly."""

    def test_parses_tier_and_outcome_from_a_written_lesson(self, tmp_path):
        kb = KnowledgeBase(tmp_path)
        kb.write_lesson(
            Lesson(
                title="implement: add a thing",
                outcome="pass",
                kind="implement",
                context="c",
                what_happened="w",
                lesson="l",
                model="sonnet",
                tier="standard",
                attempt=2,
            )
        )
        (record,) = kb.read_lessons()
        assert record.tier == "standard"
        assert record.attempt == 2

        (parsed,) = parse_lesson_outcomes([record])
        assert parsed.tier == "standard"
        assert parsed.kind == "implement"
        assert parsed.outcome == "pass"
        assert parsed.attempt == 2
        assert parsed.passed is True

    def test_falls_back_to_the_legacy_what_happened_signal(self):
        """Lessons written before the tier row still carry the tier in prose."""
        legacy = LessonRecord(
            note_name="2026-07-26-old",
            title="implement: something",
            outcome="fail",
            kind="implement",
            tags=(),
            lesson_text="",
            what_happened="Model `haiku` (light) ran the task. Agent ok=False.",
        )
        (parsed,) = parse_lesson_outcomes([legacy])
        assert parsed.tier == "light"
        assert parsed.passed is False

    def test_drops_lessons_without_a_usable_label(self):
        unlabelled = LessonRecord(
            note_name="2026-07-26-architect-note",
            title="steering",
            outcome="unknown",
            kind="unknown",
            tags=(),
            lesson_text="",
            what_happened="A hand-written architect note; no model ran.",
        )
        assert parse_lesson_outcomes([unlabelled]) == []

    def test_tier_stats_counts_samples_and_passes(self):
        stats = tier_stats(_outcomes("standard", passes=3, fails=1))
        assert stats["standard"].samples == 4
        assert stats["standard"].passes == 3
        assert stats["standard"].success_rate == 0.75
        assert stats["light"].samples == 0
        assert stats["light"].success_rate == 0.0


class TestSparseFallback:
    """Too little evidence must degrade to the known-good static heuristic."""

    def test_empty_corpus_is_heuristic_v1_verbatim(self):
        params = calibrate([], OPEN)
        assert params.strategy == V1
        assert params.heavy_threshold == HEURISTIC_V1.heavy_threshold
        assert params.light_threshold == HEURISTIC_V1.light_threshold
        assert "sparse corpus" in params.notes[0]

    def test_below_min_labeled_lessons_is_heuristic_v1_verbatim(self):
        # Six unambiguous standard-tier failures - enough to move thresholds a
        # long way, but one sample short of the configured minimum.
        outcomes = _outcomes("standard", passes=0, fails=6)
        params = calibrate(outcomes, {**OPEN, "min_labeled_lessons": 7})
        assert params.strategy == V1
        assert params.heavy_threshold == V1_HEAVY_THRESHOLD
        assert params.light_threshold == V1_LIGHT_THRESHOLD
        assert params.samples == 6

    def test_one_more_lesson_crosses_the_gate(self):
        outcomes = _outcomes("standard", passes=0, fails=7)
        params = calibrate(outcomes, {**OPEN, "min_labeled_lessons": 7})
        assert params.strategy == V2
        assert params.heavy_threshold < V1_HEAVY_THRESHOLD


class TestCalibrationMath:
    """Thresholds must move in the direction the evidence points."""

    def test_failing_standard_tier_makes_heavy_easier_to_reach(self):
        params = calibrate(_outcomes("standard", passes=1, fails=5), OPEN)
        assert params.strategy == V2
        # 17% success vs an 80% target -> escalate more work to heavy.
        assert params.heavy_threshold == V1_HEAVY_THRESHOLD - 2

    def test_reliable_standard_tier_keeps_work_off_heavy(self):
        params = calibrate(_outcomes("standard", passes=6, fails=0), OPEN)
        assert params.heavy_threshold == V1_HEAVY_THRESHOLD + 1

    def test_failing_light_tier_narrows_the_light_band(self):
        outcomes = _outcomes("light", passes=1, fails=4) + _outcomes(
            "standard", passes=2, fails=0
        )
        params = calibrate(outcomes, OPEN)
        # A light tier that keeps failing must catch fewer tasks.
        assert params.light_threshold < V1_LIGHT_THRESHOLD

    def test_reliable_light_tier_widens_the_light_band(self):
        outcomes = _outcomes("light", passes=6, fails=0) + _outcomes(
            "standard", passes=2, fails=1
        )
        params = calibrate(outcomes, OPEN)
        assert params.light_threshold > V1_LIGHT_THRESHOLD

    def test_under_sampled_tier_does_not_move_its_threshold(self):
        outcomes = _outcomes("standard", passes=0, fails=1) + _outcomes(
            "light", passes=4, fails=0
        )
        params = calibrate(outcomes, {**OPEN, "min_samples_per_tier": 3})
        assert params.heavy_threshold == V1_HEAVY_THRESHOLD
        assert any("min_samples_per_tier" in n for n in params.notes)

    def test_every_move_is_explained_in_the_notes(self):
        params = calibrate(_outcomes("standard", passes=1, fails=5), OPEN)
        assert params.notes
        assert any("standard" in n for n in params.notes)


class TestClamps:
    """A tiny or biased corpus must not collapse selection onto one tier."""

    def test_delta_is_clamped_to_max_threshold_delta(self):
        # A total wipeout would want a far larger move than the clamp allows.
        outcomes = _outcomes("standard", passes=0, fails=20)
        params = calibrate(outcomes, {**OPEN, "max_threshold_delta": 1})
        assert params.heavy_threshold == V1_HEAVY_THRESHOLD - 1

    def test_a_tier_under_its_share_cap_may_still_be_grown(self):
        # The clamp gates the tier that would *grow*, not the tier supplying the
        # evidence: heavy holds 0% here, so a failing standard tier still moves.
        outcomes = _outcomes("standard", passes=0, fails=5)
        params = calibrate(
            outcomes, {**OPEN, "max_tier_share": 0.6, "share_min_samples": 8}
        )
        assert params.heavy_threshold < V1_HEAVY_THRESHOLD

    def test_dominant_light_tier_cannot_widen_itself_on_thin_evidence(self):
        # 5 of 6 lessons are light and all passed: naively that says "route
        # even more work to haiku". The share clamp refuses on 5 samples.
        outcomes = _outcomes("light", passes=5, fails=0) + _outcomes(
            "standard", passes=1, fails=0
        )
        params = calibrate(
            outcomes,
            {**OPEN, "max_tier_share": 0.6, "share_min_samples": 8},
        )
        assert params.light_threshold == V1_LIGHT_THRESHOLD
        assert any("refusing to grow" in n for n in params.notes)

    def test_the_same_bias_moves_once_the_min_sample_gate_is_met(self):
        outcomes = _outcomes("light", passes=9, fails=0) + _outcomes(
            "standard", passes=1, fails=0
        )
        params = calibrate(
            outcomes,
            {**OPEN, "max_tier_share": 0.6, "share_min_samples": 8},
        )
        assert params.light_threshold > V1_LIGHT_THRESHOLD
        assert any("growth allowed" in n for n in params.notes)

    def test_dominant_heavy_tier_cannot_pull_more_work_onto_itself(self):
        # 5 of 7 lessons ran heavy; standard failed its two. Growing heavy on
        # this corpus would be a quota trap, so the clamp holds it.
        outcomes = _outcomes("heavy", passes=5, fails=0) + _outcomes(
            "standard", passes=0, fails=2
        )
        params = calibrate(
            outcomes,
            {
                **OPEN,
                "min_samples_per_tier": 2,
                "max_tier_share": 0.6,
                "share_min_samples": 8,
            },
        )
        assert params.heavy_threshold == V1_HEAVY_THRESHOLD
        assert any("refusing to grow" in n for n in params.notes)

    def test_thresholds_never_invert(self):
        # Force both bands toward each other and assert the band guard holds.
        outcomes = _outcomes("standard", passes=0, fails=6) + _outcomes(
            "light", passes=6, fails=0
        )
        params = calibrate(
            outcomes, {**OPEN, "max_threshold_delta": 6, "delta_scale": 20.0}
        )
        assert params.heavy_threshold - params.light_threshold >= 2


class TestPersistence:
    """The params artifact is versioned, auditable, and fail-safe to read."""

    def test_round_trips_through_json(self, tmp_path):
        cfg = load_config()
        params = calibrate(_outcomes("standard", passes=1, fails=5), OPEN)
        path = calibration.save_params(params, cfg, tmp_path)
        assert path.exists()

        data = json.loads(path.read_text())
        assert data["schema_version"] == calibration.SCHEMA_VERSION
        assert data["strategy"] == V2

        loaded = calibration.load_params(cfg, tmp_path)
        assert loaded.heavy_threshold == params.heavy_threshold
        assert loaded.light_threshold == params.light_threshold
        assert loaded.strategy == params.strategy

    def test_missing_artifact_falls_back_to_heuristic_v1(self, tmp_path):
        assert calibration.load_params(load_config(), tmp_path) == HEURISTIC_V1

    def test_corrupt_artifact_falls_back_to_heuristic_v1(self, tmp_path):
        path = calibration.params_path(load_config(), tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json at all")
        assert calibration.load_params(load_config(), tmp_path) == HEURISTIC_V1

    def test_unknown_fields_are_ignored(self):
        params = CalibrationParams.from_dict(
            {"strategy": V2, "heavy_threshold": 3, "some_future_key": [1, 2]}
        )
        assert params.strategy == V2
        assert params.heavy_threshold == 3
        assert params.light_threshold == V1_LIGHT_THRESHOLD

    def test_calibrate_repo_reads_the_lesson_corpus(self, tmp_path):
        cfg = load_config()
        kb = KnowledgeBase.from_config(cfg, tmp_path)
        for i in range(12):
            kb.write_lesson(
                Lesson(
                    title=f"implement: task {i}",
                    outcome="fail",
                    kind="implement",
                    context="c",
                    what_happened="w",
                    lesson="l",
                    model="sonnet",
                    tier="standard",
                    created=f"2026-07-{i + 1:02d}",
                )
            )
        params = calibration.recalibrate(cfg, tmp_path)
        assert params.strategy == V2
        assert params.samples == 12
        # A tier that fails everything must not keep receiving the same work.
        assert params.heavy_threshold < V1_HEAVY_THRESHOLD
        assert calibration.params_path(cfg, tmp_path).exists()


class TestRepoCorpus:
    """The parameters this repo actually ships must be sane."""

    def test_current_corpus_calibrates_without_error(self):
        cfg = load_config()
        params = calibration.calibrate_repo(cfg, ".")
        assert params.strategy in (V1, V2)
        assert params.heavy_threshold - params.light_threshold >= 2
        assert params.samples >= 0

    def test_shipped_artifact_is_loadable_and_within_its_clamps(self):
        """Whatever is committed must be a usable, non-degenerate parameter set.

        Deliberately not asserting equality with a fresh calibration: every
        merged PR adds a lesson, and the artifact is refreshed once per block by
        `hsai cycle`, so the two are expected to drift within a block.
        """
        cfg = load_config()
        params = calibration.load_params(cfg, ".")
        settings = cfg.raw["models"]["calibration"]
        max_delta = settings["max_threshold_delta"]
        assert params.strategy in (V1, V2)
        assert abs(params.heavy_threshold - V1_HEAVY_THRESHOLD) <= max_delta
        assert abs(params.light_threshold - V1_LIGHT_THRESHOLD) <= max_delta
        assert params.heavy_threshold - params.light_threshold >= 2
