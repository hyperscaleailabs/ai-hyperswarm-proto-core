import json

from hsai import ci
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
    # Local CI runs ruff + pytest (mirroring .github/workflows/ci.yml) plus a
    # kb-lint pre-flight the remote workflow does not have yet - see the
    # ci.yml patch this ticket's PR body carries for the architect to apply.
    calls = []

    def fake(cmd, **kwargs):
        calls.append(cmd)
        return Proc(cmd, 0, "", "")

    result = ci.run_local(runner=fake)
    assert result.ok
    assert ["ruff", "check", "."] in calls
    assert ["pytest"] in calls
    assert ["hsai", "kb-lint"] in calls
    assert result.steps == {"ruff": True, "pytest": True, "kb-lint": True}


def test_run_local_fails_when_kb_lint_reports_an_error():
    def fake(cmd, **kwargs):
        if cmd == ["hsai", "kb-lint"]:
            return Proc(cmd, 1, "", "knowledge/lessons/x.md:KB001: dangling wikilink\n")
        return Proc(cmd, 0, "", "")

    result = ci.run_local(runner=fake)
    assert not result.ok
    assert result.steps["kb-lint"] is False
