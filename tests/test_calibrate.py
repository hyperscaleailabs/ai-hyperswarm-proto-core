"""The calibrator, driven by a synthetic ledger + lesson corpus.

Every guarantee the calibrator makes is asserted here against data we control:
the min-sample floors block tuning outright, the clamp bounds a proposal to one
step, and the heavy-share constraint rejects a policy that would buy success
with more heavy-tier quota - even when that policy scores better.

Score arithmetic used to build the corpus (all with the committed policy,
``est_files`` at its default of 1, which is what the orchestrator itself uses):

- ``"security: fix a concurrency migration"`` -> 3 heavy signals (+6), file
  bucket (-1) => **5**, exactly the heavy threshold.
- ``"add a status subcommand"`` -> no signals, file bucket (-1) => **-1**, standard.
- ``"security: tighten the concurrency guard"`` (kind=improve) -> 2 signals
  (+4), file bucket (-1), kind (+1) => **4**, standard but one step away.
"""
from __future__ import annotations

from dataclasses import replace

from hsai import calibrate, policy as policy_mod
from hsai.calibrate import INSUFFICIENT, Sample
from hsai.config import load_config
from hsai.knowledge import KnowledgeBase, Lesson
from hsai.ledger import LedgerRecord, append_record, ledger_path

HEAVY_TITLE = "security: fix a concurrency migration"   # score 5  -> heavy
STANDARD_TITLE = "add a status subcommand"              # score -1 -> standard
BORDERLINE_TITLE = "security: tighten the concurrency guard"  # score 4 (improve)
LIGHT_TITLE = "docs: fix typo in readme"                # score -8 -> light


def _policy():
    return policy_mod.load_policy()


def _sample(
    iteration: int,
    tier: str,
    title: str,
    *,
    success: bool = True,
    kind: str = "implement",
    seconds: float = 100.0,
    attempts: int = 1,
) -> Sample:
    return Sample(
        iteration=iteration,
        ticket=iteration,
        kind=kind,
        tier=tier,
        model=tier,
        wall_clock_seconds=seconds,
        attempts=attempts,
        outcome="merged" if success else "recovered",
        lesson_outcome="pass" if success else "fail",
        title=title,
    )


def _corpus(n_heavy: int, heavy_wins: int, n_standard: int, standard_wins: int) -> list[Sample]:
    """``n_heavy`` heavy-scoring tasks and ``n_standard`` standard ones."""
    samples = []
    for i in range(n_heavy):
        samples.append(_sample(i + 1, "heavy", HEAVY_TITLE, success=i < heavy_wins))
    for i in range(n_standard):
        samples.append(
            _sample(100 + i, "standard", STANDARD_TITLE, success=i < standard_wins)
        )
    return samples


# --- the corpus itself scores the way the docstring claims --------------------
def test_fixture_titles_score_where_the_tests_assume():
    p = _policy()
    assert calibrate.decide_tier(_sample(1, "heavy", HEAVY_TITLE).task(), p)[:2] == (5, "heavy")
    assert calibrate.decide_tier(
        _sample(2, "standard", STANDARD_TITLE).task(), p
    )[:2] == (-1, "standard")
    assert calibrate.decide_tier(
        _sample(3, "standard", BORDERLINE_TITLE, kind="improve").task(), p
    )[:2] == (4, "standard")
    assert calibrate.decide_tier(_sample(4, "light", LIGHT_TITLE).task(), p)[1] == "light"


# --- join ---------------------------------------------------------------------
class TestJoin:
    def test_ledger_records_join_to_lesson_outcomes(self):
        records = [
            LedgerRecord(
                iteration=7, block=0, ticket=42, kind="implement", tier="heavy",
                model="opus", wall_clock_seconds=900.0, attempts=2, outcome="merged",
            ),
            LedgerRecord(
                iteration=8, block=0, ticket=43, kind="implement", tier="light",
                model="haiku", wall_clock_seconds=60.0, attempts=1, outcome="recovered",
            ),
        ]
        lessons = [
            _lesson_record(iteration=7, ticket=42, title="implement: feat: add a queue",
                           outcome="pass"),
            _lesson_record(iteration=8, ticket=43, title="implement: docs: fix a typo",
                           outcome="fail"),
        ]
        corpus = calibrate.join_samples(records, lessons)
        assert corpus.unjoined_records == 0
        first, second = corpus.samples
        # The lesson's "{kind}: " prefix is stripped back off to recover the ticket title.
        assert first.title == "feat: add a queue"
        assert first.success is True
        assert second.title == "docs: fix a typo"
        assert second.success is False
        assert second.task().kind == "implement"

    def test_records_without_a_lesson_are_counted_not_guessed(self):
        records = [
            LedgerRecord(
                iteration=1, block=0, ticket=1, kind="implement", tier="heavy",
                model="opus", wall_clock_seconds=10.0, attempts=1, outcome="merged",
            )
        ]
        corpus = calibrate.join_samples(records, [])
        assert corpus.samples == []
        assert corpus.unjoined_records == 1

    def test_a_merged_pr_with_a_failed_lesson_is_not_a_success(self):
        records = [
            LedgerRecord(
                iteration=5, block=0, ticket=5, kind="heal", tier="standard",
                model="sonnet", wall_clock_seconds=10.0, attempts=1, outcome="merged",
            )
        ]
        lessons = [_lesson_record(iteration=5, ticket=5, title="heal: ci red", outcome="fail")]
        (sample,) = calibrate.join_samples(records, lessons).samples
        assert sample.success is False

    def test_lessons_round_trip_through_disk(self, tmp_path):
        """The join key must survive being written to and parsed back off disk."""
        kb = KnowledgeBase(tmp_path)
        kb.write_lesson(
            Lesson(
                title="implement: feat: add a queue", outcome="pass", kind="implement",
                context="ctx", what_happened="ran", lesson="learned",
                iteration=7, ticket=42, model="opus",
            )
        )
        (record,) = kb.read_lessons()
        assert (record.iteration, record.ticket) == (7, 42)
        assert record.model == "opus"
        assert record.outcome == "pass"


def _lesson_record(*, iteration: int, ticket: int, title: str, outcome: str):
    from hsai.knowledge import LessonRecord

    return LessonRecord(
        note_name=f"note-{iteration}",
        title=title,
        outcome=outcome,
        kind=title.split(":", 1)[0],
        tags=("lesson",),
        lesson_text="",
        iteration=iteration,
        ticket=ticket,
    )


# --- per-tier stats + regret --------------------------------------------------
class TestStatsAndRegret:
    def test_tier_stats_report_success_median_and_retry_rate(self):
        samples = [
            _sample(1, "heavy", HEAVY_TITLE, success=True, seconds=100.0),
            _sample(2, "heavy", HEAVY_TITLE, success=False, seconds=300.0, attempts=2),
            _sample(3, "heavy", HEAVY_TITLE, success=True, seconds=200.0),
            _sample(4, "standard", STANDARD_TITLE, success=True, seconds=50.0),
        ]
        stats = calibrate.tier_stats(samples)
        assert stats["heavy"].n == 3
        assert stats["heavy"].success_rate == 2 / 3
        assert stats["heavy"].median_seconds == 200.0
        assert stats["heavy"].retry_rate == 1 / 3
        assert stats["heavy"].total_seconds == 600.0
        assert stats["standard"].n == 1
        assert stats["light"].n == 0  # empty tiers are reported, not omitted
        assert stats["light"].success_rate == 0.0

    def test_regret_counts_reclaimable_heavy_runs(self):
        # A first-attempt heavy win sitting exactly on the threshold: one step
        # of clamp would have routed it cheaper.
        samples = [_sample(1, "heavy", HEAVY_TITLE, success=True, seconds=900.0)]
        regret = calibrate.estimate_regret(samples, _policy())
        assert regret.heavy_overspend == 1
        assert regret.heavy_overspend_seconds == 900.0

    def test_regret_ignores_heavy_runs_a_clamp_step_cannot_reclaim(self):
        deep = _sample(1, "heavy", "architecture: redesign the concurrency migration model")
        regret = calibrate.estimate_regret([deep], _policy())
        assert regret.heavy_overspend == 0

    def test_regret_counts_under_routed_retries(self):
        samples = [
            _sample(1, "standard", STANDARD_TITLE, success=False, attempts=2),
            _sample(2, "standard", STANDARD_TITLE, success=True),
        ]
        regret = calibrate.estimate_regret(samples, _policy())
        assert regret.under_routed == 1
        assert "retried" in regret.summary()

    def test_regret_counts_a_failure_escalated_to_a_heavier_tier(self):
        samples = [
            Sample(iteration=1, ticket=77, kind="implement", tier="standard",
                   model="sonnet", wall_clock_seconds=10.0, attempts=1,
                   outcome="recovered", lesson_outcome="fail", title=STANDARD_TITLE),
            Sample(iteration=2, ticket=77, kind="implement", tier="heavy",
                   model="opus", wall_clock_seconds=10.0, attempts=1,
                   outcome="merged", lesson_outcome="pass", title=STANDARD_TITLE),
        ]
        assert calibrate.estimate_regret(samples, _policy()).under_routed == 1


# --- the bounded search -------------------------------------------------------
class TestSampleFloors:
    def test_below_the_total_floor_nothing_is_tuned(self):
        proposal = calibrate.propose_policy(
            _policy(), _corpus(2, 0, 1, 1), min_total_samples=20, min_samples_per_tier=1
        )
        assert proposal.changed is False
        assert proposal.verdict.startswith(INSUFFICIENT)
        assert proposal.policy == _policy()

    def test_an_empty_corpus_is_insufficient_not_a_crash(self):
        proposal = calibrate.propose_policy(_policy(), [])
        assert proposal.changed is False
        assert INSUFFICIENT in proposal.verdict

    def test_below_the_per_tier_floor_every_dimension_is_frozen(self):
        # 20 samples clears the total floor, but no tier reaches 50.
        proposal = calibrate.propose_policy(
            _policy(), _corpus(10, 2, 10, 10),
            min_total_samples=10, min_samples_per_tier=50,
        )
        assert proposal.changed is False
        assert INSUFFICIENT in proposal.verdict
        assert set(proposal.frozen_dimensions) == {
            "heavy_threshold", "heavy_signal_weight",
            "light_threshold", "light_signal_weight",
        }

    def test_a_thin_tier_freezes_only_its_own_dimensions(self):
        # heavy has samples, light has none: light's dimensions stay put.
        proposal = calibrate.propose_policy(
            _policy(), _corpus(10, 2, 10, 10),
            min_total_samples=10, min_samples_per_tier=4,
        )
        assert set(proposal.frozen_dimensions) == {"light_threshold", "light_signal_weight"}
        assert proposal.policy.light_threshold == _policy().light_threshold
        assert proposal.policy.light_signal_weight == _policy().light_signal_weight


class TestClamp:
    def test_a_proposal_moves_at_most_one_step(self):
        base = _policy()
        # Heavy wins only 20% of the time; standard wins every time. The
        # unbounded optimum would push the heavy threshold out of reach - the
        # clamp allows exactly one step.
        samples = _corpus(10, 2, 10, 10)
        proposal = calibrate.propose_policy(
            base, samples, min_total_samples=10, min_samples_per_tier=4,
        )
        assert proposal.changed is True
        assert calibrate.clamp_violations(base, proposal.policy) == []
        for dim, _tier in calibrate.TUNABLE_DIMENSIONS:
            assert abs(getattr(proposal.policy, dim) - getattr(base, dim)) <= 1
        assert proposal.policy.version == base.version + 1
        assert proposal.deltas  # the report names what moved
        assert str(len(samples)) in proposal.policy.notes  # provenance is recorded

    def test_the_proposal_actually_routes_the_heavy_work_cheaper(self):
        base = _policy()
        proposal = calibrate.propose_policy(
            base, _corpus(10, 2, 10, 10), min_total_samples=10, min_samples_per_tier=4,
        )
        task = _sample(1, "heavy", HEAVY_TITLE).task()
        assert calibrate.decide_tier(task, base)[1] == "heavy"
        assert calibrate.decide_tier(task, proposal.policy)[1] != "heavy"

    def test_clamp_violations_flags_a_hand_edited_jump(self):
        base = _policy()
        assert calibrate.clamp_violations(base, replace(base, heavy_threshold=7)) == [
            "heavy_threshold"
        ]
        assert "kind_weights" in calibrate.clamp_violations(
            base, replace(base, kind_weights={"heal": 9})
        )
        assert calibrate.clamp_violations(base, replace(base, heavy_threshold=6)) == []


class TestHeavyShareConstraint:
    def _samples(self):
        """Heavy always wins, standard rarely does - the tempting corpus."""
        samples = [_sample(i + 1, "heavy", HEAVY_TITLE, success=True) for i in range(6)]
        samples += [
            _sample(100 + i, "standard", BORDERLINE_TITLE, kind="improve", success=(i == 0))
            for i in range(6)
        ]
        return samples

    def test_a_heavier_policy_is_rejected_even_though_it_scores_better(self):
        base = _policy()
        samples = self._samples()
        stats = calibrate.tier_stats(samples)
        baseline = calibrate.evaluate_policy(base, samples, stats)

        # The greedy move (route the borderline work heavy too) genuinely looks
        # better on the estimator - and costs strictly more heavy-tier quota.
        greedy = replace(base, heavy_threshold=base.heavy_threshold - 1)
        greedy_ev = calibrate.evaluate_policy(greedy, samples, stats)
        assert greedy_ev.expected_success > baseline.expected_success
        assert greedy_ev.heavy_share > baseline.heavy_share

        proposal = calibrate.propose_policy(
            base, samples, min_total_samples=10, min_samples_per_tier=4,
        )
        assert proposal.changed is False
        assert "heavy-tier share" in proposal.verdict
        assert proposal.policy.heavy_threshold == base.heavy_threshold

    def test_an_accepted_proposal_never_raises_heavy_share(self):
        base = _policy()
        proposal = calibrate.propose_policy(
            base, _corpus(10, 2, 10, 10), min_total_samples=10, min_samples_per_tier=4,
        )
        assert proposal.changed is True
        assert proposal.proposed.heavy_share <= proposal.baseline.heavy_share

    def test_noise_sized_gains_are_ignored(self):
        base = _policy()
        proposal = calibrate.propose_policy(
            base, _corpus(10, 2, 10, 10),
            min_total_samples=10, min_samples_per_tier=4, min_gain=0.99,
        )
        assert proposal.changed is False
        assert "policy unchanged" in proposal.verdict


def test_replay_agreement_reports_reconstruction_fidelity():
    samples = [
        _sample(1, "heavy", HEAVY_TITLE),        # replays to heavy: agrees
        _sample(2, "light", STANDARD_TITLE),     # replays to standard: disagrees
    ]
    agreement = calibrate.replay_agreement(samples, _policy(), default_tier="standard")
    assert agreement == 0.5


# --- report + end-to-end ------------------------------------------------------
def _write_corpus(cfg, root, samples: list[Sample]) -> None:
    """Materialise samples as a real ledger + real lesson notes under ``root``."""
    kb = KnowledgeBase.from_config(cfg, root)
    path = ledger_path(cfg, root)
    for s in samples:
        append_record(
            path,
            LedgerRecord(
                iteration=s.iteration, block=0, ticket=s.ticket, kind=s.kind,
                tier=s.tier, model=s.model, wall_clock_seconds=s.wall_clock_seconds,
                attempts=s.attempts, outcome=s.outcome,
            ),
        )
        kb.write_lesson(
            Lesson(
                title=f"{s.kind}: {s.title} ({s.iteration})",
                outcome=s.lesson_outcome, kind=s.kind, context="ctx",
                what_happened="ran", lesson="learned",
                iteration=s.iteration, ticket=s.ticket, model=s.model,
            )
        )


class TestRunCalibration:
    def test_thin_data_writes_an_honest_report_and_no_policy(self, tmp_path):
        cfg = load_config()
        _write_corpus(cfg, tmp_path, _corpus(1, 1, 1, 1))

        res = calibrate.run_calibration(cfg, repo_root=tmp_path, created="2026-08-03")

        assert res.policy_path is None, "a thin corpus must not move the policy"
        assert not policy_mod.policy_path(cfg, tmp_path).exists()
        assert res.report_path.name == "selection-calibration-2026-08-03.md"
        text = res.report_path.read_text()
        assert INSUFFICIENT in text
        # Obsidian-ready: frontmatter tags plus a link up to the MOC.
        assert text.startswith("---\ntags:\n")
        assert "[[Knowledge Base MOC]]" in text
        assert "## Per-tier outcomes" in text
        assert "## Regret estimate" in text
        assert "## Non-tunable invariants" in text
        assert "| light |" in text and "| standard |" in text and "| heavy |" in text
        assert "declined to tune" in res.report.governance_note()

    def test_a_real_corpus_produces_a_bounded_reviewable_policy_diff(self, tmp_path):
        cfg = load_config()
        cfg.calibration.update({"min_total_samples": 8, "min_samples_per_tier": 4})
        _write_corpus(cfg, tmp_path, _corpus(4, 0, 4, 4))

        res = calibrate.run_calibration(cfg, repo_root=tmp_path, created="2026-08-03")

        assert res.report.proposal.changed is True
        assert res.policy_path is not None and res.policy_path.exists()
        written = policy_mod.read_policy(res.policy_path)
        assert written.version == 2
        assert calibrate.clamp_violations(policy_mod.default_policy(), written) == []
        assert "calibrated 2026-08-03" in written.notes
        report_text = res.report_path.read_text()
        assert "Proposed:" in report_text
        assert "proposed" in res.report.governance_note()

    def test_calibration_is_idempotent_on_an_unchanged_corpus(self, tmp_path):
        cfg = load_config()
        _write_corpus(cfg, tmp_path, _corpus(1, 1, 1, 1))
        first = calibrate.run_calibration(cfg, repo_root=tmp_path, created="2026-08-03")
        second = calibrate.run_calibration(cfg, repo_root=tmp_path, created="2026-08-03")
        assert first.report_path.read_text() == second.report_path.read_text()

    def test_report_counts_ledger_records_that_never_found_a_lesson(self, tmp_path):
        cfg = load_config()
        append_record(
            ledger_path(cfg, tmp_path),
            LedgerRecord(
                iteration=999, block=0, ticket=999, kind="implement", tier="heavy",
                model="opus", wall_clock_seconds=1.0, attempts=1, outcome="merged",
            ),
        )
        res = calibrate.run_calibration(cfg, repo_root=tmp_path, created="2026-08-03")
        assert res.report.corpus.unjoined_records == 1
        assert "| ledger records with no lesson | 1 |" in res.report_path.read_text()
