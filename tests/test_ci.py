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


def test_wait_remote_backs_off_once_checks_are_running(monkeypatch):
    """Once GitHub reports checks (a non-empty rollup), the wait between polls
    grows - a slow-but-healthy build shouldn't be hammered every `interval`."""
    times = iter([0.0, 1.0, 3.0, 7.0, 15.0])
    monkeypatch.setattr(ci.time, "monotonic", lambda: next(times))
    waits: list[float] = []

    def fake(cmd, **kwargs):
        rollup = {"statusCheckRollup": [
            {"__typename": "CheckRun", "status": "IN_PROGRESS", "conclusion": None}
        ]}
        return Proc(cmd, 0, json.dumps(rollup), "")

    result = ci.wait_remote(
        1, "o/r", timeout=10, interval=1, backoff_factor=2.0,
        runner=fake, sleep=waits.append,
    )

    assert result == ci.TIMEOUT
    assert waits == [1, 2, 3]  # doubling each round, capped by the remaining time


def test_wait_remote_does_not_back_off_before_checks_are_reported(monkeypatch):
    """An empty rollup means GitHub hasn't registered checks yet - poll at the
    base cadence rather than compounding a delay on top of a delay."""
    times = iter([0.0, 1.0, 3.0, 5.0, 20.0])
    monkeypatch.setattr(ci.time, "monotonic", lambda: next(times))
    waits: list[float] = []

    def fake(cmd, **kwargs):
        return Proc(cmd, 0, json.dumps({"statusCheckRollup": []}), "")

    result = ci.wait_remote(
        1, "o/r", timeout=10, interval=1, backoff_factor=2.0,
        runner=fake, sleep=waits.append,
    )

    assert result == ci.TIMEOUT
    assert waits == [1, 1, 1]


def test_wait_remote_max_timeout_extends_the_ceiling(monkeypatch):
    """`max_timeout` is a hard ceiling above the base `timeout` - polling must
    continue past the base timeout as long as it hasn't been reached."""
    times = iter([0.0, 10.0, 25.0])
    monkeypatch.setattr(ci.time, "monotonic", lambda: next(times))
    calls: list[list[str]] = []

    def fake(cmd, **kwargs):
        calls.append(cmd)
        rollup = {"statusCheckRollup": [
            {"__typename": "CheckRun", "status": "IN_PROGRESS", "conclusion": None}
        ]}
        return Proc(cmd, 0, json.dumps(rollup), "")

    result = ci.wait_remote(
        1, "o/r", timeout=5, max_timeout=20, interval=1,
        runner=fake, sleep=lambda *_: None,
    )

    assert result == ci.TIMEOUT
    # a second poll happened at t=10s, past the base 5s timeout - proving the
    # 20s ceiling (not the 5s base) governed the wait
    assert len(calls) == 2


def test_disposition_success_merges():
    d = ci.disposition(ci.SUCCESS)
    assert d.action == ci.MERGE
    assert d.should_merge is True
    assert d.remote == ci.SUCCESS


def test_disposition_failure_recovers():
    d = ci.disposition(ci.FAILURE)
    assert d.action == ci.RECOVER
    assert d.should_merge is False


def test_disposition_timeout_requeues():
    d = ci.disposition(ci.TIMEOUT)
    assert d.action == ci.REQUEUE
    assert d.should_merge is False


def test_disposition_pending_recovers_defensively():
    # `wait_remote` itself never returns PENDING, but `disposition` must still
    # refuse to merge if it ever did - defense in depth for the merge gate.
    d = ci.disposition(ci.PENDING)
    assert d.action == ci.RECOVER
    assert d.should_merge is False


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
