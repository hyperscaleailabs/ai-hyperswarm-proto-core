import re
from dataclasses import fields, replace

from hsai import policy as policy_mod
from hsai.config import load_config
from hsai.models import LIGHT_MAX_FILES, Task, _score, decide_tier, select


def _cfg():
    return load_config()


def _policy():
    """The policy the repo actually ships (not the in-code fallback)."""
    return policy_mod.load_policy()


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
        # heuristic-v2 names the policy version that produced the routing, so
        # every PR body records which committed policy routed it.
        assert choice.strategy.startswith("heuristic-v2")
        assert f"policy v{_policy().version}" in choice.strategy

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


# --- golden fixture: the committed policy must reproduce heuristic-v1 ---------
# A frozen, verbatim copy of heuristic-v1's constants and scoring, kept here as
# the golden reference. The committed .ai-swarm/selection-policy.json must
# reproduce its tier decisions exactly; if a calibration ever changes the file,
# THIS test is what proves the change was deliberate.
_V1_HEAVY_SIGNALS = (
    "architecture", "redesign", "large refactor", "rearchitect", "hard bug",
    "race condition", "concurrency", "security", "design", "migration",
    "refactor", "breaking",
)
_V1_LIGHT_SIGNALS = (
    "typo", "docs", "documentation", "readme", "format", "lint", "rename",
    "comment", "index", "chore", "bump", "whitespace",
)


def _v1_score(task: Task) -> int:
    text = f"{task.title}\n{task.body}\n{' '.join(task.labels)}".lower()
    score = 0
    for w in _V1_HEAVY_SIGNALS:
        if w in text:
            score += 2
    for w in _V1_LIGHT_SIGNALS:
        if w in text:
            score -= 2
    if task.est_files >= 8:
        score += 3
    elif task.est_files >= 4:
        score += 1
    elif task.est_files >= 2:
        score += 0
    else:
        score -= 1
    if task.kind == "heal":
        score += 2
    elif task.kind == "improve":
        score += 1
    if re.search(r"\b(doc|docs|readme|comment)\b", text) and task.est_files <= 1:
        score -= 1
    return score


def _v1_tier(task: Task, default_tier: str) -> str:
    score = _v1_score(task)
    if "size:L" in task.labels:
        return "heavy"
    if "size:M" in task.labels:
        return default_tier
    if score >= 5:
        return "heavy"
    if score <= -3 and task.est_files <= 2:
        return "light"
    return default_tier


# Every task exercised elsewhere in this file, plus deliberate boundary cases.
FIXTURE_TASKS = [
    Task(kind="implement", title="docs: update README", labels=("documentation",)),
    Task(kind="implement", title="fix: typo in error message", est_files=1),
    Task(kind="implement", title="chore: format code with black", labels=("lint",), est_files=1),
    Task(kind="implement", title="docs: add clarifying comments", est_files=1,
         body="improve inline documentation"),
    Task(kind="implement", title="add a status subcommand", est_files=2),
    Task(kind="implement", title="fix: handle edge case in parser", est_files=3),
    Task(kind="implement", title="implement: add caching layer", est_files=4,
         body="optimize performance with in-memory cache"),
    Task(kind="improve", title="refactor: split large function", est_files=3),
    Task(kind="improve", title="architecture: redesign the orchestrator",
         body="large refactor of concurrency model", est_files=10),
    Task(kind="heal", title="security: fix authentication bypass",
         body="Critical vulnerability in token validation", est_files=5),
    Task(kind="improve", title="large refactor: redesign data model", est_files=8),
    Task(kind="improve", title="migration: upgrade to new framework",
         body="Breaking changes to core APIs", est_files=9),
    Task(kind="heal", title="hard bug: race condition in worker pool", est_files=6),
    Task(kind="implement", title="add feature"),
    Task(kind="implement", title="add feature", labels=()),
    Task(kind="implement", title="add feature", est_files=0),
    Task(kind="implement", title="refactor: redesign architecture",
         body="large migration effort", est_files=15, labels=("architecture", "breaking")),
    Task(kind="implement", title="docs: update README with security notes", est_files=8,
         labels=("security",)),
    # Boundary cases the suite above does not cover.
    Task(kind="implement", title="feat: add a queue", labels=("size:L",)),
    Task(kind="implement", title="docs: fix a typo", labels=("size:L",), est_files=1),
    Task(kind="implement", title="docs: fix a typo", labels=("size:M",), est_files=1),
    Task(kind="heal", title="ci: main is red - auto-heal", body="pytest failed"),
    Task(kind="improve", title="chore: refresh reference-set snapshot", est_files=2),
    Task(kind="implement", title="security: fix a concurrency migration", est_files=1),
    Task(kind="implement", title="docs: rename a readme comment", est_files=3),
    Task(kind="implement", title="rename a variable", est_files=7),
]


class TestCommittedPolicyIsGolden:
    """The shipped policy file must be heuristic-v1, exactly."""

    def test_default_policy_matches_frozen_v1_constants(self):
        p = _policy()
        assert p.heavy_signals == _V1_HEAVY_SIGNALS
        assert p.light_signals == _V1_LIGHT_SIGNALS
        assert (p.heavy_signal_weight, p.light_signal_weight) == (2, -2)
        assert (p.heavy_threshold, p.light_threshold) == (5, -3)
        assert p.file_buckets == ((8, 3), (4, 1), (2, 0), (0, -1))
        assert p.kind_weights == {"heal": 2, "improve": 1}
        assert p.narrow_docs_delta == -1

    def test_committed_file_reproduces_v1_decisions_on_the_fixture_suite(self):
        cfg = _cfg()
        p = _policy()
        for task in FIXTURE_TASKS:
            assert _score(task, p) == _v1_score(task), f"score drift on {task.title!r}"
            assert select(task, cfg, policy=p).tier == _v1_tier(task, cfg.default_tier), (
                f"tier drift on {task.title!r}"
            )

    def test_committed_file_is_loadable_and_matches_the_in_code_fallback(self):
        path = policy_mod.find_policy_file()
        assert path is not None, "the repo must ship .ai-swarm/selection-policy.json"
        committed = policy_mod.read_policy(path)
        fallback = policy_mod.default_policy()
        # `notes` is prose for humans; every routing-relevant field must match.
        for f in fields(committed):
            if f.name in ("notes",):
                continue
            assert getattr(committed, f.name) == getattr(fallback, f.name), f.name


class TestNonTunableInvariants:
    """No policy - calibrated or hand-edited - may reach these rules."""

    def test_size_l_routes_heavy_even_under_a_light_leaning_policy(self):
        cfg = _cfg()
        # A deliberately absurd policy that would route everything light.
        lenient = replace(_policy(), heavy_threshold=99, light_threshold=99)
        task = Task(kind="implement", title="docs: fix a typo", labels=("size:L",), est_files=1)
        assert select(task, cfg, policy=lenient).tier == "heavy"
        assert "size:L" in select(task, cfg, policy=lenient).rationale

    def test_size_m_routes_default_tier_even_under_a_heavy_leaning_policy(self):
        cfg = _cfg()
        # A policy that would route everything heavy.
        aggressive = replace(_policy(), heavy_threshold=-99, light_threshold=-100)
        task = Task(kind="implement", title="add a small helper", labels=("size:M",))
        choice = select(task, cfg, policy=aggressive)
        assert choice.tier == cfg.default_tier
        assert "size:M" in choice.rationale

    def test_feature_shaped_work_never_routes_light(self):
        cfg = _cfg()
        # Score is far below any light threshold, but the change is broad:
        # more than LIGHT_MAX_FILES files can never be a light-tier edit.
        broad = Task(
            kind="implement",
            title="docs: rename comment blocks across the codebase",
            est_files=LIGHT_MAX_FILES + 1,
        )
        p = _policy()
        assert _score(broad, p) <= p.light_threshold  # would qualify on score alone
        assert select(broad, cfg, policy=p).tier != "light"

        narrow = replace(broad, est_files=LIGHT_MAX_FILES)
        assert select(narrow, cfg, policy=p).tier == "light"

    def test_light_ceiling_holds_under_an_arbitrarily_lenient_policy(self):
        cfg = _cfg()
        lenient = replace(_policy(), light_threshold=99)
        task = Task(kind="implement", title="feat: build a scheduler", est_files=6)
        assert select(task, cfg, policy=lenient).tier != "light"

    def test_budget_gate_demotion_applies_after_the_policy_decides(self):
        cfg = _cfg()
        p = _policy()
        heavy = Task(
            kind="improve", title="architecture: redesign the orchestrator",
            body="large refactor of concurrency model", est_files=10,
        )
        assert select(heavy, cfg, policy=p).tier == "heavy"
        demoted = select(heavy, cfg, demote=True, policy=p)
        assert demoted.tier == "standard"
        assert "soft budget breach" in demoted.rationale
        assert demoted.model == cfg.tiers["standard"].model

    def test_demotion_never_falls_below_the_cheapest_tier(self):
        cfg = _cfg()
        light = Task(kind="implement", title="docs: fix typo in readme", est_files=1)
        assert select(light, cfg).tier == "light"
        assert select(light, cfg, demote=True).tier == "light"

    def test_decide_tier_is_the_single_routing_path(self):
        """Replay (calibrate) and live selection must agree by construction."""
        cfg = _cfg()
        p = _policy()
        for task in FIXTURE_TASKS:
            _, tier, _ = decide_tier(task, p, default_tier=cfg.default_tier)
            assert select(task, cfg, policy=p).tier == tier
