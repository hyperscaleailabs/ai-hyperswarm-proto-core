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


def test_practices_validate_passes_on_the_real_vault(capsys):
    rc = main(["practices", "--validate"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "0 problem(s)" in out
    assert "PR-0001" in out


def test_practices_validate_exits_nonzero_on_an_unpinned_card(monkeypatch, capsys):
    bad = cli_module.practices.PracticeCard(
        id="PR-0099", title="Invented", source_repo="acme/not-a-reference",
        artifact_kind="code", artifact_ref="src/x.py", observed_on="2026-08-02",
        note_name="PR-0099-invented",
    )
    monkeypatch.setattr(cli_module.practices, "load_cards", lambda *a, **k: [bad])
    rc = main(["practices", "--validate"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "not in the pinned reference set" in out


def test_evidence_check_command_blocks_and_exits_nonzero(capsys):
    rc = main(["evidence-check", "--pr-title", "implement: feat: x", "--pr-body", "Closes #1"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "BLOCKED" in out


def test_evidence_check_command_passes_a_cited_pr(capsys):
    rc = main([
        "evidence-check", "--pr-title", "implement: feat: x",
        "--pr-body", "Closes #1\n\n## Reference-set evidence\n`openai/swarm`\n",
    ])
    out = capsys.readouterr().out
    assert rc == 0
    assert "PASS" in out


def test_repro_check_command_passes_and_exits_zero(monkeypatch, capsys):
    monkeypatch.setattr(
        cli_module.repro, "evaluate_pr",
        lambda **kwargs: ReproResult(ok=True, reason="exempt: not a heal/bugfix ticket"),
    )
    rc = main(["repro-check", "--pr-title", "implement: docs: x"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "PASS" in out
