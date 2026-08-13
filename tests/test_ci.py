import json

from hsai import ci
from hsai.cli import build_parser, cmd_ci
from hsai.config import load_config
from hsai.proc import Proc


def test_rollup_success():
    rollup = [{"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SUCCESS"}]
    assert ci._rollup_result(rollup) == ci.SUCCESS


def test_rollup_failure():
    rollup = [{"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "FAILURE"}]
    assert ci._rollup_result(rollup) == ci.FAILURE


def test_rollup_pending_when_empty():
    assert ci._rollup_result([]) == ci.PENDING


def test_rollup_pending_when_incomplete():
    rollup = [{"__typename": "CheckRun", "status": "IN_PROGRESS", "conclusion": None}]
    assert ci._rollup_result(rollup) == ci.PENDING


def test_rollup_status_context_failure():
    assert ci._rollup_result([{"__typename": "StatusContext", "state": "FAILURE"}]) == ci.FAILURE


def test_rollup_mixed_one_failure_fails():
    rollup = [
        {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SUCCESS"},
        {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "FAILURE"},
    ]
    assert ci._rollup_result(rollup) == ci.FAILURE


def _runner_returning(concl):
    def fake(cmd, **kwargs):
        rollup = {"statusCheckRollup": [
            {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": concl}
        ]}
        return Proc(cmd, 0, json.dumps(rollup), "")
    return fake


def test_wait_remote_returns_conclusive_result():
    result = ci.wait_remote(1, "o/r", runner=_runner_returning("SUCCESS"), sleep=lambda *_: None)
    assert result == ci.SUCCESS


def test_wait_remote_times_out_when_pending():
    def fake(cmd, **kwargs):
        rollup = {"statusCheckRollup": [
            {"__typename": "CheckRun", "status": "IN_PROGRESS", "conclusion": None}
        ]}
        return Proc(cmd, 0, json.dumps(rollup), "")

    result = ci.wait_remote(1, "o/r", timeout=0, interval=0, runner=fake, sleep=lambda *_: None)
    assert result == ci.TIMEOUT


def test_run_local_matches_workflow_steps():
    # Local CI runs exactly ruff + pytest, mirroring .github/workflows/ci.yml.
    calls = []

    def fake(cmd, **kwargs):
        calls.append(cmd)
        return Proc(cmd, 0, "", "")

    result = ci.run_local(runner=fake)
    assert result.ok
    assert ["ruff", "check", "."] in calls
    assert ["pytest"] in calls


# --- the CI contract: one manifest, two entry points ----------------------------

def _recording_runner(fail: set[str] | None = None):
    calls: list[list[str]] = []
    fail = fail or set()

    def fake(cmd, **kwargs):
        cmd = list(cmd)
        calls.append(cmd)
        return Proc(cmd, 1 if cmd[0] in fail else 0, "", "")

    return fake, calls


def _write_manifest(tmp_path, steps_yaml: str) -> str:
    core = tmp_path / ".ai-swarm" / "core.yaml"
    core.parent.mkdir(parents=True, exist_ok=True)
    core.write_text(
        "identity:\n  name: t\n  owner: o\n"
        "models:\n  tiers:\n    standard:\n      model: sonnet\n  default_tier: standard\n"
        f"ci:\n  steps:\n{steps_yaml}"
    )
    return str(core)


MUTATED_STEPS = (
    "    - id: ruff\n      command: [ruff, check, .]\n      scope: both\n"
    "    - id: pytest\n      command: [pytest]\n      scope: both\n"
    "    - id: shellcheck\n      command: [shellcheck, scripts/x.sh]\n      scope: local\n"
    "    - id: remote-only\n      command: [hsai, evidence-check]\n      scope: remote\n"
)


def test_run_local_executes_the_declared_manifest(tmp_path):
    # Mutating the manifest changes what run_local runs: the contract is data.
    _write_manifest(tmp_path, MUTATED_STEPS)
    runner, calls = _recording_runner()

    result = ci.run_local(cwd=str(tmp_path), runner=runner)

    assert result.ok
    assert calls == [["ruff", "check", "."], ["pytest"], ["shellcheck", "scripts/x.sh"]]
    assert [r.id for r in result.records] == ["ruff", "pytest", "shellcheck"]


def test_cli_ci_local_runs_the_same_steps_as_run_local(tmp_path):
    # Acceptance: `hsai ci --scope local` and ci.run_local are one code path.
    config_path = _write_manifest(tmp_path, MUTATED_STEPS)

    local_runner, local_calls = _recording_runner()
    ci.run_local(cwd=str(tmp_path), runner=local_runner)

    args = build_parser().parse_args(
        ["--config", config_path, "ci", "--scope", "local", "--cwd", str(tmp_path)]
    )
    cli_runner, cli_calls = _recording_runner()
    rc = cmd_ci(args, runner=cli_runner)

    assert rc == 0
    assert cli_calls == local_calls


def test_cli_ci_remote_scope_selects_remote_steps(tmp_path):
    config_path = _write_manifest(tmp_path, MUTATED_STEPS)
    args = build_parser().parse_args(["--config", config_path, "ci", "--scope", "remote"])
    runner, calls = _recording_runner()

    assert cmd_ci(args, runner=runner) == 0
    assert calls == [["ruff", "check", "."], ["pytest"], ["hsai", "evidence-check"]]


def test_cli_ci_json_matches_the_manifest(tmp_path, capsys):
    config_path = _write_manifest(tmp_path, MUTATED_STEPS)
    args = build_parser().parse_args(
        ["--config", config_path, "ci", "--scope", "local", "--json"]
    )
    runner, _ = _recording_runner()

    cmd_ci(args, runner=runner)

    payload = json.loads(capsys.readouterr().out)
    declared = load_config(config_path).ci_steps
    expected = [list(s.command) for s in declared if s.in_scope("local")]
    assert [s["command"] for s in payload["steps"]] == expected
    assert payload["ok"] is True


def test_cli_ci_returns_nonzero_when_a_required_step_is_red(tmp_path):
    config_path = _write_manifest(tmp_path, MUTATED_STEPS)
    args = build_parser().parse_args(["--config", config_path, "ci", "--scope", "local"])
    runner, _ = _recording_runner(fail={"pytest"})

    assert cmd_ci(args, runner=runner) == 1


def test_optional_step_failure_does_not_redden_the_build(tmp_path):
    _write_manifest(
        tmp_path,
        "    - id: ruff\n      command: [ruff, check, .]\n      scope: both\n"
        "    - id: flaky\n      command: [flaky]\n      scope: local\n      required: false\n",
    )
    runner, _ = _recording_runner(fail={"flaky"})

    result = ci.run_local(cwd=str(tmp_path), runner=runner)

    assert result.ok
    assert result.steps == {"ruff": True, "flaky": False}


def test_run_local_falls_back_when_no_manifest_is_reachable(tmp_path):
    # An ephemeral worktree without a core.yaml must still lint and test.
    runner, calls = _recording_runner()
    ci.run_local(cwd=str(tmp_path / "nowhere"), runner=runner)
    assert calls == [["ruff", "check", "."], ["pytest"]]


def test_missing_pr_evidence_reports_each_gap():
    assert ci.missing_pr_evidence("") == [msg for _, msg in ci.PR_EVIDENCE_RULES]
    compliant = "Closes #12\n\n## Model used\nsonnet\n\n## Lesson learned\nnote"
    assert ci.missing_pr_evidence(compliant) == []
    assert len(ci.missing_pr_evidence("Closes #12\n## Model used\nsonnet")) == 1
