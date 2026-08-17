"""Failure taxonomy, Pareto analysis, and the postmortem backlog trigger."""
from __future__ import annotations

import json

from hsai.config import load_config
from hsai.ledger import LedgerRecord
from hsai.postmortem import (
    AGENT_ERROR,
    AGENT_TIMEOUT,
    BUDGET_HALT,
    FAILURE_CLASSES,
    INCOMPLETE_DIFF,
    LINT_FAIL,
    MERGE_CONFLICT,
    NO_REPRO,
    REMOTE_CI_FAIL,
    REMOTE_CI_TIMEOUT,
    TEST_FAIL,
    UNKNOWN,
    FailureEvidence,
    build_postmortem_ticket,
    classify,
    classify_with_detail,
    default_detail,
    dominant_failure,
    file_postmortem_ticket,
    pareto_table,
    postmortem_ticket_title,
    render_pareto_table,
)
from hsai.proc import Proc
from hsai.tickets import check_well_formed

# --- classify(): one branch per closed-vocabulary member ---------------------


def test_vocabulary_has_eleven_closed_members():
    assert len(FAILURE_CLASSES) == 11
    assert len(set(FAILURE_CLASSES)) == 11  # no duplicates


def test_classify_agent_timeout_from_the_proc_timeout_marker():
    # hsai.proc.run stamps a timed-out subprocess's stderr with exactly this.
    ev = FailureEvidence(agent_ok=False, agent_error="timeout after 1200s")
    assert classify(ev) == AGENT_TIMEOUT


def test_classify_agent_error_when_the_agent_run_failed():
    ev = FailureEvidence(agent_ok=False, agent_error="claude: command exited 1")
    assert classify(ev) == AGENT_ERROR


def test_classify_incomplete_diff():
    ev = FailureEvidence(completeness_ok=False)
    assert classify(ev) == INCOMPLETE_DIFF


def test_classify_no_repro():
    ev = FailureEvidence(repro_ok=False)
    assert classify(ev) == NO_REPRO


def test_classify_review_blocked_is_agent_error():
    """No dedicated class for a blocked review: the diff the agent produced
    was substantively wrong, which is what agent_error already means."""
    ev = FailureEvidence(review_approved=False)
    assert classify(ev) == AGENT_ERROR


def test_classify_lint_fail():
    ev = FailureEvidence(ci_steps={"ruff": False, "pytest": True})
    assert classify(ev) == LINT_FAIL


def test_classify_test_fail():
    ev = FailureEvidence(ci_steps={"ruff": True, "pytest": False})
    assert classify(ev) == TEST_FAIL


def test_classify_remote_ci_fail():
    ev = FailureEvidence(ci_steps={"ruff": True, "pytest": True}, remote_ci="FAILURE")
    assert classify(ev) == REMOTE_CI_FAIL


def test_classify_remote_ci_timeout():
    ev = FailureEvidence(ci_steps={"ruff": True, "pytest": True}, remote_ci="TIMEOUT")
    assert classify(ev) == REMOTE_CI_TIMEOUT


def test_classify_merge_conflict():
    ev = FailureEvidence(merge_conflict=True)
    assert classify(ev) == MERGE_CONFLICT


def test_classify_budget_halt():
    ev = FailureEvidence(budget_halted=True)
    assert classify(ev) == BUDGET_HALT


def test_classify_unknown_is_an_explicit_fallback_not_a_silent_default():
    ev = FailureEvidence()  # nothing observed - every field at its "fine" default
    assert classify(ev) == UNKNOWN


def test_classify_ordering_prefers_the_earlier_guard():
    """budget_halted outranks everything; agent failure outranks a red local
    CI step map - mirroring the order guards actually run in orchestrator."""
    ev = FailureEvidence(
        budget_halted=True, merge_conflict=True, agent_ok=False,
        ci_steps={"ruff": False}, remote_ci="FAILURE",
    )
    assert classify(ev) == BUDGET_HALT

    ev2 = FailureEvidence(agent_ok=False, ci_steps={"ruff": False}, remote_ci="FAILURE")
    assert classify(ev2) == AGENT_ERROR


def test_default_detail_is_never_empty_for_any_class():
    for cls in FAILURE_CLASSES:
        assert default_detail(cls, FailureEvidence()).strip()


def test_classify_with_detail_combines_both():
    fclass, detail = classify_with_detail(FailureEvidence(ci_steps={"pytest": False}))
    assert fclass == TEST_FAIL
    assert detail == "pytest failed"


# --- pareto_table / render_pareto_table --------------------------------------

def _f(block, cls, iteration, ticket=None):
    return LedgerRecord(
        iteration=iteration, block=block, ticket=ticket, kind="implement",
        tier="standard", model="sonnet", wall_clock_seconds=5.0, attempts=1,
        outcome="recovered", failure_class=cls,
    )


def test_pareto_table_counts_shares_and_exemplars_ranked_by_count():
    records = [
        _f(1, REMOTE_CI_FAIL, 101, ticket=7),
        _f(1, REMOTE_CI_FAIL, 102, ticket=8),
        _f(1, TEST_FAIL, 103, ticket=9),
        _f(1, "", 104),                          # merged: no failure_class, never counted
        _f(2, AGENT_TIMEOUT, 201),               # different block: excluded
    ]
    rows = pareto_table(records, block=1)
    assert [r.failure_class for r in rows] == [REMOTE_CI_FAIL, TEST_FAIL]
    assert rows[0].count == 2 and rows[0].share == 2 / 3
    assert rows[0].exemplar_iteration == 101 and rows[0].exemplar_ticket == 7
    assert rows[1].count == 1 and rows[1].share == 1 / 3


def test_pareto_table_ties_break_alphabetically():
    records = [_f(1, TEST_FAIL, 101), _f(1, LINT_FAIL, 102)]
    rows = pareto_table(records, block=1)
    assert [r.failure_class for r in rows] == [LINT_FAIL, TEST_FAIL]


def test_pareto_table_empty_when_no_failures():
    assert pareto_table([_f(1, "", 101)], block=1) == []
    assert pareto_table([], block=1) == []


def test_render_pareto_table_formats_markdown():
    rows = pareto_table([_f(1, REMOTE_CI_FAIL, 101, ticket=7)], block=1)
    text = render_pareto_table(rows)
    assert "| class | count | share | exemplar |" in text
    assert "`remote_ci_fail`" in text and "100%" in text
    assert "iteration 101 (ticket #7)" in text


def test_render_pareto_table_empty():
    assert "no failure-class records" in render_pareto_table([])


# --- dominant_failure: both ceilings required --------------------------------

def test_dominant_failure_requires_both_ratio_and_count():
    rows = pareto_table([_f(1, TEST_FAIL, 101), _f(1, TEST_FAIL, 102)], block=1)
    assert rows[0].share == 1.0
    # ratio clears, count (2) does not clear a min_count of 3
    assert dominant_failure(rows, ratio_threshold=0.4, min_count=3) is None

    # The top class by count (test_fail, 3) clears min_count but its SHARE of
    # this larger, more mixed block (3/10 = 30%) does not clear a 0.4 ratio.
    rows3 = pareto_table(
        [_f(1, TEST_FAIL, i) for i in (101, 102, 103)]
        + [_f(1, LINT_FAIL, i) for i in (104, 105)]
        + [_f(1, AGENT_ERROR, i) for i in (106, 107)]
        + [_f(1, NO_REPRO, i) for i in (108, 109)]
        + [_f(1, INCOMPLETE_DIFF, 110)],
        block=1,
    )
    assert rows3[0].failure_class == TEST_FAIL and rows3[0].share == 0.3
    assert dominant_failure(rows3, ratio_threshold=0.4, min_count=3) is None

    rows_dominant = pareto_table([_f(1, TEST_FAIL, i) for i in range(101, 104)], block=1)
    row = dominant_failure(rows_dominant, ratio_threshold=0.4, min_count=3)
    assert row is not None and row.failure_class == TEST_FAIL


def test_dominant_failure_empty_rows_is_none():
    assert dominant_failure([], ratio_threshold=0.1, min_count=1) is None


# --- ticket construction: stable title, well-formed body ---------------------

def test_postmortem_ticket_title_is_stable_across_blocks():
    """The dedup key is per-class, not per-block, so a persistent cause across
    many blocks gets exactly one open ticket."""
    assert postmortem_ticket_title(TEST_FAIL) == postmortem_ticket_title(TEST_FAIL)
    assert "test_fail" in postmortem_ticket_title(TEST_FAIL)
    assert postmortem_ticket_title(TEST_FAIL) != postmortem_ticket_title(LINT_FAIL)


def test_build_postmortem_ticket_is_well_formed_and_priority_p1():
    rows = pareto_table([_f(1, REMOTE_CI_FAIL, i, ticket=7) for i in (101, 102, 103)], block=1)
    row = rows[0]
    spec = build_postmortem_ticket(row, block=1, ratio_threshold=0.4, min_count=3)
    assert spec.title == postmortem_ticket_title(REMOTE_CI_FAIL)
    wf = check_well_formed(spec.title, spec.render())
    assert wf.ok, wf.reasons
    assert "priority:P1" in spec.all_labels()
    assert "iteration 101" in spec.problem


# --- file_postmortem_ticket: threshold gate + title-based dedup -------------

class _FakeRunner:
    def __init__(self, *, open_issues=None):
        self.open_issues = open_issues or []
        self.calls: list[list[str]] = []
        self._issue_seq = 900

    def __call__(self, cmd, *, cwd=None, env=None, env_remove=None, timeout=None, input_text=None):
        cmd = list(cmd)
        self.calls.append(cmd)
        if cmd[:3] == ["gh", "issue", "list"]:
            return Proc(cmd, 0, json.dumps(self.open_issues), "")
        if cmd[:3] == ["gh", "issue", "create"]:
            self._issue_seq += 1
            return Proc(cmd, 0, f"https://github.com/o/r/issues/{self._issue_seq}\n", "")
        raise AssertionError(f"unhandled command {cmd!r}")


def _cfg(**postmortem_overrides):
    cfg = load_config()
    cfg.postmortem.clear()
    cfg.postmortem.update({"ratio_threshold": 0.4, "min_count": 3, **postmortem_overrides})
    return cfg


def test_file_postmortem_ticket_noop_below_threshold():
    cfg = _cfg()
    runner = _FakeRunner()
    records = [_f(1, TEST_FAIL, i) for i in (101, 102)]  # count=2 < min_count=3

    filed = file_postmortem_ticket(cfg, records, block=1, runner=runner)

    assert filed == 0
    assert not any(c[:3] == ["gh", "issue", "create"] for c in runner.calls)


def test_file_postmortem_ticket_files_once_when_dominant():
    cfg = _cfg()
    runner = _FakeRunner()
    records = [_f(1, TEST_FAIL, i, ticket=7) for i in (101, 102, 103)]

    filed = file_postmortem_ticket(cfg, records, block=1, runner=runner)

    assert filed > 0
    create = next(c for c in runner.calls if c[:3] == ["gh", "issue", "create"])
    title = create[create.index("--title") + 1]
    assert title == postmortem_ticket_title(TEST_FAIL)
    assert "priority:P1" in create


def test_file_postmortem_ticket_deduped_against_an_open_ticket_with_the_same_title():
    cfg = _cfg()
    title = postmortem_ticket_title(TEST_FAIL)
    runner = _FakeRunner(
        open_issues=[{"number": 42, "title": title, "labels": [], "assignees": [], "body": ""}]
    )
    records = [_f(1, TEST_FAIL, i) for i in (101, 102, 103)]

    filed = file_postmortem_ticket(cfg, records, block=1, runner=runner)

    assert filed == 0
    assert not any(c[:3] == ["gh", "issue", "create"] for c in runner.calls)


def test_file_postmortem_ticket_thresholds_are_config_driven():
    """Lowering min_count in cfg.postmortem (not code) changes the outcome."""
    cfg = _cfg(min_count=2)
    runner = _FakeRunner()
    records = [_f(1, TEST_FAIL, i) for i in (101, 102)]

    filed = file_postmortem_ticket(cfg, records, block=1, runner=runner)

    assert filed > 0


def test_core_yaml_configures_postmortem_thresholds():
    cfg = load_config()
    assert cfg.postmortem["ratio_threshold"] > 0
    assert cfg.postmortem["min_count"] >= 1
