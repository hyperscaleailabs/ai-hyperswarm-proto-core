"""Deterministic replay: the committed trajectories are the regression corpus.

Every test here runs the *real* `orchestrator.run_once` - the same code path a
live iteration takes - with every subprocess answered from a recording. If any
of them ever shells out, the spy in `_no_subprocess` fails the test.
"""
import json
from dataclasses import replace
from pathlib import Path

import pytest

from hsai import replay, trajectory
from hsai.config import load_config
from hsai.orchestrator import run_once
from hsai.replay import PromptDriftError, ReplayError, make_runner
from hsai.trajectory import TrajectoryEvent

FIXTURES = Path(__file__).parent / "fixtures" / "trajectories"
GREEN = FIXTURES / "implement-green.jsonl"
RED = FIXTURES / "heal-red.jsonl"


@pytest.fixture
def cfg():
    return load_config()


class _RunnerSpy:
    """Refuses - loudly - any attempt to leave the process."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(list(cmd))
        raise AssertionError(f"replay must not run a subprocess: {cmd!r}")


@pytest.fixture(autouse=True)
def _no_subprocess(monkeypatch):
    spy = _RunnerSpy()
    for target in ("hsai.proc.run", "hsai.ai.run", "hsai.orchestrator.run"):
        monkeypatch.setattr(target, spy)
    return spy


def _event(**kw) -> TrajectoryEvent:
    base = dict(
        timestamp="2026-08-11T00:00:00+00:00", iteration="7", phase="ci-before",
        command=["ruff", "check", "."], exit_code=0, stdout="ok\n",
    )
    base.update(kw)
    return TrajectoryEvent(**base)


def _write(tmp_path, events, name="t.jsonl") -> Path:
    path = tmp_path / name
    path.write_text("".join(e.to_json() + "\n" for e in events), encoding="utf-8")
    return path


# --- the committed corpus ---------------------------------------------------

def test_green_fixture_replays_a_merged_iteration(cfg, tmp_path, _no_subprocess):
    """Acceptance: the green fixture reproduces a passing IterationResult."""
    result = replay.replay_iteration(cfg, GREEN, repo_dir=str(tmp_path), strict=True)

    assert result.kind == "implement"
    assert result.ticket == 4242 and result.pr == 4243
    assert result.ci_before and result.ci_after
    assert result.merged is True and result.recovered is False
    assert result.remote == "SUCCESS"
    assert _no_subprocess.calls == []            # no claude, no git push, no gh


def test_green_replay_really_executed_the_loop(cfg, tmp_path):
    """The replay is not a parse: it leaves the iteration's real artifacts."""
    replay.replay_iteration(cfg, GREEN, repo_dir=str(tmp_path), strict=True)

    lessons = list((tmp_path / ".hsai" / "worktrees").rglob("knowledge/lessons/*.md"))
    assert lessons, "the replayed iteration should have written its lesson"
    assert "feat: cover the widget module with tests" in lessons[0].read_text()
    records = (tmp_path / "knowledge" / "ledger" / "iterations.jsonl").read_text()
    assert '"outcome": "merged"' in records
    # Token counts survive the round trip: the ledger's columns come from the
    # recorded `claude -p` envelope, not from a live call.
    assert '"input_tokens": 4120' in records


def test_red_fixture_replays_a_blocked_heal(cfg, tmp_path, _no_subprocess):
    result = replay.replay_iteration(cfg, RED, repo_dir=str(tmp_path), strict=True)

    assert result.kind == "heal"
    assert result.ticket == 4321
    assert result.ci_before is False
    assert result.recovered is True and result.merged is False
    assert result.pr is None                     # blocked before a PR was opened
    assert any("does not pass on the fix branch" in n for n in result.notes)
    assert _no_subprocess.calls == []


def test_both_fixtures_are_well_formed():
    """Every committed fixture is parseable and self-consistent."""
    for path in (GREEN, RED):
        events = replay.load_events(path)
        assert events, f"{path.name} records no events"
        assert len({e.iteration for e in events}) == 1
        agents = [e for e in events if e.is_agent]
        assert len(agents) == 1, f"{path.name} should record exactly one agent run"
        # The stored hash must describe the stored prompt, so a reader can
        # verify a fixture without trusting whoever curated it.
        assert agents[0].prompt_sha256 == trajectory.sha256_of(agents[0].prompt)
        assert agents[0].phase.startswith("agent:")
        for event in events:
            assert event.command, "every event names the command it recorded"


def test_fixtures_carry_no_absolute_home_paths():
    """Committed recordings are shareable: redaction ran before they landed."""
    for path in (GREEN, RED):
        text = path.read_text(encoding="utf-8")
        assert "/Users/" not in text and "/home/" not in text


# --- drift detection --------------------------------------------------------

def _with_drifted_prompt(tmp_path, source=GREEN, digest="") -> Path:
    """Copy a fixture with the recorded agent prompt hash moved off its prompt."""
    events = replay.load_events(source)
    for event in events:
        if event.is_agent:
            event.prompt_sha256 = digest or trajectory.sha256_of("a template that moved on")
    return _write(tmp_path, events, name="drift.jsonl")


def test_prompt_drift_names_the_offending_phase(cfg, tmp_path):
    path = _with_drifted_prompt(tmp_path)

    with pytest.raises(PromptDriftError) as exc:
        replay.replay_iteration(cfg, path, repo_dir=str(tmp_path), strict=True)

    assert "prompt drift" in str(exc.value)
    assert "agent:implement" in str(exc.value)      # the offending phase
    assert "re-record" in str(exc.value)


def test_prompt_drift_is_fatal_even_without_strict(cfg, tmp_path):
    """A cassette that tolerates prompt drift is worse than no cassette."""
    path = _with_drifted_prompt(tmp_path, digest="0" * 64)

    with pytest.raises(PromptDriftError):
        replay.replay_iteration(cfg, path, repo_dir=str(tmp_path), strict=False)


def test_command_drift_names_the_offending_phase(tmp_path):
    path = _write(tmp_path, [_event(phase="ci-before", command=["ruff", "check", "."])])
    runner = make_runner(path)

    with pytest.raises(ReplayError) as exc:
        runner(["pytest"])
    assert "command drift" in str(exc.value)
    assert "ci-before" in str(exc.value)


def test_an_agent_call_where_none_was_recorded_is_drift(tmp_path):
    """An extra `claude -p` is a harness change, not a prompt change."""
    path = _write(tmp_path, [_event(phase="ci-after")])
    runner = make_runner(path)

    with pytest.raises(ReplayError) as exc:
        runner(["claude", "-p", "hello", "--model", "sonnet"])
    assert type(exc.value) is ReplayError            # not a PromptDriftError
    assert "command drift" in str(exc.value) and "ci-after" in str(exc.value)


def test_a_missing_agent_call_is_drift(tmp_path):
    path = _write(tmp_path, [
        _event(phase="agent:implement", command=["claude", "-p", "do it"], prompt="do it"),
    ])
    runner = make_runner(path)

    with pytest.raises(ReplayError) as exc:
        runner(["pytest"])
    assert "command drift" in str(exc.value) and "agent:implement" in str(exc.value)


def test_matching_prompt_replays_the_recorded_stdout(tmp_path):
    path = _write(tmp_path, [
        _event(phase="agent:implement", command=["claude", "-p", "do the thing"],
               prompt="do the thing", stdout='{"result": "done"}'),
    ])
    runner = make_runner(path, strict=True)

    proc = runner(["claude", "-p", "do the thing", "--model", "sonnet"])

    assert proc.ok and proc.stdout == '{"result": "done"}'
    runner.finish()


# --- strictness -------------------------------------------------------------

def test_strict_rejects_commands_the_recording_does_not_cover(tmp_path):
    path = _write(tmp_path, [_event()])
    runner = make_runner(path, strict=True)
    runner(["ruff", "check", "."])

    with pytest.raises(ReplayError) as exc:
        runner(["pytest"])
    assert "exhausted" in str(exc.value)


def test_lenient_replay_tolerates_an_extra_command(tmp_path):
    path = _write(tmp_path, [_event()])
    runner = make_runner(path)
    runner(["ruff", "check", "."])

    proc = runner(["pytest"])
    assert proc.ok and proc.stdout == ""


def test_strict_rejects_a_replay_that_stopped_early(tmp_path):
    path = _write(tmp_path, [_event(), _event(phase="ci-after", command=["pytest"])])
    runner = make_runner(path, strict=True)
    runner(["ruff", "check", "."])

    with pytest.raises(ReplayError) as exc:
        runner.finish()
    assert "never replayed" in str(exc.value)
    assert "ci-after" in str(exc.value)


def test_repo_root_is_rebound_so_a_replay_stays_in_its_sandbox(tmp_path):
    path = _write(tmp_path, [
        _event(command=["git", "rev-parse", "--show-toplevel"], stdout="/recorded/tree\n"),
    ])
    runner = make_runner(path, repo_root=str(tmp_path))

    assert runner(["git", "rev-parse", "--show-toplevel"]).stdout.strip() == str(tmp_path)


def test_empty_and_missing_trajectories_are_refused(tmp_path):
    (tmp_path / "empty.jsonl").write_text("", encoding="utf-8")
    with pytest.raises(ValueError):
        make_runner(tmp_path / "empty.jsonl")
    with pytest.raises(FileNotFoundError):
        make_runner(tmp_path / "absent.jsonl")


# --- recording is the inverse of replay -------------------------------------

def test_replaying_with_recording_on_reproduces_the_trajectory(cfg, tmp_path):
    """Record(replay(x)) == x: the two halves agree on the same event stream."""
    recorded_dir = tmp_path / "store"
    live = replace(cfg, trajectories={
        "enabled": True, "dir": str(recorded_dir), "retention_count": 0,
    })
    runner = make_runner(GREEN, strict=True, repo_root=str(tmp_path))

    run_once(live, repo_dir=str(tmp_path), runner=runner, ai_runner=runner,
             iteration=9001)

    again = replay.load_events(recorded_dir / "9001.jsonl")
    original = replay.load_events(GREEN)
    assert [e.command[0] for e in again] == [e.command[0] for e in original]
    assert [e.phase for e in again] == [e.phase for e in original]
    assert [e.exit_code for e in again] == [e.exit_code for e in original]
    # The prompt survives verbatim - which is exactly why drift is detectable.
    assert [e.prompt_sha256 for e in again] == [e.prompt_sha256 for e in original]


def test_a_recorded_replay_replays_again(cfg, tmp_path):
    """The re-recording is itself a valid fixture (round trip closes)."""
    store = tmp_path / "store"
    live = replace(cfg, trajectories={
        "enabled": True, "dir": str(store), "retention_count": 0,
    })
    first = make_runner(GREEN, strict=True, repo_root=str(tmp_path))
    run_once(live, repo_dir=str(tmp_path), runner=first, ai_runner=first, iteration=9001)

    second = tmp_path / "second"
    second.mkdir()
    result = replay.replay_iteration(
        cfg, store / "9001.jsonl", repo_dir=str(second), strict=True
    )
    assert result.merged is True and result.ticket == 4242


def test_replay_iteration_reports_how_much_it_consumed(cfg, tmp_path):
    result = replay.replay_iteration(cfg, RED, repo_dir=str(tmp_path), strict=True)
    total = len(replay.load_events(RED))
    assert f"replayed {total}/{total} recorded event(s)" in result.notes


def test_replay_does_not_record_itself(cfg, tmp_path):
    """A replay must not grow the local store with runs that never happened."""
    replay.replay_iteration(cfg, GREEN, repo_dir=str(tmp_path), strict=True)
    assert not (tmp_path / cfg.trajectories_dir).exists()


def test_fixture_events_survive_a_json_round_trip():
    for path in (GREEN, RED):
        for line in path.read_text(encoding="utf-8").splitlines():
            event = TrajectoryEvent.from_dict(json.loads(line))
            assert TrajectoryEvent.from_dict(json.loads(event.to_json())) == event
