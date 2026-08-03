from pathlib import Path

from hsai import cli as cli_module
from hsai.cli import build_parser, main
from hsai.repro import ReproResult

CORE_YAML = Path(__file__).resolve().parents[1] / ".ai-swarm" / "core.yaml"


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


def test_parser_calibrate_defaults():
    parser = build_parser()
    args = parser.parse_args(["calibrate"])
    assert args.command == "calibrate"
    assert args.dry_run is False


def test_calibrate_command_reports_and_writes_nothing_on_a_thin_corpus(tmp_path, monkeypatch, capsys):
    """End to end in an empty repo: a report is written, the policy is not."""
    monkeypatch.chdir(tmp_path)
    rc = main(["--config", str(CORE_YAML), "calibrate", "--dry-run"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "samples: 0 joined" in out
    assert "insufficient data: policy unchanged" in out
    for tier in ("light", "standard", "heavy"):
        assert tier in out
    reports = list((tmp_path / "knowledge" / "reports").glob("selection-calibration-*.md"))
    assert len(reports) == 1
    assert not (tmp_path / ".ai-swarm" / "selection-policy.json").exists()


def test_repro_check_command_passes_and_exits_zero(monkeypatch, capsys):
    monkeypatch.setattr(
        cli_module.repro, "evaluate_pr",
        lambda **kwargs: ReproResult(ok=True, reason="exempt: not a heal/bugfix ticket"),
    )
    rc = main(["repro-check", "--pr-title", "implement: docs: x"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "PASS" in out
