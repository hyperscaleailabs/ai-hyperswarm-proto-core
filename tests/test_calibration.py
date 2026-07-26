"""Tests for the learned model-selection calibration (heuristic-v2)."""
from hsai.calibration import (
    V1_HEAVY_THRESHOLD,
    V1_LIGHT_THRESHOLD,
    CalibrationConfig,
    CalibrationParams,
    Outcome,
    _tier_shares,
    calibrate,
    load_params,
    model_to_tier,
    outcomes_from_records,
)
from hsai.config import load_config
from hsai.knowledge import KnowledgeBase, Lesson, LessonRecord


def _cfg():
    return load_config()


def _outcomes(**counts):
    """Build an outcome list from ``tier=(passes, fails)`` kwargs."""
    out = []
    for tier, (passes, fails) in counts.items():
        out.extend(Outcome(tier=tier, passed=True) for _ in range(passes))
        out.extend(Outcome(tier=tier, passed=False) for _ in range(fails))
    return out


class TestParsingLessonOutcomes:
    """Mining structured outcome signals out of lesson notes."""

    def test_lesson_records_carry_the_model_alias(self, tmp_path):
        kb = KnowledgeBase(tmp_path)
        kb.write_lesson(
            Lesson(
                title="implement: add thing",
                outcome="pass",
                kind="implement",
                context="c",
                what_happened="w",
                lesson="l",
                model="sonnet",
            )
        )
        (record,) = kb.read_lessons()
        assert record.model == "sonnet"

    def test_outcomes_map_model_to_tier(self):
        cfg = _cfg()
        records = [
            LessonRecord("n1", "t", "pass", "implement", (), "", model="haiku"),
            LessonRecord("n2", "t", "fail", "implement", (), "", model="sonnet"),
            LessonRecord("n3", "t", "pass", "improve", (), "", model="opus"),
        ]
        outs = outcomes_from_records(records, cfg)
        assert {(o.tier, o.passed) for o in outs} == {
            ("light", True),
            ("standard", False),
            ("heavy", True),
        }

    def test_unlabeled_and_human_lessons_are_ignored(self):
        cfg = _cfg()
        records = [
            LessonRecord("n1", "t", "unknown", "implement", (), "", model="sonnet"),
            LessonRecord("n2", "t", "pass", "improve", (), "", model="n/a (human)"),
            LessonRecord("n3", "t", "pass", "implement", (), "", model=""),
        ]
        assert outcomes_from_records(records, cfg) == []

    def test_model_to_tier_inverts_configured_tiers(self):
        cfg = _cfg()
        assert model_to_tier(cfg) == {"haiku": "light", "sonnet": "standard", "opus": "heavy"}


class TestSparseDataFallback:
    """Below the min-sample gate, v2 must equal v1 verbatim."""

    def test_empty_corpus_falls_back_to_v1(self):
        cfg = _cfg()
        params = calibrate([], cfg)
        assert params.strategy == "heuristic-v1"
        assert params.heavy_threshold == V1_HEAVY_THRESHOLD
        assert params.light_threshold == V1_LIGHT_THRESHOLD

    def test_below_min_labeled_falls_back_to_v1(self):
        cfg = _cfg()
        cal = CalibrationConfig(min_labeled=5)
        params = calibrate(_outcomes(standard=(2, 2)), cfg, cal)  # 4 < 5
        assert params.strategy == "heuristic-v1"
        assert (params.heavy_threshold, params.light_threshold) == (
            V1_HEAVY_THRESHOLD,
            V1_LIGHT_THRESHOLD,
        )

    def test_fallback_params_are_v1_defaults(self):
        params = CalibrationParams.fallback()
        assert params.strategy == "heuristic-v1"
        assert params.heavy_threshold == V1_HEAVY_THRESHOLD
        assert params.light_threshold == V1_LIGHT_THRESHOLD


class TestCalibrationMath:
    """Derived thresholds shift in the expected direction from success rates."""

    def test_struggling_standard_lowers_heavy_threshold(self):
        cfg = _cfg()
        cal = CalibrationConfig(min_labeled=5, min_per_tier=3, max_delta=2)
        # Standard tier fails often -> escalate more work to heavy -> lower bar.
        params = calibrate(_outcomes(standard=(1, 5)), cfg, cal)
        assert params.strategy == "heuristic-v2"
        assert params.heavy_threshold < V1_HEAVY_THRESHOLD

    def test_reliable_standard_raises_heavy_threshold(self):
        cfg = _cfg()
        cal = CalibrationConfig(min_labeled=5, min_per_tier=3, max_delta=2)
        # Standard rarely fails -> keep more work on standard -> raise the bar.
        params = calibrate(_outcomes(standard=(8, 0)), cfg, cal)
        assert params.heavy_threshold > V1_HEAVY_THRESHOLD

    def test_struggling_light_lowers_light_threshold(self):
        cfg = _cfg()
        cal = CalibrationConfig(min_labeled=5, min_per_tier=3, max_delta=2)
        # Light tier fails often -> fewer tasks routed light -> stricter bar.
        params = calibrate(_outcomes(light=(1, 5)), cfg, cal)
        assert params.light_threshold < V1_LIGHT_THRESHOLD

    def test_reliable_light_raises_light_threshold(self):
        cfg = _cfg()
        cal = CalibrationConfig(min_labeled=5, min_per_tier=3, max_delta=2)
        params = calibrate(_outcomes(light=(8, 0)), cfg, cal)
        assert params.light_threshold > V1_LIGHT_THRESHOLD

    def test_deltas_are_clamped_to_max_delta(self):
        cfg = _cfg()
        cal = CalibrationConfig(min_labeled=5, min_per_tier=3, max_delta=2, delta_scale=100)
        params = calibrate(_outcomes(standard=(0, 6)), cfg, cal)
        # Even a total wipeout cannot move the threshold beyond max_delta.
        assert params.heavy_threshold == V1_HEAVY_THRESHOLD - cal.max_delta

    def test_under_sampled_tier_holds_its_boundary_at_v1(self):
        cfg = _cfg()
        cal = CalibrationConfig(min_labeled=5, min_per_tier=3)
        # Enough labeled lessons overall (heavy carries the count), but standard
        # itself is under-sampled, so the heavy boundary must not move.
        params = calibrate(_outcomes(standard=(0, 2), heavy=(5, 0)), cfg, cal)
        assert params.strategy == "heuristic-v2"
        assert params.heavy_threshold == V1_HEAVY_THRESHOLD

    def test_notes_record_the_audit_trail(self):
        cfg = _cfg()
        cal = CalibrationConfig(min_labeled=5, min_per_tier=3)
        params = calibrate(_outcomes(standard=(1, 5)), cfg, cal)
        assert any("heavy_threshold" in n for n in params.notes)


class TestShareClamp:
    """No single tier may exceed a configured share without its min-sample gate."""

    def test_biased_undersampled_heavy_is_clamped_below_max_share(self):
        cfg = _cfg()
        # Aggressive movement + low share ceiling would push almost everything to
        # heavy; heavy itself is under-sampled, so the clamp must reel it back.
        cal = CalibrationConfig(
            min_labeled=5, min_per_tier=3, max_delta=6, max_share=0.35, delta_scale=100
        )
        params = calibrate(_outcomes(standard=(0, 8)), cfg, cal)
        share = _tier_shares(params.heavy_threshold, params.light_threshold)
        assert share["heavy"] <= cal.max_share
        assert any("share clamp" in n for n in params.notes)

    def test_well_sampled_heavy_may_exceed_share(self):
        cfg = _cfg()
        # Same aggressive movement, but heavy now meets its min-sample gate, so it
        # has earned the right to take a large share - no clamp.
        cal = CalibrationConfig(
            min_labeled=5, min_per_tier=3, max_delta=6, max_share=0.35, delta_scale=100
        )
        params = calibrate(_outcomes(standard=(0, 8), heavy=(4, 0)), cfg, cal)
        share = _tier_shares(params.heavy_threshold, params.light_threshold)
        assert share["heavy"] > cal.max_share
        assert not any("share clamp" in n for n in params.notes)


class TestLoadParamsFromDisk:
    """The disk-backed entry point mines the real lesson corpus."""

    def test_load_params_on_empty_vault_is_v1(self, tmp_path):
        cfg = _cfg()
        params = load_params(cfg, tmp_path)
        assert params.strategy == "heuristic-v1"

    def test_load_params_reads_written_lessons(self, tmp_path):
        cfg = _cfg()
        kb = KnowledgeBase(tmp_path)
        for i in range(6):
            kb.write_lesson(
                Lesson(
                    title=f"implement: task {i}",
                    outcome="fail",
                    kind="implement",
                    context="c",
                    what_happened="w",
                    lesson="l",
                    model="sonnet",
                )
            )
        params = load_params(cfg, tmp_path)
        assert params.strategy == "heuristic-v2"
        assert params.n_labeled == 6
        assert params.heavy_threshold < V1_HEAVY_THRESHOLD
