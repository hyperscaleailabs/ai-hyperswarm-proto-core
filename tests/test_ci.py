from hsai import ci
from hsai.proc import Proc


class FakeRemoteRunner:
    """Answers `gh api .../check-runs` with a canned sequence of conclusions,
    one per call, so :func:`hsai.ci.poll_remote_status` can be exercised
    without ever touching the network.
    """

    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.calls = 0

    def __call__(self, cmd, *, cwd=None, env=None, timeout=None, input_text=None) -> Proc:
        assert cmd[:2] == ["gh", "api"]
        out = self.responses[min(self.calls, len(self.responses) - 1)]
        self.calls += 1
        return Proc(cmd, 0, f"{out}\n", "")


def test_remote_ok_true_for_success_and_benign_conclusions():
    assert ci.remote_ok("success")
    assert ci.remote_ok("success,skipped,neutral")
    assert ci.remote_ok("")  # no check runs at all


def test_remote_ok_false_for_any_failure():
    assert not ci.remote_ok("success,failure")
    assert not ci.remote_ok("failure")
    assert not ci.remote_ok("cancelled")


def test_poll_remote_status_resolves_immediately_when_conclusive():
    runner = FakeRemoteRunner(["success"])
    sleeps: list[float] = []

    status = ci.poll_remote_status(
        "o/r", "branch", runner=runner, sleep=sleeps.append,
    )

    assert status == "success"
    assert runner.calls == 1
    assert sleeps == []  # never had to wait


def test_poll_remote_status_polls_until_conclusive():
    runner = FakeRemoteRunner(["", "null,in_progress", "success,success"])
    sleeps: list[float] = []

    status = ci.poll_remote_status(
        "o/r", "branch", runner=runner, interval=5.0, sleep=sleeps.append,
    )

    assert status == "success,success"
    assert runner.calls == 3
    assert sleeps == [5.0, 5.0]


def test_poll_remote_status_gives_up_after_max_attempts():
    runner = FakeRemoteRunner(["in_progress"])
    sleeps: list[float] = []

    status = ci.poll_remote_status(
        "o/r", "branch", runner=runner, max_attempts=3, sleep=sleeps.append,
    )

    assert status == "in_progress"
    assert runner.calls == 3
    assert sleeps == [10.0, 10.0]
