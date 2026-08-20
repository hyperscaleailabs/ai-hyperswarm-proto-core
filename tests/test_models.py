from dataclasses import replace

import pytest

from hsai.config import load_config
from hsai.models import (
    V1,
    V1_THRESHOLDS,
    V2,
    ModelChoice,
    Task,
    Thresholds,
    active_strategy,
    calibrated_thresholds,
    features,
    select,
    select_reviewer,
    tier_for,
)


def _cfg():
    return load_config()


class TestLightTier:
    """Tasks that should select light tier (score <= -3)."""

    def test_docs_task_selects_light(self):
        cfg = _cfg()
        task = Task(
            kind="implement", title="docs: update README", labels=("documentation",)
        )
        choice = select(task, cfg)
        assert choice.tier == "light"
        assert choice.model == cfg.tiers["light"].model

    def test_typo_fix_selects_light(self):
        cfg = _cfg()
        task = Task(kind="implement", title="fix: typo in error message", est_files=1)
        choice = select(task, cfg)
        assert choice.tier == "light"

    def test_formatting_change_selects_light(self):
        cfg = _cfg()
        task = Task(
            kind="implement",
            title="chore: format code with black",
            labels=("lint",),
            est_files=1,
        )
        choice = select(task, cfg)
        assert choice.tier == "light"

    def test_single_file_comment_selects_light(self):
        cfg = _cfg()
        task = Task(
            kind="implement",
            title="docs: add clarifying comments",
            est_files=1,
            body="improve inline documentation",
        )
        choice = select(task, cfg)
        assert choice.tier == "light"


class TestStandardTier:
    """Tasks that should select standard tier (-3 < score < 5)."""

    def test_ordinary_feature_uses_default_tier(self):
        cfg = _cfg()
        task = Task(kind="implement", title="add a status subcommand", est_files=2)
        choice = select(task, cfg)
        assert choice.tier == cfg.default_tier

    def test_small_bugfix_selects_standard(self):
        cfg = _cfg()
        task = Task(
            kind="implement",
            title="fix: handle edge case in parser",
            est_files=3,
        )
        choice = select(task, cfg)
        assert choice.tier == "standard"

    def test_feature_with_tests_selects_standard(self):
        cfg = _cfg()
        task = Task(
            kind="implement",
            title="implement: add caching layer",
            est_files=4,
            body="optimize performance with in-memory cache",
        )
        choice = select(task, cfg)
        assert choice.tier == "standard"

    def test_minor_refactor_selects_standard(self):
        cfg = _cfg()
        task = Task(
            kind="improve",
            title="refactor: split large function",
            est_files=3,
        )
        choice = select(task, cfg)
        assert choice.tier == "standard"


class TestHeavyTier:
    """Tasks that should select heavy tier (score >= 5)."""

    def test_architecture_task_selects_heavy(self):
        cfg = _cfg()
        task = Task(
            kind="improve",
            title="architecture: redesign the orchestrator",
            body="large refactor of concurrency model",
            est_files=10,
        )
        choice = select(task, cfg)
        assert choice.tier == "heavy"

    def test_security_issue_selects_heavy(self):
        cfg = _cfg()
        task = Task(
            kind="heal",
            title="security: fix authentication bypass",
            body="Critical vulnerability in token validation",
            est_files=5,
        )
        choice = select(task, cfg)
        assert choice.tier == "heavy"

    def test_large_refactor_selects_heavy(self):
        cfg = _cfg()
        task = Task(
            kind="improve",
            title="large refactor: redesign data model",
            est_files=8,
        )
        choice = select(task, cfg)
        assert choice.tier == "heavy"

    def test_migration_selects_heavy(self):
        cfg = _cfg()
        task = Task(
            kind="improve",
            title="migration: upgrade to new framework",
            body="Breaking changes to core APIs",
            est_files=9,
        )
        choice = select(task, cfg)
        assert choice.tier == "heavy"

    def test_hard_bug_with_files_selects_heavy(self):
        cfg = _cfg()
        task = Task(
            kind="heal",
            title="hard bug: race condition in worker pool",
            est_files=6,
        )
        choice = select(task, cfg)
        assert choice.tier == "heavy"


class TestModelChoiceMetadata:
    """Verify ModelChoice records correct metadata."""

    def test_choice_records_rationale_and_strategy(self):
        cfg = _cfg()
        choice = select(Task(kind="implement", title="add feature"), cfg)
        assert "score=" in choice.rationale
        assert choice.strategy == "heuristic-v1"

    def test_rationale_includes_tier_reason(self):
        cfg = _cfg()
        task = Task(kind="implement", title="docs: update README", est_files=1)
        choice = select(task, cfg)
        assert "low-complexity" in choice.rationale or "docs" in choice.rationale.lower()

    def test_tier_maps_to_configured_model(self):
        cfg = _cfg()
        task = Task(kind="implement", title="feature", est_files=2)
        choice = select(task, cfg)
        assert choice.model == cfg.tiers[choice.tier].model


class TestEdgeCases:
    """Verify behavior on edge cases."""

    def test_empty_labels_doesnt_crash(self):
        cfg = _cfg()
        task = Task(kind="implement", title="add feature", labels=())
        choice = select(task, cfg)
        assert choice.tier in ("light", "standard", "heavy")

    def test_zero_files_edge_case(self):
        cfg = _cfg()
        task = Task(kind="implement", title="add feature", est_files=0)
        choice = select(task, cfg)
        assert choice.tier in ("light", "standard", "heavy")

    def test_many_signals_balanced(self):
        cfg = _cfg()
        task = Task(
            kind="implement",
            title="refactor: redesign architecture",
            body="large migration effort",
            est_files=15,
            labels=("architecture", "breaking"),
        )
        choice = select(task, cfg)
        assert choice.tier == "heavy"

    def test_conflicting_signals_favor_structural(self):
        cfg = _cfg()
        task = Task(
            kind="implement",
            title="docs: update README with security notes",
            est_files=8,
            labels=("security",),
        )
        choice = select(task, cfg)
        assert choice.tier == "standard"


class TestReviewerSelection:
    """The reviewer must never be the author grading its own work."""

    def test_reviewer_tier_always_differs_from_the_author(self):
        cfg = _cfg()
        for tier in ("heavy", "standard", "light"):
            author = ModelChoice(tier=tier, model=cfg.tiers[tier].model, rationale="x")
            reviewer = select_reviewer(author, cfg)
            assert reviewer.tier != author.tier, tier
            assert reviewer.tier in cfg.tiers
            assert reviewer.model == cfg.tiers[reviewer.tier].model
            assert reviewer.model != author.model
            assert reviewer.strategy == "reviewer-v1"
            assert tier in reviewer.rationale       # auditable: who reviewed whom

    def test_default_policy_keeps_the_gate_affordable(self):
        cfg = _cfg()
        heavy = ModelChoice(tier="heavy", model=cfg.tiers["heavy"].model, rationale="x")
        # A heavy author is reviewed one tier down, not by another heavy run:
        # the gate fires on every change, so it must not eat the heavy budget.
        assert select_reviewer(heavy, cfg).tier == "standard"

    def test_a_self_referential_policy_is_discarded(self):
        cfg = replace(_cfg(), review={"tier_policy": {"standard": "standard"}})
        author = ModelChoice(tier="standard", model="sonnet", rationale="x")
        reviewer = select_reviewer(author, cfg)
        assert reviewer.tier != "standard"
        assert "no usable policy" in reviewer.rationale

    def test_an_unconfigured_policy_target_falls_back_to_an_adjacent_tier(self):
        cfg = replace(_cfg(), review={"tier_policy": {"light": "nonexistent"}})
        author = ModelChoice(tier="light", model="haiku", rationale="x")
        reviewer = select_reviewer(author, cfg)
        assert reviewer.tier in cfg.tiers and reviewer.tier != "light"


# --- outcome-calibrated routing (heuristic-v2) + shadow evaluation ------------

# (task, tier heuristic-v1 has always produced). The parity contract: with no
# models.calibration block, select() must still return exactly these.
_V1_TABLE = (
    (Task(kind="implement", title="docs: update README", labels=("documentation",)), "light"),
    (Task(kind="implement", title="fix: typo in error message", est_files=1), "light"),
    (Task(kind="implement", title="add a status subcommand", est_files=2), "standard"),
    (Task(kind="improve", title="refactor: split large function", est_files=3), "standard"),
    (Task(kind="heal", title="security: fix authentication bypass", est_files=5), "heavy"),
    (Task(kind="improve", title="migration: upgrade to new framework", est_files=9), "heavy"),
    (Task(kind="implement", title="feat: a thing", labels=("size:L",)), "heavy"),
    (Task(kind="implement", title="feat: a thing", labels=("size:M",)), "standard"),
)


def _task_id(value):
    """Readable parametrise ids (a Task by title, anything else verbatim)."""
    return value.title if isinstance(value, Task) else str(value)


def _with_models(cfg, **overrides):
    """A config whose ``models:`` block carries ``overrides`` (tiers untouched)."""
    return replace(cfg, models={**cfg.models, **overrides})


class TestCalibrationAbsentIsV1Parity:
    """No calibration block => nothing about routing changes. The whole point."""

    def test_the_shipped_config_pins_v1_and_has_no_calibration(self):
        cfg = _cfg()
        assert active_strategy(cfg) == V1
        assert calibrated_thresholds(cfg)[0] is None
        assert "no models.calibration block" in calibrated_thresholds(cfg)[1]

    @pytest.mark.parametrize("task,expected", _V1_TABLE, ids=_task_id)
    def test_tier_is_unchanged_and_the_shadow_agrees(self, task, expected):
        choice = select(task, _cfg())
        assert choice.tier == expected
        assert choice.strategy == V1
        # v2 falls back to the v1 thresholds, so it cannot disagree.
        assert choice.shadow_strategy == V2
        assert choice.shadow_tier == expected
        assert choice.shadow_disagrees is False

    @pytest.mark.parametrize("task,expected", _V1_TABLE, ids=_task_id)
    def test_a_malformed_calibration_block_also_falls_back(self, task, expected):
        # Garbage in core.yaml must degrade to v1, never to a guess.
        for bad in ({"thresholds": "nope"}, {"version": 2}, "not-a-mapping"):
            cfg = _with_models(_cfg(), calibration=bad)
            assert calibrated_thresholds(cfg)[0] is None
            assert select(task, cfg).tier == expected


class TestShadowEvaluation:
    """Both strategies always run; only the pinned one routes."""

    # score=3 under the shared scoring function: standard at v1's heavy>=5,
    # heavy the moment a calibration lowers that threshold to 3.
    TASK = Task(kind="improve", title="refactor: split large function", est_files=3)
    CALIBRATION = {
        "version": 1,
        "thresholds": {"heavy": 3, "light": -3},
        "fitted_at": "2026-08-20",
        "sample_size": 42,
        "rationale": "synthetic",
    }

    def test_the_task_scores_where_the_test_claims_it_does(self):
        assert features(self.TASK).complexity_score == 3

    def test_v2_shifts_the_task_but_only_in_the_shadow_while_v1_is_pinned(self):
        cfg = _with_models(_cfg(), calibration=self.CALIBRATION)
        choice = select(self.TASK, cfg)

        assert choice.tier == "standard"           # active routing is untouched
        assert choice.model == cfg.tiers["standard"].model
        assert choice.strategy == V1
        assert choice.shadow_strategy == V2
        assert choice.shadow_tier == "heavy"       # ...but v2 would have gone heavy
        assert choice.shadow_disagrees is True

    def test_pinning_v2_promotes_the_fitted_thresholds_and_shadows_v1(self):
        cfg = _with_models(
            _cfg(), calibration=self.CALIBRATION, selection_strategy=V2
        )
        choice = select(self.TASK, cfg)

        assert choice.tier == "heavy"
        assert choice.model == cfg.tiers["heavy"].model
        assert choice.strategy == V2
        assert choice.shadow_strategy == V1
        assert choice.shadow_tier == "standard"
        assert choice.shadow_disagrees is True

    def test_an_unknown_pinned_strategy_falls_back_to_v1(self):
        cfg = _with_models(_cfg(), selection_strategy="heuristic-v99")
        assert active_strategy(cfg) == V1
        assert select(self.TASK, cfg).strategy == V1

    def test_calibrated_thresholds_are_parsed_and_described(self):
        cfg = _with_models(_cfg(), calibration=self.CALIBRATION)
        thresholds, why = calibrated_thresholds(cfg)
        assert thresholds == Thresholds(heavy=3, light=-3)
        assert "n=42" in why


class TestRoutingFeatures:
    """The features the ledger records, so a fit can replay the decision."""

    def test_features_capture_every_input_to_the_score(self):
        task = Task(
            kind="heal",
            title="security: fix a race condition",
            body="architecture change",
            est_files=9,
            labels=("size:L",),
        )
        feats = features(task)
        assert feats.kind == "heal"
        assert feats.est_files == 9
        assert feats.size_label == "L"
        # security, race condition, architecture - three distinct heavy signals.
        assert feats.heavy_signals == 3
        assert feats.light_signals == 0
        assert select(task, _cfg()).features == feats

    def test_tier_for_is_the_shared_rule_both_strategies_apply(self):
        feats = features(Task(kind="improve", title="refactor: x", est_files=3))
        assert tier_for(feats, V1_THRESHOLDS, "standard")[0] == "standard"
        assert tier_for(feats, Thresholds(heavy=3, light=-3), "standard")[0] == "heavy"

    def test_a_soft_budget_breach_demotes_both_strategies_and_is_recorded(self):
        cfg = _cfg()
        task = Task(kind="improve", title="migration: upgrade framework", est_files=9)
        plain = select(task, cfg)
        demoted = select(task, cfg, demote=True)

        assert plain.tier == "heavy" and plain.demoted is False
        assert demoted.tier == "standard" and demoted.demoted is True
        # The shadow follows the same gate - it is orthogonal to the strategy.
        assert demoted.shadow_tier == "standard"
