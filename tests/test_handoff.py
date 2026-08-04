import json

from hsai import handoff
from hsai.handoff import Handoff
from hsai.proc import Proc


class _CommentRunner:
    """Minimal fake `gh` answering the two calls handoff.py makes: posting an
    issue comment and reading an issue's comments back."""

    def __init__(self) -> None:
        self.comments: dict[int, list[str]] = {}

    def __call__(self, cmd, *, cwd=None, env=None, timeout=None, input_text=None) -> Proc:
        cmd = list(cmd)
        if cmd[:3] == ["gh", "issue", "comment"]:
            num = int(cmd[3])
            body = cmd[cmd.index("--body") + 1]
            self.comments.setdefault(num, []).append(body)
            return Proc(cmd, 0, "", "")
        if cmd[:3] == ["gh", "issue", "view"]:
            num = int(cmd[3])
            fields = cmd[cmd.index("--json") + 1]
            assert fields == "comments"
            data = {"comments": [{"body": b} for b in self.comments.get(num, [])]}
            return Proc(cmd, 0, json.dumps(data), "")
        raise AssertionError(f"unhandled command {cmd!r}")


def _handoff(**overrides) -> Handoff:
    defaults = dict(
        attempt=1,
        tier="standard",
        model="sonnet",
        remote_ci="FAILURE",
        failing_steps=("pytest",),
        agent_error="boom: assertion failed",
        changed_paths=("src/hsai/foo.py",),
        trajectory_id="1-7",
    )
    defaults.update(overrides)
    return Handoff(**defaults)


def test_handoff_round_trips_through_a_posted_comment():
    runner = _CommentRunner()
    h = _handoff()
    handoff.post("o/r", 7, h, runner=runner)

    got = handoff.read_latest("o/r", 7, runner=runner)
    assert got == h


def test_posted_comment_carries_the_stable_heading_and_fenced_json():
    runner = _CommentRunner()
    handoff.post("o/r", 7, _handoff(), runner=runner)

    body = runner.comments[7][0]
    assert body.startswith(handoff.HEADING)
    assert "```json" in body
    payload = json.loads(body.split("```json", 1)[1].split("```", 1)[0])
    assert payload["attempt"] == 1
    assert payload["remote_ci"] == "FAILURE"


def test_read_latest_returns_none_when_there_are_no_comments():
    runner = _CommentRunner()
    assert handoff.read_latest("o/r", 7, runner=runner) is None


def test_read_latest_tolerates_unrelated_comments():
    runner = _CommentRunner()
    runner.comments[7] = ["just a human leaving a note, no heading here"]
    assert handoff.read_latest("o/r", 7, runner=runner) is None


def test_read_latest_tolerates_malformed_json():
    runner = _CommentRunner()
    runner.comments[7] = [f"{handoff.HEADING}\n\n```json\n{{not valid json\n```\n"]
    assert handoff.read_latest("o/r", 7, runner=runner) is None


def test_read_latest_tolerates_a_handoff_missing_required_fields():
    runner = _CommentRunner()
    runner.comments[7] = [f"{handoff.HEADING}\n\n```json\n{{\"attempt\": 1}}\n```\n"]
    assert handoff.read_latest("o/r", 7, runner=runner) is None


def test_read_latest_picks_the_most_recent_handoff():
    runner = _CommentRunner()
    first = _handoff(attempt=1, remote_ci="FAILURE")
    second = _handoff(attempt=2, remote_ci="TIMEOUT")
    handoff.post("o/r", 7, first, runner=runner)
    handoff.post("o/r", 7, second, runner=runner)

    got = handoff.read_latest("o/r", 7, runner=runner)
    assert got == second


def test_read_latest_skips_unrelated_comments_interleaved_with_a_handoff():
    runner = _CommentRunner()
    handoff.post("o/r", 7, _handoff(), runner=runner)
    runner.comments[7].append("a human reply, not a handoff")

    got = handoff.read_latest("o/r", 7, runner=runner)
    assert got == _handoff()


def test_render_evidence_includes_the_key_fields_for_the_prompt():
    evidence = _handoff().render_evidence()
    assert "attempt 1" in evidence
    assert "standard" in evidence
    assert "sonnet" in evidence
    assert "FAILURE" in evidence
    assert "pytest" in evidence
    assert "src/hsai/foo.py" in evidence
    assert "1-7" in evidence
    assert "hsai replay 1-7" in evidence
    assert "boom: assertion failed" in evidence


def test_render_evidence_omits_empty_optional_fields():
    minimal = Handoff(attempt=1, tier="light", model="haiku", remote_ci="TIMEOUT")
    evidence = minimal.render_evidence()
    assert "attempt 1" in evidence
    assert "TIMEOUT" in evidence
    assert "failing local CI steps" not in evidence
    assert "files touched" not in evidence
    assert "trajectory" not in evidence
    assert "agent error excerpt" not in evidence


def test_clip_error_bounds_long_text():
    long_text = "x" * 2000
    clipped = handoff.clip_error(long_text)
    assert len(clipped) < len(long_text)
    assert clipped.startswith("x" * 100)
    assert "chars]" in clipped


def test_clip_error_tolerates_none():
    assert handoff.clip_error(None) == ""
    assert handoff.clip_error("") == ""
