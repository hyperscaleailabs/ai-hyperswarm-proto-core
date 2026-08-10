import json
from types import SimpleNamespace

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


def test_wait_remote_uses_bounded_exponential_backoff(monkeypatch):
    """A slow build is polled with growing (then capped) intervals, never
    hammered every ``interval`` seconds for the whole wait window."""
    clock = {"t": 0.0}
    monkeypatch.setattr(ci, "time", SimpleNamespace(monotonic=lambda: clock["t"]))

    def fake_sleep(seconds):
        sleeps.append(seconds)
        clock["t"] += seconds

    sleeps: list[float] = []

    def fake_runner(cmd, **kwargs):
        rollup = {"statusCheckRollup": [
            {"__typename": "CheckRun", "status": "IN_PROGRESS", "conclusion": None}
        ]}
        return Proc(cmd, 0, json.dumps(rollup), "")

    result = ci.wait_remote(
        1, "o/r", timeout=50, interval=5, max_interval=20, backoff_multiplier=2.0,
        runner=fake_runner, sleep=fake_sleep,
    )

    assert result == ci.TIMEOUT
    # Doubles each round (checks were reporting every time), capped at 20.
    assert sleeps == [5, 10, 20, 15]
    assert max(sleeps) <= 20


def test_wait_remote_does_not_back_off_while_no_checks_have_reported(monkeypatch):
    """An empty rollup ('nothing reported yet') polls at the base interval,
    not an already-inflated one - Actions hasn't even started the run."""
    clock = {"t": 0.0}
    monkeypatch.setattr(ci, "time", SimpleNamespace(monotonic=lambda: clock["t"]))

    def fake_sleep(seconds):
        sleeps.append(seconds)
        clock["t"] += seconds

    sleeps: list[float] = []

    def fake_runner(cmd, **kwargs):
        return Proc(cmd, 0, json.dumps({"statusCheckRollup": []}), "")

    result = ci.wait_remote(
        1, "o/r", timeout=12, interval=5, max_interval=20,
        runner=fake_runner, sleep=fake_sleep,
    )

    assert result == ci.TIMEOUT
    assert sleeps == [5, 5, 2]  # never grows past the base interval


def test_disposition_success_merges():
    d = ci.disposition(ci.SUCCESS)
    assert d.action == ci.MERGE
    assert d.remote == ci.SUCCESS


def test_disposition_failure_recovers():
    d = ci.disposition(ci.FAILURE)
    assert d.action == ci.RECOVER


def test_disposition_timeout_requeues():
    d = ci.disposition(ci.TIMEOUT)
    assert d.action == ci.REQUEUE


def test_disposition_pending_requeues():
    """A bare PENDING (should not normally reach here after `wait_remote`) is
    handled the same defensive way as TIMEOUT - never treated as a FAILURE."""
    d = ci.disposition(ci.PENDING)
    assert d.action == ci.REQUEUE


def test_only_success_ever_merges():
    """No rollup outcome other than SUCCESS may ever produce a MERGE disposition."""
    for remote in (ci.FAILURE, ci.PENDING, ci.TIMEOUT, "UNKNOWN"):
        assert ci.disposition(remote).action != ci.MERGE


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
