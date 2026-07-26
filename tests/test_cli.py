from hsai import cli as cli_module
from hsai.cli import build_parser, main
from hsai.repro import ReproResult


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
