import json
import subprocess
from pathlib import Path

import pytest

from hsai import bench, cli, ledger, orchestrator, trajectory
from hsai.config import load_config

CORPUS = Path(bench.CORPUS_DIR)
BASELINE = Path(bench.BASELINE_PATH)


@pytest.fixture(scope="module")
def cfg():
    return load_config()


def _scenario(**given) -> bench.Scenario:
    """A minimal green implement scenario, overridden per test."""
    base = {
        "ci_green": True,
        "has_tickets": True,
        "ticket": {"number": 1, "title": "feat: add a widget", "body": "Add one widget."},
        "agent": {"ok": True},
        "changed_paths": ["src/hsai/widget.py", "tests/test_widget.py"],
        "ci_after_green": True,
        "review_approve": True,
        "remote_ci": "SUCCESS",
    }
    base.update(given)
    return bench.Scenario.from_dict(
        {"schema_version": 1, "name": "adhoc", "given": base, "expect": {"outcome": "merged"}}
    )


# --- the committed corpus ---------------------------------------------------

def test_corpus_covers_the_required_scenarios():
    """The corpus is the bench's coverage claim; keep it honest."""
    names = {s.name for s in bench.load_corpus(CORPUS)}
    for required in (
        "implement-green", "heal-red", "off-spec-workflow-revert",
        "repro-guard-block", "budget-hard-breach-halt",
    ):
        assert required in names
    assert len(names) >= 8


def test_corpus_scenarios_are_distinct_and_well_formed():
    scenarios = bench.load_corpus(CORPUS)
    assert len({s.name for s in scenarios}) == len(scenarios)
    for s in scenarios:
        assert s.description, f"{s.name} has no description"
        assert s.expect, f"{s.name} expects nothing"


def test_bench_passes_on_the_committed_corpus(cfg):
    report = bench.run_bench(cfg, CORPUS)
    assert report.failures == []
    assert report.ok
    assert report.pass_rate == 1.0
    assert report.tier_agreement == 1.0
    assert report.recovery_accuracy == 1.0


def test_bench_never_spawns_a_process(cfg, monkeypatch):
    """The whole premise: no model, no network, no quota.

    Poisoning `subprocess.run` itself is the strongest available statement -
    it holds regardless of which runner any layer happens to be holding, and
    the repro guard genuinely does invoke one (a scripted one).
    """
    def explode(*a, **kw):
        raise AssertionError("bench spawned a real subprocess")

    monkeypatch.setattr(subprocess, "run", explode)
    assert bench.run_bench(cfg, CORPUS).ok


def test_scripted_runner_refuses_anything_it_did_not_expect():
    """The fake is fail-closed: an unhandled command is a loud error, not a
    silent success that would let the bench drift away from real behaviour."""
    runner = bench._ScriptedRunner(root="/tmp", fix_passes=True, parent_passes=False)
    assert runner(["git", "worktree", "add"]).ok
    assert runner(["pytest", "tests/test_x.py"]).ok
    assert not runner(["pytest", "tests/test_x.py"], cwd="/tmp/repro-check-abc").ok
    with pytest.raises(AssertionError, match="unexpected command"):
        runner(["claude", "-p", "do the thing"])


# --- replay: the real decision code, one guard at a time --------------------

def test_replay_produces_a_schema_versioned_trajectory(cfg):
    """A replayed scenario and a live iteration are the same kind of object."""
    result = bench.replay(_scenario(), cfg)
    assert result.traj.schema_version == trajectory.SCHEMA_VERSION
    assert result.traj.outcome == orchestrator.MERGED
    assert result.traj.prompt_hash  # the real _task_prompt ran
    assert result.traj.diff_stat == {
        "files": 2, "code": 2, "knowledge": 0, "tests": 1, "workflows": 0
    }


def test_replay_uses_the_real_path_decision(cfg):
    assert bench.replay(_scenario(ci_green=False), cfg).traj.kind == orchestrator.HEAL
    assert bench.replay(_scenario(has_tickets=False), cfg).traj.kind == orchestrator.IMPROVE
    assert bench.replay(_scenario(), cfg).traj.kind == orchestrator.IMPLEMENT


def test_replay_halts_before_selecting_a_model_on_a_hard_breach(cfg):
    spend = [{"tier": "heavy", "outcome": "merged", "seconds": 10} for _ in range(3)]
    result = bench.replay(_scenario(prior_iterations=spend), cfg)
    assert result.budget == ledger.HARD
    assert result.traj.outcome == orchestrator.HALTED
    # Nothing downstream ran: no tier picked, no ticket claimed, no quota spent.
    assert (result.traj.tier, result.traj.kind, result.traj.model) == ("", "", "")


def test_replay_demotes_the_tier_on_a_soft_breach(cfg):
    heavy = {"number": 1, "title": "feat: add a widget", "body": "x", "labels": ["size:L"]}
    ok = bench.replay(_scenario(ticket=heavy), cfg)
    assert (ok.budget, ok.traj.tier) == (ledger.OK, "heavy")

    spend = [{"tier": "standard", "outcome": "merged", "seconds": 900} for _ in range(5)]
    soft = bench.replay(_scenario(ticket=heavy, prior_iterations=spend), cfg)
    assert (soft.budget, soft.traj.tier) == (ledger.SOFT, "standard")


def test_replay_blocks_a_knowledge_only_diff_on_a_code_ticket(cfg):
    result = bench.replay(_scenario(changed_paths=["knowledge/lessons/x.md"]), cfg)
    assert result.traj.outcome == orchestrator.INCOMPLETE
    assert result.traj.recovered is True
    # It never reached a PR, so remote CI was never consulted.
    assert result.traj.remote_ci == ""


def test_replay_runs_the_real_repro_guard(cfg):
    heal = {"number": 1, "title": "ci: main is red - auto-heal", "body": "red"}
    blocked = bench.replay(
        _scenario(ci_green=False, ticket=heal, changed_paths=["src/hsai/ci.py"]), cfg
    )
    assert blocked.traj.outcome == orchestrator.NO_REPRO

    # A test that passes on the fix branch AND on the pre-fix tree proves
    # nothing - the guard must reject that too, not just a missing test.
    not_reproduced = bench.replay(
        _scenario(
            ci_green=False, ticket=heal,
            changed_paths=["src/hsai/ci.py", "tests/test_ci.py"],
            repro={"fix_passes": True, "parent_passes": True},
        ),
        cfg,
    )
    assert not_reproduced.traj.outcome == orchestrator.NO_REPRO

    reproduced = bench.replay(
        _scenario(
            ci_green=False, ticket=heal,
            changed_paths=["src/hsai/ci.py", "tests/test_ci.py"],
            repro={"fix_passes": True, "parent_passes": False},
        ),
        cfg,
    )
    assert reproduced.traj.outcome == orchestrator.MERGED


def test_replay_skips_review_on_a_red_branch_and_recovers(cfg):
    result = bench.replay(_scenario(ci_after_green=False, remote_ci="FAILURE"), cfg)
    assert result.traj.review == "skipped"
    assert result.traj.outcome == orchestrator.RECOVERED


def test_replay_applies_the_real_retry_policy(cfg):
    first = {"number": 1, "title": "feat: add a widget", "body": "x", "prior_attempts": 0}
    last = {"number": 1, "title": "feat: add a widget", "body": "x", "prior_attempts": 1}
    assert bench.replay(_scenario(ticket=first, remote_ci="FAILURE"), cfg).disposition == (
        orchestrator.RETRY
    )
    assert bench.replay(_scenario(ticket=last, remote_ci="FAILURE"), cfg).disposition == (
        orchestrator.BLOCKED
    )


def test_replay_honours_the_ticket_authorized_workflow_edit(cfg):
    paths = ["src/hsai/bench.py", ".github/workflows/ci.yml"]
    off_spec = bench.replay(_scenario(changed_paths=paths), cfg)
    assert off_spec.traj.diff_stat["workflows"] == 0  # reverted

    asked = {"number": 1, "title": "feat: add a bench job",
             "body": "Add a job to .github/workflows/ci.yml."}
    authorized = bench.replay(_scenario(ticket=asked, changed_paths=paths), cfg)
    assert authorized.traj.diff_stat["workflows"] == 1  # survived


# --- expectation matching ---------------------------------------------------

def test_mismatch_is_reported_per_expectation(cfg):
    scenario = bench.Scenario.from_dict(
        {
            "schema_version": 1,
            "name": "wrong",
            "given": {"ci_green": True, "has_tickets": True,
                      "ticket": {"title": "feat: add a widget"},
                      "changed_paths": ["src/hsai/widget.py"]},
            "expect": {"tier": "heavy", "outcome": "merged"},
        }
    )
    mismatches = bench.replay(scenario, cfg).mismatches()
    assert mismatches == ["tier: expected 'heavy', got 'standard'"]


def test_an_unknown_expectation_is_a_failure_not_a_no_op(cfg):
    """A typo in a fixture must fail loudly, never silently assert nothing."""
    scenario = bench.Scenario.from_dict(
        {
            "schema_version": 1, "name": "typo",
            "given": {"ticket": {"title": "feat: add a widget"},
                      "changed_paths": ["src/hsai/widget.py"]},
            "expect": {"teir": "standard"},
        }
    )
    assert bench.replay(scenario, cfg).mismatches() == [
        "teir: unknown expectation (not an observable)"
    ]


# --- corpus loading ---------------------------------------------------------

def test_corpus_rejects_a_foreign_schema_version(tmp_path):
    (tmp_path / "s.json").write_text(json.dumps({"schema_version": 99, "name": "x"}))
    with pytest.raises(bench.CorpusError, match="schema_version"):
        bench.load_corpus(tmp_path)


def test_empty_corpus_raises(tmp_path):
    with pytest.raises(bench.CorpusError, match="no scenarios"):
        bench.load_corpus(tmp_path)


def test_corpus_order_is_deterministic():
    names = [s.name for s in bench.load_corpus(CORPUS)]
    assert names == [s.name for s in bench.load_corpus(CORPUS)]


# --- report + baseline gate -------------------------------------------------

def test_report_ratios_and_render():
    report = bench.BenchReport(
        total=4, passed=3, tier_expected=4, tier_agreed=4,
        recovery_expected=2, recovery_correct=1, seconds=0.4,
        failures=[{"scenario": "x", "mismatches": ["tier: expected 'heavy', got 'light'"]}],
    )
    assert report.pass_rate == 0.75
    assert report.tier_agreement == 1.0
    assert report.recovery_accuracy == 0.5
    assert report.mean_seconds == 0.1
    assert report.ok is False
    rendered = report.render()
    assert "3/4 scenarios passed" in rendered
    assert "mean seconds/ticket" in rendered
    assert "FAIL x" in rendered


def test_an_unexercised_dimension_is_not_a_failure():
    """A corpus that expects no tier scores 1.0 for tier agreement, not 0.0."""
    report = bench.BenchReport(total=1, passed=1)
    assert report.tier_agreement == 1.0
    assert report.recovery_accuracy == 1.0


def test_committed_baseline_matches_the_current_corpus(cfg):
    baseline = bench.read_baseline(BASELINE)
    report = bench.run_bench(cfg, CORPUS)
    assert bench.regressions(report, baseline) == []
    # The committed floor must not drift below what the corpus actually is.
    assert baseline["scenarios"] == report.total


def test_regressions_detects_each_gated_metric():
    report = bench.BenchReport(
        total=2, passed=1, tier_expected=2, tier_agreed=1,
        recovery_expected=2, recovery_correct=1,
    )
    found = bench.regressions(report, {"scenarios": 2, "pass_rate": 1.0,
                                       "tier_agreement": 1.0, "recovery_accuracy": 1.0})
    assert len(found) == 3
    assert any(f.startswith("pass_rate") for f in found)
    assert any(f.startswith("tier_agreement") for f in found)
    assert any(f.startswith("recovery_accuracy") for f in found)


def test_deleting_a_scenario_is_itself_a_regression():
    report = bench.BenchReport(total=7, passed=7)
    found = bench.regressions(report, {"scenarios": 8, "pass_rate": 1.0,
                                       "tier_agreement": 1.0, "recovery_accuracy": 1.0})
    assert found == ["scenarios: 7 < baseline 8 (scenarios were removed)"]


def test_wall_clock_is_measured_but_never_gated():
    """A slow CI runner must not fail the gate; only decisions are gated."""
    slow = bench.BenchReport(total=1, passed=1, seconds=999.0)
    assert slow.mean_seconds == 999.0
    assert "mean_seconds" not in bench.GATED_METRICS
    assert bench.regressions(slow, {"scenarios": 1, "pass_rate": 1.0}) == []


def test_write_and_read_baseline_roundtrip(cfg, tmp_path):
    report = bench.run_bench(cfg, CORPUS)
    path = bench.write_baseline(report, tmp_path / "nested" / "baseline.json")
    assert bench.read_baseline(path) == report.baseline_dict()
    # Only metrics are committed - no per-scenario payload, no wall-clock.
    assert set(bench.read_baseline(path)) == {
        "schema_version", "scenarios", "pass_rate", "tier_agreement", "recovery_accuracy"
    }


def test_read_baseline_missing_raises(tmp_path):
    with pytest.raises(bench.CorpusError, match="no baseline"):
        bench.read_baseline(tmp_path / "absent.json")


# --- CLI --------------------------------------------------------------------

def test_cli_bench_json_is_green_on_the_corpus(capsys):
    assert cli.main(["bench", "--json", "--corpus", str(CORPUS)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["pass_rate"] == 1.0
    assert payload["failures"] == []
    assert len(payload["results"]) == payload["scenarios"]
    # Each result carries the replayed trajectory, so `--json` is inspectable.
    assert payload["results"][0]["trajectory"]["schema_version"] == 1


def test_cli_bench_check_passes_against_the_committed_baseline(capsys):
    assert cli.main(["bench", "--check"]) == 0
    assert "no regression" in capsys.readouterr().out


def test_cli_bench_exits_nonzero_when_a_scenario_deviates(tmp_path, capsys):
    """The gate's own regression test: corrupt one expected tier, expect exit 1."""
    corrupted = json.loads((CORPUS / "01-implement-green.json").read_text())
    corrupted["expect"]["tier"] = "light"
    (tmp_path / "01-implement-green.json").write_text(json.dumps(corrupted))

    assert cli.main(["bench", "--corpus", str(tmp_path)]) == 1
    captured = capsys.readouterr()
    assert "FAIL implement-green" in captured.out
    assert "tier: expected 'light', got 'standard'" in captured.out
    assert "deviated from the corpus" in captured.err


def test_cli_bench_exits_nonzero_on_a_baseline_regression(tmp_path, capsys):
    """A corpus smaller than the baseline is a regression even when it passes."""
    (tmp_path / "s.json").write_text(
        (CORPUS / "01-implement-green.json").read_text()
    )
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps({"schema_version": 1, "scenarios": 16, "pass_rate": 1.0,
                                    "tier_agreement": 1.0, "recovery_accuracy": 1.0}))
    assert cli.main(["bench", "--corpus", str(tmp_path), "--check",
                     "--baseline", str(baseline)]) == 1
    assert "REGRESSION scenarios" in capsys.readouterr().err


def test_cli_bench_reports_a_missing_corpus(tmp_path, capsys):
    assert cli.main(["bench", "--corpus", str(tmp_path / "nope")]) == 1
    assert "no scenarios" in capsys.readouterr().err


def test_cli_bench_update_baseline_writes_metrics_only(tmp_path, capsys):
    target = tmp_path / "baseline.json"
    assert cli.main(["bench", "--update-baseline", "--baseline", str(target)]) == 0
    assert capsys.readouterr().out.startswith("bench: wrote baseline")
    assert json.loads(target.read_text())["scenarios"] == 16
