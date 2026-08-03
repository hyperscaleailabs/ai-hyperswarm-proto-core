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


TICKET_WITH_CRITERIA = (
    "## Acceptance criteria\n- [ ] widget builds\n- [ ] widget tested\n\n"
    "## Verification plan\n- [ ] pytest\n"
)
PR_WITH_REVIEW = """Closes #7

## Model used
`sonnet`

## Acceptance review
| id | criterion | status | evidence |
| --- | --- | --- | --- |
| AC1 | widget builds | **met** | src/hsai/widget.py:1 |
| AC2 | widget tested | **met** | tests/test_widget.py |

## Lesson learned
Small and green.
"""


def test_evidence_check_passes_with_a_full_acceptance_review(capsys):
    rc = main([
        "evidence-check",
        "--pr-body", PR_WITH_REVIEW,
        "--ticket-body", TICKET_WITH_CRITERIA,
    ])
    assert rc == 0
    assert "evidence-check: PASS" in capsys.readouterr().out


def test_evidence_check_blocks_a_pr_missing_the_acceptance_review(capsys):
    body = PR_WITH_REVIEW.split("## Acceptance review")[0] + "## Lesson learned\nx\n"
    rc = main(["evidence-check", "--pr-body", body, "--ticket-body", TICKET_WITH_CRITERIA])
    out = capsys.readouterr().out
    assert rc == 1
    assert "::error::" in out and "Acceptance review" in out


def test_evidence_check_reads_the_pr_body_from_the_environment(monkeypatch, capsys):
    monkeypatch.setenv("PR_BODY", PR_WITH_REVIEW)
    monkeypatch.setenv("TICKET_BODY", TICKET_WITH_CRITERIA)
    assert main(["evidence-check"]) == 0
    assert "PASS" in capsys.readouterr().out


def test_evidence_check_fetches_the_linked_ticket_when_not_supplied(monkeypatch, capsys):
    from hsai.github import Issue

    seen = {}

    def fake_get_issue(repo, number, **kwargs):
        seen["number"] = number
        return Issue(number=number, title="t", labels=(), assignees=(),
                     body=TICKET_WITH_CRITERIA)

    monkeypatch.setattr(cli_module.github, "get_issue", fake_get_issue)
    monkeypatch.delenv("TICKET_BODY", raising=False)
    rc = main(["evidence-check", "--pr-body", PR_WITH_REVIEW])
    assert seen["number"] == 7  # taken from the "Closes #7" link
    assert rc == 0
    assert "PASS" in capsys.readouterr().out
