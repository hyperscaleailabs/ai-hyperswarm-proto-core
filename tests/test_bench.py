import json
from pathlib import Path

import pytest

from hsai import bench
from hsai.cli import main
from hsai.config import load_config

CORPUS = Path(__file__).parent / "fixtures" / "trajectories"
BASELINE = Path(__file__).parent.parent / "bench" / "baseline.json"

# Every scenario the ticket named as required coverage, by id.
REQUIRED_SCENARIOS = {
    "implement-green",
    "heal-red",
    "off-spec-recovery",
    "repro-guard-block",
    "budget-hard-breach",
    "agent-timeout",
    "merge-conflict",
    "blocked-after-max-attempts",
}


@pytest.fixture
def cfg():
    return load_config()


def _scenario(name: str) -> bench.Scenario:
    return next(s for s in bench.load_corpus(CORPUS) if s.id == name)


# --- corpus -----------------------------------------------------------------

def test_corpus_covers_at_least_eight_distinct_scenarios():
    scenarios = bench.load_corpus(CORPUS)
    ids = {s.id for s in scenarios}
    assert len(scenarios) >= 8
    assert len(ids) == len(scenarios), "scenario ids must be unique"
    assert REQUIRED_SCENARIOS <= ids, f"missing: {sorted(REQUIRED_SCENARIOS - ids)}"


def test_corpus_scenarios_are_well_formed():
    for scenario in bench.load_corpus(CORPUS):
        assert scenario.title, f"{scenario.id} has no title"
        assert scenario.given.get("ticket"), f"{scenario.id} has no ticket"
        # Every scenario must pin at least the outcome - a fixture that asserts
        # nothing is a fixture that can never fail.
        assert "outcome" in scenario.expect
        assert "tier" in scenario.expect


def test_scenario_rejects_an_unreadable_schema_version(tmp_path):
    path = tmp_path / "future.json"
    path.write_text(json.dumps({"schema_version": 99, "id": "x", "given": {}, "expect": {}}))
    with pytest.raises(ValueError, match="schema_version"):
        bench.Scenario.load(path)


def test_load_corpus_rejects_a_missing_or_empty_directory(tmp_path):
    with pytest.raises(FileNotFoundError):
        bench.load_corpus(tmp_path / "nope")
    (tmp_path / "empty").mkdir()
    with pytest.raises(FileNotFoundError):
        bench.load_corpus(tmp_path / "empty")


# --- the offline invariant --------------------------------------------------

def test_bench_never_invokes_a_model(cfg, monkeypatch):
    """The whole point: replaying the corpus spends no quota and hits no network.

    Enforced two ways - the replay's own runner raises on a `claude` command,
    and here `subprocess.run` itself is poisoned, so *any* shell-out at all
    (network or otherwise) fails the test rather than passing quietly.
    """
    import subprocess

    def explode(*args, **kwargs):
        raise AssertionError(f"bench spawned a subprocess: {args!r}")

    monkeypatch.setattr(subprocess, "run", explode)
    report = bench.run_bench(cfg, CORPUS)
    assert report.scenarios >= 8


def test_bench_runner_refuses_a_model_invocation(tmp_path):
    runner = bench.BenchRunner(tmp_path, fix_ok=True, parent_ok=False)
    with pytest.raises(bench.ModelInvokedError):
        runner(["claude", "-p", "do the thing"])


# --- replay fidelity --------------------------------------------------------

def test_corpus_replays_clean_against_the_real_decision_code(cfg):
    report = bench.run_bench(cfg, CORPUS)
    assert report.deviations == [], report.render()
    assert report.ok
    assert report.passed == report.scenarios
    assert report.pass_rate == 1.0
    assert report.tier_agreement == 1.0
    assert report.recovery_accuracy == 1.0
    assert report.mean_seconds > 0


@pytest.mark.parametrize(
    "scenario_id, outcome, guard",
    [
        ("implement-green", bench.MERGED, ""),
        ("heal-red", bench.MERGED, ""),
        ("off-spec-recovery", bench.INCOMPLETE, "completeness"),
        ("repro-guard-block", bench.NO_REPRO, "repro"),
        ("repro-guard-no-test", bench.NO_REPRO, "repro"),
        ("budget-hard-breach", bench.HALTED, "budget"),
        ("agent-timeout", bench.RECOVERED, "remote_ci"),
        ("merge-conflict", bench.RECOVERED, "remote_ci"),
        ("review-blocked", bench.REVIEW_BLOCKED, "review"),
    ],
)
def test_each_guard_fires_on_its_own_scenario(cfg, scenario_id, outcome, guard):
    result = bench.replay(_scenario(scenario_id), cfg)
    assert (result.outcome, result.guard) == (outcome, guard)


def test_retry_policy_blocks_a_ticket_that_ran_out_of_attempts(cfg):
    retried = bench.replay(_scenario("merge-conflict"), cfg)
    exhausted = bench.replay(_scenario("blocked-after-max-attempts"), cfg)
    # Same outcome, different disposition of the ticket - that distinction is
    # exactly what "recovery correctness" measures.
    assert retried.outcome == exhausted.outcome == bench.RECOVERED
    assert retried.recovery == bench.RETRY
    assert exhausted.recovery == bench.BLOCKED


def test_budget_breaches_change_the_tier_the_way_the_gate_intends(cfg):
    hard = bench.replay(_scenario("budget-hard-breach"), cfg)
    soft = bench.replay(_scenario("budget-soft-breach-demote"), cfg)
    # Same size:L ticket shape: a hard breach halts on the tier it would have
    # used, a soft breach demotes one step and keeps going.
    assert (hard.outcome, hard.tier) == (bench.HALTED, "heavy")
    assert hard.seconds == 0.0
    assert (soft.outcome, soft.tier) == (bench.MERGED, "standard")


def test_light_tier_is_reserved_for_narrow_docs_work(cfg):
    assert bench.replay(_scenario("docs-light-tier"), cfg).tier == "light"
    assert bench.replay(_scenario("implement-green"), cfg).tier == "standard"


def test_empty_backlog_routes_to_self_improvement(cfg):
    assert bench.replay(_scenario("improve-empty-backlog"), cfg).kind == "improve"


def test_review_gate_fails_closed_on_unreadable_output(cfg):
    """A reviewer whose reply cannot be parsed must not let a change through."""
    scenario = _scenario("implement-green")
    mangled = bench.Scenario(
        id=scenario.id, title=scenario.title,
        given={**scenario.given, "review_output": "looks fine to me"},
        expect=scenario.expect,
    )
    assert bench.replay(mangled, cfg).outcome == bench.REVIEW_BLOCKED


# --- the regression gate ----------------------------------------------------

def test_corrupting_an_expected_tier_makes_the_bench_fail(cfg, tmp_path):
    """The gate's own proof: a wrong expectation must be caught, not absorbed."""
    for path in sorted(CORPUS.glob("*.json")):
        (tmp_path / path.name).write_text(path.read_text(), encoding="utf-8")
    target = tmp_path / "01-implement-green.json"
    data = json.loads(target.read_text())
    assert data["expect"]["tier"] != "heavy"
    data["expect"]["tier"] = "heavy"
    target.write_text(json.dumps(data), encoding="utf-8")

    report = bench.run_bench(cfg, tmp_path)
    assert not report.ok
    assert report.tier_agreement < 1.0
    assert [d.field for d in report.deviations] == ["tier"]
    assert bench.check_regression(report, bench.read_baseline(BASELINE))


def test_committed_baseline_matches_the_corpus(cfg):
    report = bench.run_bench(cfg, CORPUS)
    baseline = bench.read_baseline(BASELINE)
    assert bench.check_regression(report, baseline) == []
    assert baseline["scenarios"] == report.scenarios


def test_check_regression_flags_a_shrinking_corpus():
    report = bench.BenchReport(scenarios=5, passed=5)
    problems = bench.check_regression(
        report,
        {"schema_version": 1, "scenarios": 13, "pass_rate": 1.0,
         "tier_agreement": 1.0, "recovery_accuracy": 1.0,
         "mean_seconds_per_ticket": 100.0},
    )
    assert any("corpus shrank" in p for p in problems)


def test_check_regression_flags_a_dropped_pass_rate():
    report = bench.BenchReport(scenarios=10, passed=8)
    problems = bench.check_regression(
        report,
        {"schema_version": 1, "scenarios": 10, "pass_rate": 1.0,
         "tier_agreement": 1.0, "recovery_accuracy": 1.0,
         "mean_seconds_per_ticket": 100.0},
    )
    assert any("pass_rate regressed" in p for p in problems)


def test_check_regression_tolerates_small_timing_drift():
    baseline = {"schema_version": 1, "scenarios": 1, "pass_rate": 1.0,
                "tier_agreement": 1.0, "recovery_accuracy": 1.0,
                "mean_seconds_per_ticket": 100.0}
    within = bench.BenchReport(scenarios=1, passed=1, total_seconds=110.0)
    beyond = bench.BenchReport(scenarios=1, passed=1, total_seconds=200.0)
    assert bench.check_regression(within, baseline) == []
    assert any("mean_seconds" in p for p in bench.check_regression(beyond, baseline))


def test_read_baseline_rejects_an_unreadable_schema_version(tmp_path):
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps({"schema_version": 99}))
    with pytest.raises(ValueError, match="schema_version"):
        bench.read_baseline(path)


def test_write_baseline_round_trips(cfg, tmp_path):
    report = bench.run_bench(cfg, CORPUS)
    path = bench.write_baseline(report, tmp_path / "nested" / "baseline.json")
    assert bench.read_baseline(path) == report.metrics()


# --- the CLI surface --------------------------------------------------------

def test_cli_bench_json_is_offline_and_green(capsys):
    code = main(["bench", "--corpus", str(CORPUS), "--baseline", str(BASELINE),
                 "--check", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["metrics"]["pass_rate"] == 1.0
    assert payload["deviations"] == []
    assert payload["regressions"] == []
    assert payload["replays"]["implement-green"]["outcome"] == bench.MERGED


def test_cli_bench_exits_non_zero_on_a_deviating_scenario(tmp_path, capsys):
    for path in sorted(CORPUS.glob("*.json")):
        (tmp_path / path.name).write_text(path.read_text(), encoding="utf-8")
    target = tmp_path / "02-heal-red.json"
    data = json.loads(target.read_text())
    data["expect"]["outcome"] = "recovered"
    target.write_text(json.dumps(data), encoding="utf-8")

    code = main(["bench", "--corpus", str(tmp_path)])
    out = capsys.readouterr()
    assert code == 1
    assert "heal-red" in out.out
    assert "deviation" in out.err


def test_cli_bench_reports_a_missing_corpus(tmp_path, capsys):
    assert main(["bench", "--corpus", str(tmp_path / "absent")]) == 1
    assert "bench:" in capsys.readouterr().err


def test_cli_bench_refuses_to_write_a_baseline_from_a_red_run(tmp_path, capsys):
    for path in sorted(CORPUS.glob("*.json")):
        (tmp_path / path.name).write_text(path.read_text(), encoding="utf-8")
    target = tmp_path / "01-implement-green.json"
    data = json.loads(target.read_text())
    data["expect"]["outcome"] = "halted"
    target.write_text(json.dumps(data), encoding="utf-8")
    out_baseline = tmp_path / "baseline.json"

    code = main(["bench", "--corpus", str(tmp_path), "--baseline", str(out_baseline),
                 "--write-baseline"])
    assert code == 1
    assert not out_baseline.exists()
    assert "refusing" in capsys.readouterr().err
