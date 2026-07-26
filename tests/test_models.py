from hsai.calibration import CalibrationParams
from hsai.config import load_config
from hsai.models import Task, select

# Tier ordering, lightest to heaviest, for "strictly higher" assertions.
_RANK = {"light": 0, "standard": 1, "heavy": 2}


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


class TestEscalateOnRetry:
    """A retried ticket (attempt > 1) must escalate one tier per prior failure."""

    def test_retry_selects_strictly_higher_tier(self):
        cfg = _cfg()
        task = Task(kind="implement", title="add a status subcommand", est_files=2)
        first = select(task, cfg, attempt=1)
        retry = select(task, cfg, attempt=2)
        assert first.tier == "standard"
        assert _RANK[retry.tier] > _RANK[first.tier]
        assert retry.tier == "heavy"
        assert retry.signal == "escalate-retry"

    def test_light_task_escalates_step_by_step(self):
        cfg = _cfg()
        task = Task(kind="implement", title="docs: update README", est_files=1)
        assert select(task, cfg, attempt=1).tier == "light"
        assert select(task, cfg, attempt=2).tier == "standard"
        assert select(task, cfg, attempt=3).tier == "heavy"

    def test_retry_rationale_names_the_escalation(self):
        cfg = _cfg()
        task = Task(kind="implement", title="add a status subcommand", est_files=2)
        choice = select(task, cfg, attempt=2)
        assert "escalate-on-retry" in choice.rationale

    def test_heavy_task_cannot_escalate_past_heavy(self):
        cfg = _cfg()
        task = Task(
            kind="heal",
            title="architecture: redesign the orchestrator core",
            est_files=10,
        )
        assert select(task, cfg, attempt=1).tier == "heavy"
        assert select(task, cfg, attempt=3).tier == "heavy"

    def test_first_attempt_is_never_escalated(self):
        cfg = _cfg()
        task = Task(kind="implement", title="add a status subcommand", est_files=2)
        choice = select(task, cfg, attempt=1)
        assert choice.signal != "escalate-retry"


class TestCalibratedParamsAndSignal:
    """select() honors learned params and exposes a machine-readable signal."""

    def test_signal_is_populated_for_every_branch(self):
        cfg = _cfg()
        cases = {
            "size-label": Task(kind="implement", title="x", labels=("size:L",)),
            "score-light": Task(kind="implement", title="docs: readme", est_files=1),
            "score-heavy": Task(kind="heal", title="architecture redesign", est_files=10),
            "default": Task(kind="implement", title="add a widget", est_files=2),
        }
        for expected, task in cases.items():
            assert select(task, cfg).signal == expected

    def test_calibrated_heavy_threshold_shifts_selection(self):
        cfg = _cfg()
        task = Task(kind="implement", title="add a caching helper", est_files=4)
        # Under v1 (heavy_threshold=5) this scores standard; a calibrated lower
        # threshold routes the very same task to heavy.
        assert select(task, cfg).tier == "standard"
        lowered = CalibrationParams(
            heavy_threshold=1, light_threshold=-3, strategy="heuristic-v2"
        )
        assert select(task, cfg, params=lowered).tier == "heavy"

    def test_strategy_reflects_supplied_params(self):
        cfg = _cfg()
        task = Task(kind="implement", title="add feature", est_files=2)
        v2 = CalibrationParams(heavy_threshold=5, light_threshold=-3, strategy="heuristic-v2")
        assert select(task, cfg, params=v2).strategy == "heuristic-v2"
        assert select(task, cfg).strategy == "heuristic-v1"
