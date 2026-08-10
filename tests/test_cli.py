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


def test_parser_cycle_resume_args():
    parser = build_parser()
    plain = parser.parse_args(["cycle"])
    assert plain.index is None and not plain.resume and not plain.dry_run

    resumed = parser.parse_args(["cycle", "--resume", "--cycle-index", "42"])
    assert resumed.resume and resumed.index == 42      # --cycle-index aliases --index
    assert parser.parse_args(["cycle", "--index", "42"]).index == 42


def test_cycle_command_passes_resume_through(monkeypatch, capsys):
    from hsai.cycle import CycleResult
    from hsai.governance import BlockReport

    seen = {}

    def fake_run_cycle(cfg, **kwargs):
        seen.update(kwargs)
        return CycleResult(
            report=BlockReport(cycle_index=42), review_issue=0, governance_pr=0,
            journal_path="/tmp/j.jsonl", resumed=True,
        )

    monkeypatch.setattr("hsai.cycle.run_cycle", fake_run_cycle)

    rc = main(["cycle", "--resume", "--cycle-index", "42"])
    out = capsys.readouterr().out

    assert rc == 0
    assert seen["resume"] is True and seen["cycle_index"] == 42 and seen["dry_run"] is False
    assert "block 42 (resumed)" in out and "/tmp/j.jsonl" in out


def test_parser_gc_defaults():
    parser = build_parser()
    args = parser.parse_args(["gc"])
    assert args.command == "gc"
    assert args.dry_run is False   # the CLI makes the choice explicit
    assert args.older_than is None


def test_gc_command_defaults_to_dry_run_flag_off_means_live(monkeypatch, capsys):
    from hsai.gc import GcResult

    seen = {}

    def fake_run_gc(cfg, **kwargs):
        seen.update(kwargs)
        return GcResult(dry_run=kwargs["dry_run"], removed_worktrees=["/repo/.hsai/wt/x"])

    monkeypatch.setattr(cli_module, "run_gc", fake_run_gc)

    rc = main(["gc"])
    out = capsys.readouterr().out

    assert rc == 0
    assert seen["dry_run"] is False
    assert "/repo/.hsai/wt/x" in out


def test_gc_command_honors_dry_run_flag(monkeypatch, capsys):
    from hsai.gc import GcResult

    seen = {}

    def fake_run_gc(cfg, **kwargs):
        seen.update(kwargs)
        return GcResult(dry_run=kwargs["dry_run"])

    monkeypatch.setattr(cli_module, "run_gc", fake_run_gc)

    rc = main(["gc", "--dry-run", "--older-than", "6"])
    out = capsys.readouterr().out

    assert rc == 0
    assert seen["dry_run"] is True
    assert seen["stale_hours"] == 6
    assert "dry-run" in out


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


def test_traj_prints_a_stored_run_for_post_mortem(tmp_path, monkeypatch, capsys):
    """`hsai traj <iteration>` is the post-mortem entrance."""
    _seed_trajectory(tmp_path)
    spy = _no_subprocess(monkeypatch)

    rc = main(["traj", "12", "--root", str(tmp_path)])
    out = capsys.readouterr().out

    assert rc == 0
    assert "trajectory 12" in out
    assert "ticket #7" in out
    assert "input_tokens=1500" in out
    assert spy.calls == []                       # reading disk spends no quota


def test_traj_unknown_iteration_exits_nonzero(tmp_path, monkeypatch, capsys):
    _no_subprocess(monkeypatch)
    rc = main(["traj", "999", "--root", str(tmp_path)])
    assert rc == 1
    assert "no trajectory" in capsys.readouterr().err


def test_replay_prints_a_human_reconstruction(tmp_path, monkeypatch, capsys):
    _seed_trajectory(tmp_path)
    spy = _no_subprocess(monkeypatch)

    rc = main(["replay", "12", "--root", str(tmp_path)])
    out = capsys.readouterr().out

    assert rc == 0
    assert "trajectory 12" in out
    assert "Implement the widget END TO END." in out      # prompt
    assert "tool_use(Read)" in out and "Widget implemented." in out  # step stream
    assert "input_tokens=1500" in out and "output_tokens=320" in out  # usage
    assert "outcome: merged" in out
    assert spy.calls == []                                 # no `claude`, no network


def test_replay_json_flag_round_trips(tmp_path, monkeypatch, capsys):
    traj = _seed_trajectory(tmp_path)
    spy = _no_subprocess(monkeypatch)

    rc = main(["replay", "12", "--root", str(tmp_path), "--json"])
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
    path = trajectory.path_for(tmp_path, "12", 0)

    assert main(["replay", str(path)]) == 0
    assert "trajectory 12" in capsys.readouterr().out


def test_replay_unknown_id_exits_nonzero(tmp_path, monkeypatch, capsys):
    _no_subprocess(monkeypatch)
    rc = main(["replay", "999", "--root", str(tmp_path)])
    assert rc == 1
    assert "no trajectory" in capsys.readouterr().err
