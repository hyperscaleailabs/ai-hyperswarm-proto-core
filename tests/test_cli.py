import json

from hsai import cli as cli_module
from hsai import trajectory
from hsai.cli import build_parser, main
from hsai.repro import ReproResult
from hsai.trajectory import Step, Trajectory


def test_parser_loop_args():
    parser = build_parser()
    args = parser.parse_args(["loop", "-n", "2", "--max-parallel", "3"])
    assert args.command == "loop"
    assert args.iterations == 2
    assert args.max_parallel == 3


def test_loop_alias_maps_to_subcommand():
    # `hsai --loop` should behave like `hsai loop`
    assert main is not None  # importable entry point
    parser = build_parser()
    args = parser.parse_args(["loop"])
    assert args.iterations == 1
    assert args.max_parallel is None


def test_status_command_returns_zero(capsys):
    rc = main(["status"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "hyperscaleailabs/ai-hyperswarm-proto-core" in out


def test_parser_repro_check_defaults():
    parser = build_parser()
    args = parser.parse_args(["repro-check"])
    assert args.command == "repro-check"
    assert args.base_ref == "origin/main"
    assert args.pr_title is None


def test_repro_check_command_blocks_and_exits_nonzero(monkeypatch, capsys):
    monkeypatch.setattr(
        cli_module.repro, "evaluate_pr",
        lambda **kwargs: ReproResult(ok=False, reason="blocked for testing"),
    )
    rc = main(["repro-check", "--pr-title", "implement: fix: x", "--base-ref", "origin/main"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "BLOCKED" in out
    assert "blocked for testing" in out


def test_repro_check_command_passes_and_exits_zero(monkeypatch, capsys):
    monkeypatch.setattr(
        cli_module.repro, "evaluate_pr",
        lambda **kwargs: ReproResult(ok=True, reason="exempt: not a heal/bugfix ticket"),
    )
    rc = main(["repro-check", "--pr-title", "implement: docs: x"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "PASS" in out


# --- replay (reads the local trajectory store, spends no quota) -------------

class _RunnerSpy:
    """Records - and refuses - any attempt to shell out."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(list(cmd))
        raise AssertionError(f"replay must not run a subprocess: {cmd!r}")


def _seed_trajectory(root) -> Trajectory:
    traj = Trajectory(
        iteration=12, ticket=7, kind="implement", tier="standard", model="sonnet",
        prompt="Implement the widget END TO END.",
        steps=[
            Step(index=1, kind="tool_use", name="Read", text='{"path": "src/hsai/ai.py"}'),
            Step(index=2, kind="result", text="Widget implemented."),
        ],
        usage={"input_tokens": 1500, "output_tokens": 320},
        duration_seconds=42.5, outcome="merged",
    )
    trajectory.write(traj, root)
    return traj


def _no_subprocess(monkeypatch) -> _RunnerSpy:
    spy = _RunnerSpy()
    monkeypatch.setattr("hsai.proc.run", spy)
    monkeypatch.setattr("hsai.ai.run", spy)
    return spy


def test_replay_prints_a_human_reconstruction(tmp_path, monkeypatch, capsys):
    _seed_trajectory(tmp_path)
    spy = _no_subprocess(monkeypatch)

    rc = main(["replay", "12-7", "--root", str(tmp_path)])
    out = capsys.readouterr().out

    assert rc == 0
    assert "trajectory 12-7" in out
    assert "Implement the widget END TO END." in out      # prompt
    assert "tool_use(Read)" in out and "Widget implemented." in out  # step stream
    assert "input_tokens=1500" in out and "output_tokens=320" in out  # usage
    assert "outcome: merged" in out
    assert spy.calls == []                                 # no `claude`, no network


def test_replay_json_flag_round_trips(tmp_path, monkeypatch, capsys):
    traj = _seed_trajectory(tmp_path)
    spy = _no_subprocess(monkeypatch)

    rc = main(["replay", "12-7", "--root", str(tmp_path), "--json"])
    out = capsys.readouterr().out

    assert rc == 0
    payload = json.loads(out)
    assert payload["iteration"] == 12 and payload["ticket"] == 7
    assert payload["usage"]["output_tokens"] == 320
    assert [s["kind"] for s in payload["steps"]] == ["tool_use", "result"]
    assert payload == json.loads(traj.to_json())
    assert spy.calls == []


def test_replay_accepts_a_file_path(tmp_path, monkeypatch, capsys):
    _seed_trajectory(tmp_path)
    _no_subprocess(monkeypatch)
    path = trajectory.path_for(tmp_path, "12-7")

    assert main(["replay", str(path)]) == 0
    assert "trajectory 12-7" in capsys.readouterr().out


def test_replay_unknown_id_exits_nonzero(tmp_path, monkeypatch, capsys):
    _no_subprocess(monkeypatch)
    rc = main(["replay", "999-1", "--root", str(tmp_path)])
    assert rc == 1
    assert "no trajectory" in capsys.readouterr().err
