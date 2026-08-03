import json
from dataclasses import replace

from hsai.calibration import HEURISTIC_V1, V1, V2, CalibrationParams, calibrate
from hsai.config import load_config
from hsai.models import Task, select


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


class TestMachineReadableRationale:
    """The reason a tier was chosen must survive as data, not just prose."""

    def test_rationale_json_is_parseable_and_complete(self):
        cfg = _cfg()
        task = Task(kind="implement", title="docs: fix a typo", est_files=1)
        choice = select(task, cfg)
        data = json.loads(choice.rationale_json())
        assert data["tier"] == choice.tier
        assert data["model"] == choice.model
        assert data["strategy"] == choice.strategy
        assert data["score"] == choice.score
        assert data["attempt"] == 1
        assert data["signals"] == list(choice.signals)

    def test_signals_name_the_rule_that_fired(self):
        cfg = _cfg()
        assert "label:size:L" in select(
            Task(kind="implement", title="feat: big thing", labels=("size:L",)), cfg
        ).signals
        assert "default-tier" in select(
            Task(kind="implement", title="add a status subcommand", est_files=2), cfg
        ).signals
        assert "budget-demotion" in select(
            Task(kind="implement", title="feat: big thing", labels=("size:L",)),
            cfg,
            demote=True,
        ).signals

    def test_rationale_is_stable_across_identical_selections(self):
        cfg = _cfg()
        task = Task(kind="heal", title="ci: main is red - auto-heal", est_files=3)
        first = select(task, cfg)
        second = select(task, cfg)
        assert first.rationale_json() == second.rationale_json()
        assert first.rationale == second.rationale


class TestEscalateOnRetry:
    """A retry must not be spent on the tier that already failed the ticket."""

    def test_retry_selects_a_strictly_higher_tier(self):
        cfg = _cfg()
        task = Task(kind="implement", title="add a status subcommand", est_files=2)
        first = select(task, cfg, attempt=1)
        retry = select(task, cfg, attempt=2)
        assert first.tier == "standard"
        assert retry.tier == "heavy"
        assert retry.model == cfg.tiers["heavy"].model

    def test_light_task_escalates_to_standard_then_heavy(self):
        cfg = _cfg()
        task = Task(kind="implement", title="docs: update README", est_files=1)
        assert select(task, cfg, attempt=1).tier == "light"
        assert select(task, cfg, attempt=2).tier == "standard"
        assert select(task, cfg, attempt=3).tier == "heavy"

    def test_escalation_is_recorded_in_the_rationale(self):
        cfg = _cfg()
        task = Task(kind="implement", title="add a status subcommand", est_files=2)
        choice = select(task, cfg, attempt=2)
        assert "retry-escalation:attempt=2" in choice.signals
        assert "escalated standard->heavy" in choice.rationale
        assert choice.attempt == 2

    def test_escalation_caps_at_the_heaviest_tier(self):
        cfg = _cfg()
        task = Task(
            kind="improve", title="architecture: redesign the orchestrator", est_files=10
        )
        choice = select(task, cfg, attempt=3)
        assert choice.tier == "heavy"
        assert "retry-escalation:capped-at-heavy" in choice.signals

    def test_escalation_can_be_switched_off(self):
        cfg = _cfg()
        params = replace(HEURISTIC_V1, escalate_on_retry=False)
        task = Task(kind="implement", title="add a status subcommand", est_files=2)
        assert select(task, cfg, attempt=2, params=params).tier == "standard"

    def test_budget_demotion_outranks_retry_escalation(self):
        """A quota ceiling is a hard constraint; a retry is only a preference."""
        cfg = _cfg()
        task = Task(kind="implement", title="add a status subcommand", est_files=2)
        choice = select(task, cfg, attempt=2, demote=True)
        assert choice.tier == "standard"
        assert "retry-escalation:attempt=2" in choice.signals
        assert "budget-demotion" in choice.signals


class TestCalibratedParams:
    """Learned thresholds must actually change what gets selected."""

    def test_default_params_are_heuristic_v1(self):
        cfg = _cfg()
        choice = select(Task(kind="implement", title="add feature"), cfg)
        assert choice.strategy == V1

    def test_lowered_heavy_threshold_routes_more_work_to_heavy(self):
        cfg = _cfg()
        task = Task(kind="improve", title="refactor: split large function", est_files=3)
        assert select(task, cfg).tier == "standard"

        calibrated = CalibrationParams(strategy=V2, heavy_threshold=3, light_threshold=-3)
        choice = select(task, cfg, params=calibrated)
        assert choice.tier == "heavy"
        assert choice.strategy == V2
        assert "score>=3" in choice.signals

    def test_narrowed_light_band_keeps_work_off_the_cheapest_tier(self):
        cfg = _cfg()
        task = Task(kind="implement", title="fix: typo in error message", est_files=1)
        assert select(task, cfg).tier == "light"

        calibrated = CalibrationParams(strategy=V2, heavy_threshold=5, light_threshold=-4)
        assert select(task, cfg, params=calibrated).tier == "standard"

    def test_sparse_calibration_selects_identically_to_heuristic_v1(self):
        """A corpus too thin to learn from must reproduce v1 exactly."""
        cfg = _cfg()
        sparse = calibrate([], cfg.raw["models"]["calibration"])
        assert sparse.strategy == V1
        assert sparse.heavy_threshold == HEURISTIC_V1.heavy_threshold
        assert sparse.light_threshold == HEURISTIC_V1.light_threshold

        tasks = [
            Task(kind="implement", title="docs: update README", est_files=1),
            Task(kind="implement", title="add a status subcommand", est_files=2),
            Task(kind="heal", title="security: fix authentication bypass", est_files=5),
            Task(kind="improve", title="feat: big thing", labels=("size:L",)),
        ]
        for task in tasks:
            baseline = select(task, cfg)
            fallback = select(task, cfg, params=sparse)
            assert fallback.tier == baseline.tier
            assert fallback.score == baseline.score
            assert fallback.signals == baseline.signals
            assert fallback.strategy == V1


class TestDryRunSelectionPath:
    """The three orchestrator task kinds must each produce a stable rationale."""

    def test_heal_implement_improve_all_record_a_rationale(self):
        cfg = _cfg()
        tasks = {
            "heal": Task(kind="heal", title="ci: main is red - auto-heal", est_files=2),
            "implement": Task(
                kind="implement",
                title="skill: learned model-selection heuristic-v2",
                labels=("size:L",),
                est_files=4,
            ),
            "improve": Task(
                kind="improve",
                title="chore: refresh reference-set snapshot and extract one practice",
                est_files=2,
            ),
        }
        for kind, task in tasks.items():
            for attempt in (1, 2):
                choice = select(task, cfg, attempt=attempt)
                assert choice.tier in cfg.tiers, kind
                assert choice.rationale.strip()
                assert choice.signals
                assert json.loads(choice.rationale_json())["attempt"] == attempt
