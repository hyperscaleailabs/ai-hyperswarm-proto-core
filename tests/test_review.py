"""The adversarial acceptance-criteria review gate.

Covers verdict parsing, the strict schema validator, fail-open behaviour on
garbage / timeouts / errors, tier-offset selection never resolving to heavy,
redaction of persisted artifacts, and the CI-side evidence gate.
"""
import dataclasses
import json
from pathlib import Path

from hsai import review
from hsai.config import load_config
from hsai.proc import Proc
from hsai.review import (
    FAIL,
    INCONCLUSIVE,
    MET,
    PASS,
    SKIPPED,
    UNCLEAR,
    UNMET,
    Criterion,
    build_prompt,
    check_pr_evidence,
    parse_criteria,
    parse_verdict,
    redact,
    render_table,
    reviewer_choice,
    reviewer_tier,
    run_review,
)

TICKET_BODY = """## Problem
Widget missing.

## Proposal
Build the widget.

## Acceptance criteria
- [ ] the widget builds
- [ ] the widget has a test
- [ ] the widget is documented

## Verification plan
- [ ] pytest green
"""

CRITERIA = parse_criteria(TICKET_BODY)


def _block(payload: dict) -> str:
    return "Here is my review.\n\n```json\n" + json.dumps(payload) + "\n```\n"


def _all_met(n: int = 3) -> dict:
    return {
        "verdict": "PASS",
        "criteria": [
            {"id": f"AC{i}", "met": True, "evidence": f"src/hsai/widget.py:{i}"}
            for i in range(1, n + 1)
        ],
        "blocking_reasons": [],
    }


# --- criterion extraction ---------------------------------------------------

def test_parse_criteria_reads_only_the_acceptance_section():
    assert [c.id for c in CRITERIA] == ["AC1", "AC2", "AC3"]
    assert CRITERIA[0].text == "the widget builds"
    # The verification plan's checkboxes are NOT acceptance criteria.
    assert all("pytest" not in c.text for c in CRITERIA)


def test_parse_criteria_empty_without_the_section():
    assert parse_criteria("just do the thing") == ()
    assert parse_criteria("") == ()


def test_parse_criteria_stops_at_the_next_heading_of_any_level():
    body = "## Acceptance criteria\n- [ ] one\n### Notes\n- [ ] not a criterion\n"
    assert [c.text for c in parse_criteria(body)] == ["one"]


# --- verdict parsing + strict validation ------------------------------------

def test_parse_verdict_accepts_a_well_formed_pass():
    verdict = parse_verdict(_block(_all_met()), CRITERIA)
    assert verdict.verdict == PASS
    assert [c.met for c in verdict.criteria] == [MET, MET, MET]
    # criterion text is joined back on from the ticket, not trusted from the model
    assert verdict.criteria[0].text == "the widget builds"
    assert verdict.criteria[1].evidence == "src/hsai/widget.py:2"


def test_parse_verdict_fail_carries_blocking_reasons():
    payload = _all_met()
    payload["verdict"] = "FAIL"
    payload["criteria"][1] = {"id": "AC2", "met": False, "evidence": "no test added"}
    payload["blocking_reasons"] = ["AC2: no test exercises the widget"]
    verdict = parse_verdict(_block(payload), CRITERIA)
    assert verdict.verdict == FAIL
    assert verdict.criteria[1].met == UNMET
    assert [c.id for c in verdict.unmet] == ["AC2"]
    assert verdict.blocking_reasons == ("AC2: no test exercises the widget",)


def test_parse_verdict_revalidates_a_lying_pass():
    """A 'PASS' that reports an unmet criterion is corrected to FAIL."""
    payload = _all_met()
    payload["criteria"][2] = {"id": "AC3", "met": False, "evidence": "no docs"}
    verdict = parse_verdict(_block(payload), CRITERIA)
    assert verdict.verdict == FAIL
    # a reason is synthesized when the model failed to name one
    assert verdict.blocking_reasons
    assert "AC3" in verdict.blocking_reasons[0]


def test_parse_verdict_normalises_ids_and_met_values():
    payload = {
        "verdict": "PASS",
        "criteria": [
            {"id": 1, "met": "yes", "evidence": "a"},
            {"id": "ac-2", "met": "true", "evidence": "b"},
            {"id": "criterion 3", "met": "unclear", "evidence": "c"},
        ],
        "blocking_reasons": [],
    }
    verdict = parse_verdict(_block(payload), CRITERIA)
    assert verdict.verdict == PASS
    assert [c.id for c in verdict.criteria] == ["AC1", "AC2", "AC3"]
    assert [c.met for c in verdict.criteria] == [MET, MET, UNCLEAR]


def test_parse_verdict_uses_the_last_fenced_block():
    early = _block({"verdict": "FAIL", "criteria": [], "blocking_reasons": []})
    verdict = parse_verdict(early + _block(_all_met()), CRITERIA)
    assert verdict.verdict == PASS


def test_parse_verdict_inconclusive_on_garbage():
    for garbage in ("", "ok\n", "I think it looks fine!", "```json\nnot json\n```"):
        verdict = parse_verdict(garbage, CRITERIA)
        assert verdict.verdict == INCONCLUSIVE, garbage
        assert verdict.error


def test_parse_verdict_rejects_a_non_object_payload():
    assert parse_verdict("```json\n[1, 2]\n```", CRITERIA).verdict == INCONCLUSIVE


def test_parse_verdict_rejects_an_unknown_verdict_string():
    payload = _all_met()
    payload["verdict"] = "LGTM"
    assert parse_verdict(_block(payload), CRITERIA).verdict == INCONCLUSIVE


def test_parse_verdict_rejects_missing_or_malformed_criteria():
    no_array = {"verdict": "PASS", "blocking_reasons": []}
    assert parse_verdict(_block(no_array), CRITERIA).verdict == INCONCLUSIVE

    empty = {"verdict": "PASS", "criteria": [], "blocking_reasons": []}
    assert parse_verdict(_block(empty), CRITERIA).verdict == INCONCLUSIVE

    not_objects = {"verdict": "PASS", "criteria": ["AC1 ok"], "blocking_reasons": []}
    assert parse_verdict(_block(not_objects), CRITERIA).verdict == INCONCLUSIVE


def test_parse_verdict_rejects_an_unrecognised_met_value():
    payload = _all_met()
    payload["criteria"][0]["met"] = "probably-ish"
    verdict = parse_verdict(_block(payload), CRITERIA)
    assert verdict.verdict == INCONCLUSIVE
    assert "AC1" in verdict.error


def test_parse_verdict_rejects_a_silently_skipped_criterion():
    """A reviewer cannot pass a change by simply not mentioning a criterion."""
    payload = _all_met(n=2)  # AC3 never reported
    verdict = parse_verdict(_block(payload), CRITERIA)
    assert verdict.verdict == INCONCLUSIVE
    assert "AC3" in verdict.error


def test_parse_verdict_rejects_non_list_blocking_reasons():
    payload = _all_met()
    payload["blocking_reasons"] = "none"
    assert parse_verdict(_block(payload), CRITERIA).verdict == INCONCLUSIVE


def test_parse_verdict_without_expected_criteria_sorts_by_id():
    payload = {
        "verdict": "PASS",
        "criteria": [
            {"id": "AC10", "met": True, "evidence": "x"},
            {"id": "AC2", "met": True, "evidence": "y"},
        ],
        "blocking_reasons": [],
    }
    verdict = parse_verdict(_block(payload))
    assert [c.id for c in verdict.criteria] == ["AC2", "AC10"]


# --- tier-offset selection never resolves to heavy --------------------------

def test_reviewer_tier_is_one_step_below_the_implementation_tier():
    cfg = load_config()
    assert reviewer_tier(cfg, "heavy") == "standard"
    assert reviewer_tier(cfg, "standard") == "light"
    assert reviewer_tier(cfg, "light") == "light"  # floors at the cheapest tier


def test_reviewer_tier_never_resolves_to_heavy_for_any_offset():
    cfg = load_config()
    for offset in (-5, 0, 1, 2, 99):
        cfg_off = dataclasses.replace(cfg, review={**cfg.review, "tier_offset": offset})
        for impl in ("light", "standard", "heavy", "unknown-tier"):
            assert reviewer_tier(cfg_off, impl) != "heavy", (offset, impl)


def test_reviewer_choice_records_why_it_is_cheap():
    cfg = load_config()
    choice = reviewer_choice(cfg, "heavy")
    assert choice.tier == "standard"
    assert choice.model == cfg.tiers["standard"].model
    assert choice.strategy == "review-v1"
    assert "never heavy" in choice.rationale


# --- redaction --------------------------------------------------------------

def test_redact_masks_secret_shaped_env_values():
    env = {
        "ANTHROPIC_API_KEY": "sk-ant-supersecretvalue",
        "GH_TOKEN": "ghp_anothersecret",
        "HOME": "/Users/someone",
    }
    text = "key=sk-ant-supersecretvalue token=ghp_anothersecret home=/Users/someone"
    out = redact(text, load_config(), environ=env)
    assert "sk-ant-supersecretvalue" not in out
    assert "ghp_anothersecret" not in out
    assert "***REDACTED:ANTHROPIC_API_KEY***" in out
    # Non-secret variables stay readable so the artifact is still auditable.
    assert "/Users/someone" in out


def test_redact_is_a_noop_without_matching_values():
    assert redact("plain text", load_config(), environ={"HOME": "/tmp"}) == "plain text"


def test_redact_leaves_short_values_alone():
    """Blind-replacing a 1-character 'secret' would shred the whole artifact."""
    text = "AC1 met at src/hsai/widget.py:1"
    assert redact(text, load_config(), environ={"MY_API_KEY": "1"}) == text


# --- rendering --------------------------------------------------------------

def test_render_table_has_a_row_per_criterion_with_evidence():
    verdict = parse_verdict(_block(_all_met()), CRITERIA)
    table = render_table(verdict)
    assert table.count("\n") == 4  # header + separator + 3 rows
    for criterion in CRITERIA:
        assert f"| {criterion.id} |" in table
        assert criterion.text in table
    assert "src/hsai/widget.py:1" in table


def test_render_table_escapes_pipes_and_newlines():
    verdict = parse_verdict(
        _block({
            "verdict": "PASS",
            "criteria": [{"id": "AC1", "met": True, "evidence": "a | b\nc"}],
            "blocking_reasons": [],
        }),
        (Criterion(id="AC1", text="x | y"),),
    )
    # Pipes are escaped and newlines flattened, so a hostile evidence string
    # cannot break the table out of its four columns.
    row = render_table(verdict).splitlines()[-1]
    assert row == r"| AC1 | x \| y | **met** | a \| b c |"


def test_render_table_without_a_verdict():
    assert "no per-criterion verdict" in render_table(None)


def test_build_prompt_names_every_criterion_and_demands_json():
    pack = review.ReviewPack(
        ticket=7, ticket_title="feat: widget", ticket_body=TICKET_BODY,
        criteria=CRITERIA, diff="+ code", changed_paths=("src/hsai/widget.py",),
        ci_summary="CI green",
    )
    prompt = build_prompt(pack)
    assert "AC1, AC2, AC3" in prompt
    assert "```json" in prompt
    assert "blocking_reasons" in prompt
    assert "adversarial" in prompt.lower()


def test_review_pack_truncates_an_enormous_diff():
    pack = review.ReviewPack(
        ticket=1, ticket_title="t", ticket_body=TICKET_BODY, criteria=CRITERIA,
        diff="x" * 100, changed_paths=(), ci_summary="green", diff_limit=10,
    )
    assert "[diff truncated]" in pack.render()


# --- run_review: fail-open, skip, block -------------------------------------

class _ReviewRunner:
    """Answers the git prologue of ``build_pack`` and then the reviewer call."""

    def __init__(self, *, agent_stdout: str = "", agent_code: int = 0,
                 agent_stderr: str = "") -> None:
        self.agent_stdout = agent_stdout
        self.agent_code = agent_code
        self.agent_stderr = agent_stderr
        self.calls: list[list[str]] = []

    def __call__(self, cmd, *, cwd=None, env=None, timeout=None, input_text=None) -> Proc:
        cmd = list(cmd)
        self.calls.append(cmd)
        if cmd[:1] == ["claude"]:
            return Proc(cmd, self.agent_code, self.agent_stdout, self.agent_stderr)
        if cmd[:2] == ["git", "merge-base"]:
            return Proc(cmd, 0, "basesha\n", "")
        if cmd[:2] == ["git", "diff"]:
            return Proc(cmd, 0, "+++ b/src/hsai/widget.py\n+def widget(): ...\n", "")
        if cmd[:2] == ["git", "status"]:
            return Proc(cmd, 0, " M src/hsai/widget.py\n", "")
        if cmd[:2] == ["git", "add"]:
            return Proc(cmd, 0, "", "")
        raise AssertionError(f"unhandled command {cmd!r}")


def _run(tmp_path, runner, *, cfg=None, ticket_body=TICKET_BODY, impl_tier="standard"):
    return run_review(
        cfg or load_config(), repo_root=str(tmp_path), wt=str(tmp_path),
        ticket=7, ticket_title="feat: widget", ticket_body=ticket_body,
        kind="implement", ci_summary="CI green", impl_tier=impl_tier,
        iteration=3, runner=runner, ai_runner=runner,
    )


def test_run_review_passes_and_persists_an_artifact(tmp_path):
    runner = _ReviewRunner(agent_stdout=_block(_all_met()))
    outcome = _run(tmp_path, runner)

    assert outcome.status == PASS
    assert outcome.blocks is False
    assert outcome.tier == "light" and outcome.model == "haiku"
    assert outcome.seconds >= 0.0

    saved = sorted((tmp_path / review.REVIEWS_DIR).glob("*.json"))
    assert len(saved) == 1 and str(saved[0]) == outcome.artifact
    payload = json.loads(saved[0].read_text())
    assert payload["status"] == PASS
    assert payload["ticket"] == 7 and payload["iteration"] == 3
    assert payload["reviewer"]["tier"] == "light"
    assert [c["id"] for c in payload["verdict"]["criteria"]] == ["AC1", "AC2", "AC3"]
    assert "widget" in payload["pack"]["diff"]


def test_run_review_blocks_on_an_unmet_criterion(tmp_path):
    payload = _all_met()
    payload["verdict"] = "FAIL"
    payload["criteria"][1]["met"] = False
    payload["blocking_reasons"] = ["AC2: no test"]
    outcome = _run(tmp_path, _ReviewRunner(agent_stdout=_block(payload)))

    assert outcome.status == FAIL
    assert outcome.blocks is True
    assert "AC2" in outcome.reason
    section = outcome.render_section()
    assert "**unmet**" in section and "Blocking reasons" in section


def test_run_review_fails_open_on_garbage_output(tmp_path):
    outcome = _run(tmp_path, _ReviewRunner(agent_stdout="looks good to me!"))
    assert outcome.status == INCONCLUSIVE
    assert outcome.blocks is False
    assert "no fenced JSON object" in outcome.reason
    assert "inconclusive" in outcome.render_section()


def test_run_review_fails_open_on_a_timeout(tmp_path):
    # proc.run maps a subprocess timeout onto code 124 with a timeout stderr.
    runner = _ReviewRunner(agent_code=124, agent_stderr="timeout after 600s")
    outcome = _run(tmp_path, runner)
    assert outcome.status == INCONCLUSIVE
    assert outcome.blocks is False
    assert "timed out" in outcome.reason and "timeout after 600s" in outcome.reason


def test_run_review_fails_open_on_an_errored_reviewer(tmp_path):
    runner = _ReviewRunner(agent_code=1, agent_stderr="claude: boom")
    outcome = _run(tmp_path, runner)
    assert outcome.status == INCONCLUSIVE
    assert outcome.blocks is False
    # even a failed review leaves an auditable artifact behind
    assert (tmp_path / review.REVIEWS_DIR).exists()


def test_run_review_fails_open_when_the_reviewer_raises(tmp_path, monkeypatch):
    """Even an exception out of the model layer must not wedge the loop."""
    def boom(*args, **kwargs):
        raise RuntimeError("subscription guard tripped")

    monkeypatch.setattr(review, "run_agent", boom)
    outcome = _run(tmp_path, _ReviewRunner())
    assert outcome.status == INCONCLUSIVE
    assert outcome.blocks is False
    assert "RuntimeError" in outcome.reason


def test_run_review_fail_open_false_turns_inconclusive_into_a_block(tmp_path):
    cfg = load_config()
    cfg = dataclasses.replace(cfg, review={**cfg.review, "fail_open": False})
    outcome = _run(tmp_path, _ReviewRunner(agent_stdout="garbage"), cfg=cfg)
    assert outcome.status == FAIL
    assert outcome.blocks is True
    assert "fail_open is false" in outcome.reason


def test_run_review_skipped_when_disabled(tmp_path):
    cfg = load_config()
    cfg = dataclasses.replace(cfg, review={**cfg.review, "enabled": False})
    runner = _ReviewRunner(agent_stdout=_block(_all_met()))
    outcome = _run(tmp_path, runner, cfg=cfg)

    assert outcome.status == SKIPPED
    assert outcome.blocks is False
    # disabled means NO model is run and NO artifact is written at all
    assert not [c for c in runner.calls if c[:1] == ["claude"]]
    assert not (tmp_path / review.REVIEWS_DIR).exists()
    assert "not applicable" in outcome.render_section()


def test_run_review_skipped_when_the_ticket_has_no_criteria(tmp_path):
    runner = _ReviewRunner(agent_stdout=_block(_all_met()))
    outcome = _run(tmp_path, runner, ticket_body="just fix it somehow")
    assert outcome.status == SKIPPED
    assert not [c for c in runner.calls if c[:1] == ["claude"]]


def test_run_review_stages_before_diffing_so_new_files_are_visible(tmp_path):
    runner = _ReviewRunner(agent_stdout=_block(_all_met()))
    _run(tmp_path, runner)
    add_idx = next(i for i, c in enumerate(runner.calls) if c[:2] == ["git", "add"])
    diff_idx = next(i for i, c in enumerate(runner.calls) if c[:2] == ["git", "diff"])
    assert add_idx < diff_idx
    assert "--cached" in runner.calls[diff_idx]


def test_run_review_never_runs_the_heavy_tier_even_for_heavy_work(tmp_path):
    runner = _ReviewRunner(agent_stdout=_block(_all_met()))
    outcome = _run(tmp_path, runner, impl_tier="heavy")
    assert outcome.tier == "standard"
    claude = next(c for c in runner.calls if c[:1] == ["claude"])
    assert claude[claude.index("--model") + 1] == "sonnet"
    # the reviewer reads and judges; it is never handed edit permission
    assert claude[claude.index("--permission-mode") + 1] == "plan"


def test_run_review_secrets_never_reach_the_artifact(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-leakycanary")
    payload = _all_met()
    payload["criteria"][0]["evidence"] = "found sk-ant-leakycanary in the diff"
    outcome = _run(tmp_path, _ReviewRunner(agent_stdout=_block(payload)))
    text = Path(outcome.artifact).read_text()
    assert "sk-ant-leakycanary" not in text
    assert "***REDACTED:ANTHROPIC_API_KEY***" in text


# --- CI-side evidence gate --------------------------------------------------

GOOD_PR_BODY = """Closes #7

## Model used
- **model**: `sonnet`

## Acceptance review
| id | criterion | status | evidence |
| --- | --- | --- | --- |
| AC1 | the widget builds | **met** | src/hsai/widget.py:1 |
| AC2 | the widget has a test | **met** | tests/test_widget.py |
| AC3 | the widget is documented | **met** | README.md:4 |

## Lesson learned
Kept it small.
"""


def test_evidence_check_accepts_a_complete_pr_body():
    result = check_pr_evidence(GOOD_PR_BODY, ticket_body=TICKET_BODY)
    assert result.ok, result.reasons


def test_evidence_check_still_enforces_the_original_invariants():
    result = check_pr_evidence("no evidence at all", ticket_body="")
    assert not result.ok
    assert any("Closes #N" in r for r in result.reasons)
    assert any("Model used" in r for r in result.reasons)
    assert any("Lesson learned" in r for r in result.reasons)


def test_evidence_check_requires_the_review_section_for_criteria_tickets():
    body = GOOD_PR_BODY.split("## Acceptance review")[0] + "## Lesson learned\nx\n"
    result = check_pr_evidence(body, ticket_body=TICKET_BODY)
    assert not result.ok
    assert any("Acceptance review" in r for r in result.reasons)


def test_evidence_check_requires_a_row_per_criterion():
    body = GOOD_PR_BODY.replace(
        "| AC3 | the widget is documented | **met** | README.md:4 |\n", ""
    )
    result = check_pr_evidence(body, ticket_body=TICKET_BODY)
    assert not result.ok
    assert any("AC3" in r for r in result.reasons)


def test_evidence_check_does_not_demand_a_review_for_criteria_free_tickets():
    body = "Closes #3\n\n## Model used\n`haiku`\n\n## Lesson learned\nfine\n"
    assert check_pr_evidence(body, ticket_body="chore: bump a pin").ok


def test_evidence_check_ignores_review_rows_outside_the_section():
    """A table pasted under a later heading must not satisfy the gate."""
    body = (
        "Closes #7\n\n## Model used\n`sonnet`\n\n## Acceptance review\n_(nothing)_\n\n"
        "## Lesson learned\n| AC1 | x | **met** | y |\n| AC2 | x | **met** | y |\n"
        "| AC3 | x | **met** | y |\n"
    )
    result = check_pr_evidence(body, ticket_body=TICKET_BODY)
    assert not result.ok
    assert any("AC1" in r for r in result.reasons)
