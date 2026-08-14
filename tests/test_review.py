"""The independent review gate: parsing, fail-closed behaviour, and metering."""
import json
from dataclasses import replace

from hsai import ledger, review
from hsai.config import load_config
from hsai.models import ModelChoice
from hsai.proc import Proc

AUTHOR = ModelChoice(tier="standard", model="sonnet", rationale="x")

TICKET_BODY = """## Problem
The widget is missing.

## Proposal
Build it.

## Acceptance criteria
- [ ] the widget builds
- [x] the widget is tested

## Verification plan
- [ ] pytest green
"""

# Three shapes of reviewer output, as recorded from real runs.
CLEAN_JSON = """```json
{"approve": true, "blocking": [], "advisory": ["nit: name the helper"],
 "rationale": "Both criteria are covered by code plus a test."}
```"""

JSON_IN_PROSE = """I read the diff against the ticket.

The new module is scoped and the test proves the behaviour. One thought on
naming, but nothing that should hold the change.

```json
{"approve": true, "blocking": [], "advisory": [], "rationale": "Scoped and tested."}
```

(That is my verdict.)
"""

GARBAGE = "Looks fine to me, ship it! No JSON here, sorry."


def _envelope(text: str, *, tokens: tuple[int, int] = (400, 60)) -> str:
    """A `claude -p --output-format json` envelope wrapping ``text``."""
    return json.dumps(
        {
            "type": "result",
            "result": text,
            "usage": {"input_tokens": tokens[0], "output_tokens": tokens[1]},
        }
    )


APPROVING_ENVELOPE = _envelope(CLEAN_JSON)


class _ReviewRunner:
    """Answers git/claude for the gate; records every command it was asked."""

    def __init__(self, *, output: str = APPROVING_ENVELOPE, ok: bool = True) -> None:
        self.output = output
        self.ok = ok
        self.calls: list[list[str]] = []

    def __call__(
        self, cmd, *, cwd=None, env=None, env_remove=None, timeout=None, input_text=None
    ) -> Proc:
        cmd = list(cmd)
        self.calls.append(cmd)
        if cmd[:2] == ["git", "diff"] and "--name-only" in cmd:
            return Proc(cmd, 0, "src/hsai/widget.py\ntests/test_widget.py\n", "")
        if cmd[:2] == ["git", "diff"]:
            return Proc(cmd, 0, "diff --git a/src/hsai/widget.py\n+def widget():\n", "")
        if cmd[:1] == ["claude"]:
            return Proc(cmd, 0 if self.ok else 1, self.output, "" if self.ok else "boom")
        raise AssertionError(f"unexpected command {cmd!r}")

    @property
    def claude_calls(self) -> list[list[str]]:
        return [c for c in self.calls if c[:1] == ["claude"]]


def _review(cfg, root, runner) -> review.ReviewVerdict:
    return review.review_change(
        cfg,
        repo_root=str(root), wt=str(root), base_ref="parentsha",
        ticket_title="feat: add widget", ticket_body=TICKET_BODY,
        author=AUTHOR, iteration=3, block=0, ticket=7,
        runner=runner, ai_runner=runner,
    )


# --- parse_verdict: the fail-closed contract --------------------------------

def test_parse_verdict_accepts_clean_json_and_json_wrapped_in_prose():
    clean = review.parse_verdict(CLEAN_JSON)
    assert clean.approve is True
    assert clean.blocking == []
    assert clean.advisory == ["nit: name the helper"]
    assert "criteria" in clean.rationale
    assert clean.status == "approve"

    wrapped = review.parse_verdict(JSON_IN_PROSE)
    assert wrapped.approve is True
    assert wrapped.rationale == "Scoped and tested."


def test_parse_verdict_is_fail_closed_on_garbage_and_on_silence():
    for output in (GARBAGE, "", "   ", "```json\n{not json at all}\n```", "```json\n[1,2]\n```"):
        verdict = review.parse_verdict(output)
        assert verdict.approve is False, output
        assert verdict.blocking, "a non-approval must name why"
        assert verdict.status == "blocked"


def test_parse_verdict_reads_the_last_block_and_distrusts_a_bare_approve_flag():
    # A reviewer that "approves" while listing blocking findings is not an
    # approval: the findings are evidence, the flag is only a claim.
    verdict = review.parse_verdict(
        '```json\n{"approve": true, "blocking": ["src/x.py drops the ticket check"]}\n```'
    )
    assert verdict.approve is False
    assert verdict.blocking == ["src/x.py drops the ticket check"]

    # Withholding approval without evidence still blocks, with a stated reason.
    empty = review.parse_verdict('```json\n{"approve": false}\n```')
    assert empty.approve is False and empty.blocking

    # The LAST block wins, exactly like synthesis.parse_ticket_specs.
    last = review.parse_verdict(
        '```json\n{"approve": false, "blocking": ["draft"]}\n```\n'
        'On reflection:\n```json\n{"approve": true, "rationale": "fine"}\n```'
    )
    assert last.approve is True


def test_acceptance_criteria_are_parsed_off_the_ticket():
    assert review.acceptance_criteria(TICKET_BODY) == [
        "the widget builds",
        "the widget is tested",
    ]
    # No acceptance section: nothing to check off, but never a crash.
    assert review.acceptance_criteria("just a sentence") == []


def test_prompt_carries_the_ticket_criteria_and_the_diff():
    prompt = review.build_prompt(
        ticket_title="feat: add widget",
        ticket_body=TICKET_BODY,
        criteria=review.acceptance_criteria(TICKET_BODY),
        paths=["src/hsai/widget.py"],
        diff="+def widget(): ...",
        author=AUTHOR,
    )
    assert review.PROMPT_MARKER in prompt
    assert "- the widget builds" in prompt
    assert "src/hsai/widget.py" in prompt
    assert "+def widget(): ..." in prompt
    assert "`sonnet`" in prompt        # the reviewer knows who wrote it


# --- review_change: metering, skipping, and the verdict ----------------------

def test_review_change_approves_and_appends_a_review_ledger_record(tmp_path):
    cfg = load_config()
    runner = _ReviewRunner()

    verdict = _review(cfg, tmp_path, runner)

    assert verdict.approve is True and verdict.status == "approve"
    assert verdict.reviewer_model and verdict.reviewer_tier
    assert verdict.reviewer_tier != AUTHOR.tier

    records = ledger.read_records(ledger.ledger_path(cfg, tmp_path))
    assert [r.kind for r in records] == ["review"]
    assert records[0].outcome == "approve"
    assert records[0].ticket == 7 and records[0].iteration == 3
    assert records[0].input_tokens == 400 and records[0].output_tokens == 60
    assert records[0].model == verdict.reviewer_model

    # ...and the block aggregate counts the review's spend (G4 economics).
    agg = ledger.aggregate_block(records, block=0)
    assert agg.iterations == 1 and agg.total_tokens == 460
    assert agg.tier_counts == {verdict.reviewer_tier: 1}


def test_review_change_blocks_on_unparseable_output_and_records_it(tmp_path):
    cfg = load_config()
    runner = _ReviewRunner(output=_envelope(GARBAGE))

    verdict = _review(cfg, tmp_path, runner)

    assert verdict.approve is False
    assert verdict.blocking == [review.FAIL_CLOSED]
    records = ledger.read_records(ledger.ledger_path(cfg, tmp_path))
    assert [(r.kind, r.outcome) for r in records] == [("review", "blocked")]


def test_a_crashed_reviewer_blocks_rather_than_waving_the_change_through(tmp_path):
    cfg = load_config()
    runner = _ReviewRunner(output="", ok=False)

    verdict = _review(cfg, tmp_path, runner)

    assert verdict.approve is False
    assert any("reviewer run failed" in b for b in verdict.blocking)


def test_blocking_findings_are_capped_by_config(tmp_path):
    cfg = replace(load_config(), review={"enabled": True, "max_blocking_findings": 2})
    findings = [f"finding {i}" for i in range(5)]
    runner = _ReviewRunner(
        output=_envelope(
            "```json\n" + json.dumps({"approve": False, "blocking": findings}) + "\n```"
        )
    )

    verdict = _review(cfg, tmp_path, runner)

    assert verdict.blocking[:2] == ["finding 0", "finding 1"]
    assert len(verdict.blocking) == 3
    assert "3 further blocking finding(s) elided" in verdict.blocking[-1]


def test_a_hard_budget_breach_skips_the_review_instead_of_deadlocking(tmp_path):
    """A budget-exhausted block must still be able to finish its in-flight PR."""
    cfg = load_config()
    path = ledger.ledger_path(cfg, tmp_path)
    for i in range(cfg.budget["max_heavy_iterations_per_block"]):
        ledger.append_record(path, ledger.LedgerRecord(
            iteration=i, block=0, ticket=1, kind="implement", tier="heavy",
            model="opus", wall_clock_seconds=1.0, attempts=1, outcome="merged",
        ))
    runner = _ReviewRunner()

    verdict = _review(cfg, tmp_path, runner)

    assert verdict.skipped is True
    assert verdict.approve is True            # the gate is additive, never a deadlock
    assert verdict.status == "skipped"
    assert "hard budget breach" in verdict.rationale
    assert runner.claude_calls == []          # and it spent nothing to say so
    assert [r.kind for r in ledger.read_records(path)] == ["implement"] * 3


def test_disabling_the_gate_skips_it_entirely(tmp_path):
    cfg = replace(load_config(), review={"enabled": False})
    runner = _ReviewRunner()

    verdict = _review(cfg, tmp_path, runner)

    assert verdict.skipped is True and verdict.approve is True
    assert runner.calls == []
    assert ledger.read_records(ledger.ledger_path(cfg, tmp_path)) == []


def test_the_gate_is_enabled_by_default_in_core_yaml():
    cfg = load_config()
    assert review.is_enabled(cfg) is True
    assert cfg.review["tier_policy"]["heavy"] == "standard"
    assert int(cfg.review["timeout_seconds"]) > 0


# --- rendering: the verdict is recorded verbatim (G2) ------------------------

def test_verdict_renders_for_the_pr_body_and_the_lesson():
    verdict = review.ReviewVerdict(
        approve=False, blocking=["src/hsai/x.py: criterion 2 has no test"],
        advisory=["rename `foo`"], rationale="One criterion is unproven.",
        reviewer_model="haiku", reviewer_tier="light",
    )
    rendered = verdict.render()
    assert "**BLOCKED**" in rendered
    assert "`haiku`" in rendered
    assert "src/hsai/x.py: criterion 2 has no test" in rendered
    assert "rename `foo`" in rendered
    assert "One criterion is unproven." in rendered
    assert verdict.summary().startswith("blocked by `haiku`")

    approved = review.ReviewVerdict(approve=True, reviewer_model="sonnet")
    assert "**APPROVED**" in approved.render()
    assert "_(none)_" in approved.render()

    skipped = review.skip_review("disabled")
    assert skipped.render() == "_(not run: disabled)_"
    assert skipped.summary() == "skipped (disabled)"
