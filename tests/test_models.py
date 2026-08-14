from dataclasses import replace

from hsai.config import load_config
from hsai.models import ModelChoice, Task, escalate, select, select_reviewer


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


class TestEscalationLadder:
    """A retried ticket climbs the tier ladder instead of repeating itself."""

    def test_escalate_climbs_one_rung_per_attempt_saturating_at_heavy(self):
        assert escalate("light", 1) == "light"     # a first try has nothing to escalate
        assert escalate("light", 2) == "standard"
        assert escalate("light", 3) == "heavy"
        assert escalate("light", 4) == "heavy"      # saturates, does not overflow
        assert escalate("standard", 2) == "heavy"
        assert escalate("heavy", 2) == "heavy"       # already at the top

    def test_escalate_ignores_an_unconfigured_tier(self):
        assert escalate("nonexistent", 3) == "nonexistent"

    def test_second_attempt_resolves_one_tier_heavier_than_the_first(self):
        cfg = _cfg()
        base = Task(kind="implement", title="add a status subcommand", est_files=2)
        first = select(base, cfg)
        second = select(replace(base, attempt=2), cfg)
        assert first.tier == cfg.default_tier
        assert second.tier == escalate(first.tier, 2)
        assert second.tier != first.tier

    def test_escalation_saturates_at_heavy_and_stays_there(self):
        cfg = _cfg()
        heavy_task = Task(
            kind="improve", title="architecture: redesign the orchestrator",
            body="large refactor of concurrency model", est_files=10, attempt=3,
        )
        choice = select(heavy_task, cfg)
        assert choice.tier == "heavy"

    def test_rationale_names_the_escalation(self):
        cfg = _cfg()
        task = Task(kind="implement", title="add a status subcommand", est_files=2, attempt=2)
        choice = select(task, cfg)
        assert f"escalated {cfg.default_tier}->{choice.tier} on attempt 2" in choice.rationale

    def test_a_first_attempt_never_escalates(self):
        cfg = _cfg()
        task = Task(kind="implement", title="add a status subcommand", est_files=2, attempt=1)
        choice = select(task, cfg)
        assert "escalated" not in choice.rationale

    def test_prior_failure_text_can_shift_the_score_toward_heavy(self):
        cfg = _cfg()
        task = Task(
            kind="implement", title="add a status subcommand", est_files=2,
            prior_failure=(
                "a race condition and a concurrency bug in the security-sensitive "
                "migration path caused the test to flake"
            ),
        )
        choice = select(task, cfg)
        assert choice.tier == "heavy"


class TestBudgetGateWinsOverEscalation:
    """A soft breach demotes even when a retry would otherwise escalate; a hard
    breach means `select()` (and the model call it would drive) never runs at
    all - proven at the `_implementation_block` level in test_cycle.py."""

    def test_soft_breach_demotes_instead_of_escalating(self):
        cfg = _cfg()
        task = Task(kind="implement", title="add a status subcommand", est_files=2, attempt=2)
        escalated = select(task, cfg, demote=False)
        demoted = select(task, cfg, demote=True)

        # Without the breach, attempt 2 escalates past the default tier.
        assert escalated.tier == escalate(cfg.default_tier, 2)
        # Under a soft breach, the demotion wins: the tier ends up CHEAPER than
        # the (non-escalated) base tier, never heavier.
        from hsai.ledger import demote_tier

        assert demoted.tier == demote_tier(cfg.default_tier)
        assert demoted.tier != escalated.tier
        assert "demoted" in demoted.rationale and "wins over escalation" in demoted.rationale

    def test_demote_wins_even_on_a_ticket_that_would_saturate_at_heavy(self):
        cfg = _cfg()
        task = Task(
            kind="improve", title="architecture: redesign the orchestrator",
            body="large refactor", est_files=10, attempt=3,
        )
        demoted = select(task, cfg, demote=True)
        assert demoted.tier != "heavy"
