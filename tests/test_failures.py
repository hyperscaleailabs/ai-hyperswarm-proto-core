"""The failure taxonomy: one class per iteration, over an ordered rule list.

`classify` is pure, so every case here is a plain value assertion. The
precedence block is the important half: signals co-occur constantly in a real
failure, and the order of the rules is what decides which one the loop acts on.
"""
from hsai import failures
from hsai.failures import (
    AGENT_ERROR,
    BLOCK_IMMEDIATELY,
    DEMOTE_TIER,
    ESCALATE_TIMEOUT,
    GUARD_INCOMPLETE,
    GUARD_NO_REPRO,
    LINT,
    MERGE_CONFLICT,
    NONE,
    REMOTE_INFRA,
    RETRY_SAME_TIER,
    RETRY_WITH_REMEDIATION,
    TEST_FAILURE,
    TIMEOUT,
    UNKNOWN,
    WORKFLOW_TAMPER,
    Signals,
    action_for,
    classify,
    class_from_labels,
    render_failure_table,
)


def _fail(**kw) -> Signals:
    """A failing iteration carrying only the signals the caller names."""
    return Signals(failed=True, **kw)


# --- one class per signal ----------------------------------------------------

def test_clean_iteration_has_no_failure_class():
    clean = Signals(ci_steps={"ruff": True, "pytest": True}, remote_ci="SUCCESS")
    verdict = classify(clean)
    assert verdict.name == NONE
    assert verdict.is_failure is False
    assert verdict.label == ""


def test_lint():
    verdict = classify(_fail(ci_steps={"ruff": False, "pytest": True}))
    assert verdict.name == LINT
    assert "ruff" in verdict.reason
    assert verdict.label == "failure:lint"


def test_test_failure():
    verdict = classify(_fail(ci_steps={"ruff": True, "pytest": False}))
    assert verdict.name == TEST_FAILURE
    assert "pytest" in verdict.reason


def test_timeout_from_the_agent_and_from_the_remote_poll():
    assert classify(
        _fail(agent_ok=False, agent_timed_out=True, agent_error="timeout after 1200s")
    ).name == TIMEOUT
    assert classify(_fail(remote_ci="TIMEOUT")).name == TIMEOUT


def test_guard_incomplete():
    assert classify(_fail(completeness_ok=False)).name == GUARD_INCOMPLETE


def test_guard_no_repro():
    assert classify(_fail(repro_ok=False)).name == GUARD_NO_REPRO
    # A guard that did not apply (None) or passed (True) must not fire.
    assert classify(_fail(repro_ok=None)).name == UNKNOWN
    assert classify(_fail(repro_ok=True)).name == UNKNOWN


def test_workflow_tamper():
    verdict = classify(_fail(workflow_paths=(".github/workflows/ci.yml",)))
    assert verdict.name == WORKFLOW_TAMPER
    assert ".github/workflows/ci.yml" in verdict.reason


def test_merge_conflict():
    assert classify(_fail(merge_conflict=True)).name == MERGE_CONFLICT


def test_remote_infra_is_a_red_remote_over_a_green_local():
    verdict = classify(_fail(ci_steps={"ruff": True, "pytest": True}, remote_ci="FAILURE"))
    assert verdict.name == REMOTE_INFRA
    assert "while local CI was green" in verdict.reason


def test_agent_error_is_the_exit_status_not_the_stderr_text():
    assert classify(_fail(agent_ok=False, agent_error="boom\nmore")).name == AGENT_ERROR
    # A healthy run may still write warnings to stderr - that is not a failure.
    assert classify(
        Signals(agent_ok=True, agent_error="warning: deprecated flag", remote_ci="SUCCESS")
    ).name == NONE


def test_unknown_is_the_floor_for_an_unexplained_failure():
    verdict = classify(_fail())
    assert verdict.name == UNKNOWN
    assert "no recognised signal" in verdict.reason


def test_every_class_in_the_taxonomy_is_reachable():
    """The ten names the ledger, labels and policy all key off."""
    assert set(failures.CLASSES) == {
        LINT, TEST_FAILURE, TIMEOUT, GUARD_INCOMPLETE, GUARD_NO_REPRO,
        WORKFLOW_TAMPER, MERGE_CONFLICT, REMOTE_INFRA, AGENT_ERROR, UNKNOWN,
    }


# --- precedence: what wins when several signals are present at once ----------

def test_precedence_tamper_beats_everything():
    """A worker that moved the goalposts is a safety event, not a build error."""
    verdict = classify(_fail(
        workflow_paths=(".github/workflows/ci.yml",),
        completeness_ok=False,
        repro_ok=False,
        merge_conflict=True,
        agent_ok=False,
        agent_timed_out=True,
        ci_steps={"ruff": False, "pytest": False},
        remote_ci="FAILURE",
    ))
    assert verdict.name == WORKFLOW_TAMPER


def test_precedence_a_guard_verdict_beats_a_ci_signal():
    """The guards reason about the diff; CI only about the tree it produced."""
    assert classify(_fail(
        completeness_ok=False, ci_steps={"ruff": False, "pytest": False},
        remote_ci="FAILURE",
    )).name == GUARD_INCOMPLETE
    assert classify(_fail(
        repro_ok=False, ci_steps={"ruff": True, "pytest": False}, remote_ci="FAILURE",
    )).name == GUARD_NO_REPRO
    # ...and completeness (an earlier rule) beats repro when both fired.
    assert classify(_fail(completeness_ok=False, repro_ok=False)).name == GUARD_INCOMPLETE


def test_precedence_timeout_beats_agent_error():
    """A killed agent also exits non-zero, so agent_error is always present."""
    verdict = classify(_fail(
        agent_timed_out=True, agent_ok=False, agent_error="timeout after 1200s",
        ci_steps={"ruff": True, "pytest": False},
    ))
    assert verdict.name == TIMEOUT


def test_precedence_local_ci_beats_the_remote_conclusion():
    """A concrete local failure explains the red remote build, not vice versa."""
    verdict = classify(_fail(ci_steps={"ruff": True, "pytest": False}, remote_ci="FAILURE"))
    assert verdict.name == TEST_FAILURE


def test_precedence_lint_beats_test_failure_when_both_are_red():
    assert classify(
        _fail(ci_steps={"ruff": False, "pytest": False})
    ).name == LINT


def test_classify_is_pure():
    """Same input, same verdict - and the input is not mutated."""
    signals = _fail(ci_steps={"ruff": True, "pytest": False}, remote_ci="FAILURE")
    before = repr(signals)
    assert classify(signals) == classify(signals)
    assert repr(signals) == before


# --- merge-conflict sniffing -------------------------------------------------

def test_looks_like_merge_conflict():
    for text in (
        "! [rejected] hsai/iter-1 -> hsai/iter-1 (non-fast-forward)",
        "hint: Updates were rejected because the remote contains work",
        "CONFLICT (content): Merge conflict in src/hsai/ai.py",
        "error: cannot lock ref 'refs/heads/x'",
    ):
        assert failures.looks_like_merge_conflict(text) is True
    assert failures.looks_like_merge_conflict("Everything up-to-date") is False
    assert failures.looks_like_merge_conflict("") is False


# --- the retry policy --------------------------------------------------------

def test_default_policy_covers_every_class():
    for name in failures.CLASSES:
        assert name in failures.DEFAULT_RETRY_POLICY
        assert failures.DEFAULT_RETRY_POLICY[name] in failures.ACTIONS


def test_tamper_and_conflict_block_immediately_without_a_retry():
    for name in (WORKFLOW_TAMPER, MERGE_CONFLICT):
        action = action_for(name)
        assert action.name == BLOCK_IMMEDIATELY
        assert action.blocks is True
        assert action.remediate is False


def test_actions_differ_materially_from_one_another():
    assert action_for(TEST_FAILURE).name == RETRY_WITH_REMEDIATION
    assert action_for(TEST_FAILURE).remediate is True
    assert action_for(TIMEOUT).name == ESCALATE_TIMEOUT
    assert action_for(TIMEOUT).escalate is True
    assert action_for(AGENT_ERROR).name == DEMOTE_TIER
    assert action_for(AGENT_ERROR).demote is True
    # retry_same_tier changes nothing about the next attempt.
    plain = action_for(REMOTE_INFRA)
    assert plain.name == RETRY_SAME_TIER
    assert not (plain.blocks or plain.remediate or plain.demote or plain.escalate)


def test_config_overrides_the_default_policy():
    assert action_for(TEST_FAILURE, {TEST_FAILURE: BLOCK_IMMEDIATELY}).blocks is True


def test_a_typo_in_the_policy_never_strands_a_ticket():
    """An unknown action name degrades to a plain retry, not to a crash."""
    assert action_for(LINT, {LINT: "teleport_the_ticket"}).name == RETRY_SAME_TIER
    assert action_for("not_a_class").name == RETRY_SAME_TIER
    # A clean run has no action at all.
    assert action_for("").blocks is False
    assert action_for("").name == "none"


# --- labels: how a class survives between attempts ---------------------------

def test_class_round_trips_through_a_github_label():
    for name in failures.CLASSES:
        assert class_from_labels(["priority:P2", failures.label_for(name)]) == name
    assert class_from_labels(["priority:P2", "attempts:1"]) == ""
    assert class_from_labels(["failure:not_a_real_class"]) == ""
    assert class_from_labels([]) == ""


def test_failure_labels_lists_what_a_fresh_claim_must_clear():
    labels = ["priority:P2", "failure:lint", "attempts:1", "failure:timeout"]
    assert failures.failure_labels(labels) == ["failure:lint", "failure:timeout"]


# --- the shared taxonomy table ----------------------------------------------

def test_render_failure_table_sorts_the_dominant_mode_first():
    table = render_failure_table({TEST_FAILURE: 1, GUARD_INCOMPLETE: 3, LINT: 0})
    lines = table.splitlines()
    assert lines[0] == "| failure class | count |"
    assert lines[2] == "| `guard_incomplete` | 3 |"     # the mode to attack
    assert lines[3] == "| `test_failure` | 1 |"
    assert "lint" not in table                          # zero counts are noise


def test_render_failure_table_says_so_when_a_window_was_clean():
    for counts in ({}, {LINT: 0}, {"": 4}):
        assert "No classified failures" in render_failure_table(counts)


def test_slug_makes_a_branch_name_filesystem_safe():
    assert failures.slug("hsai/iter-17-abc") == "hsai-iter-17-abc"
    assert failures.slug("") == ""
