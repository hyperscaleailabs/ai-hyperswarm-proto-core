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
