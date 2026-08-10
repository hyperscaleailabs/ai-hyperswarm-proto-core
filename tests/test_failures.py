"""The failure taxonomy: pure classification and pure policy lookup."""
from dataclasses import replace

import pytest

from hsai import failures
from hsai.config import load_config, validate

GREEN = {"ruff": True, "pytest": True}
RED_LINT = {"ruff": False, "pytest": True}
RED_TESTS = {"ruff": True, "pytest": False}


# --- one class at a time ------------------------------------------------------
@pytest.mark.parametrize(
    "signals",
    [{}, {"ci_steps": GREEN}, {"ci_steps": GREEN, "remote": "SUCCESS"}],
)
def test_classify_returns_empty_for_a_clean_run(signals):
    """No failure signalled -> no class. `failed` is what asks for a verdict."""
    assert failures.classify(**signals) == ""
    assert failures.classify(failed=True, **signals) == failures.UNKNOWN


@pytest.mark.parametrize(
    "signals, expected",
    [
        # ruff red -> lint
        ({"ci_steps": RED_LINT, "remote": "FAILURE"}, failures.LINT),
        # pytest red -> test_failure
        ({"ci_steps": RED_TESTS, "remote": "FAILURE"}, failures.TEST_FAILURE),
        # proc.run renders an expired subprocess as "timeout after <n>s"
        (
            {"agent_ok": False, "agent_error": "[phase=implement] timeout after 1200s"},
            failures.TIMEOUT,
        ),
        ({"timed_out": True}, failures.TIMEOUT),
        # the completeness guard's verdict
        ({"guard": "incomplete"}, failures.GUARD_INCOMPLETE),
        # the reproduce-before-fix guard's verdict
        ({"guard": "no_repro"}, failures.GUARD_NO_REPRO),
        # a worker edited the checks it is judged by
        ({"workflow_paths": [".github/workflows/ci.yml"]}, failures.WORKFLOW_TAMPER),
        # git's conflict vocabulary, wherever it surfaced
        ({"merge_conflict": True}, failures.MERGE_CONFLICT),
        (
            {"agent_ok": False, "agent_error": "CONFLICT (content): Merge conflict in src/a.py"},
            failures.MERGE_CONFLICT,
        ),
        # local clean, remote red -> the divergence is environmental
        ({"ci_steps": GREEN, "remote": "FAILURE"}, failures.REMOTE_INFRA),
        ({"ci_steps": GREEN, "remote": "TIMEOUT"}, failures.REMOTE_INFRA),
        # the CLI exited non-zero and said nothing more specific
        ({"agent_ok": False, "agent_error": "claude: exited 1"}, failures.AGENT_ERROR),
    ],
)
def test_classify_covers_every_class(signals, expected):
    assert failures.classify(**signals) == expected
    # `failed` only decides the ""/unknown fallback; it never changes a verdict.
    assert failures.classify(failed=True, **signals) == expected


def test_classify_names_at_least_eight_distinct_classes():
    """The taxonomy is only useful if the classifier can actually reach it."""
    reached = {
        failures.classify(failed=True, workflow_paths=["a"]),
        failures.classify(failed=True, merge_conflict=True),
        failures.classify(failed=True, guard="incomplete"),
        failures.classify(failed=True, guard="no_repro"),
        failures.classify(failed=True, timed_out=True),
        failures.classify(failed=True, ci_steps=RED_LINT),
        failures.classify(failed=True, ci_steps=RED_TESTS),
        failures.classify(failed=True, agent_ok=False),
        failures.classify(failed=True, remote="FAILURE"),
        failures.classify(failed=True),
    }
    assert reached == set(failures.CLASSES)
    assert len(reached) >= 8


def test_classify_is_pure():
    """No hidden state: identical signals classify identically, forever."""
    steps = dict(RED_TESTS)
    signals = dict(agent_ok=False, ci_steps=steps, remote="FAILURE", failed=True)
    first = failures.classify(**signals)
    assert [failures.classify(**signals) for _ in range(5)] == [first] * 5
    assert steps == RED_TESTS          # the caller's mapping is never mutated
    assert failures.classify() == ""   # and no argument at all is a clean run


# --- precedence when signals co-occur ----------------------------------------
def test_precedence_tamper_beats_everything():
    """A run that edited the gate cannot be trusted on any other signal."""
    assert failures.classify(
        workflow_paths=[".github/workflows/ci.yml"],
        guard="incomplete",
        merge_conflict=True,
        timed_out=True,
        agent_ok=False,
        ci_steps=RED_TESTS,
        remote="FAILURE",
        failed=True,
    ) == failures.WORKFLOW_TAMPER


def test_precedence_guard_verdict_beats_a_ci_signal():
    """'the work was not done' outranks 'the build is unhappy about it'."""
    assert failures.classify(
        guard="no_repro", ci_steps=RED_LINT, remote="FAILURE", failed=True
    ) == failures.GUARD_NO_REPRO
    assert failures.classify(
        guard="incomplete", ci_steps=RED_TESTS, remote="FAILURE", failed=True
    ) == failures.GUARD_INCOMPLETE
    # ...and with no guard verdict the CI signal is what is left.
    assert failures.classify(
        guard="", ci_steps=RED_TESTS, remote="FAILURE", failed=True
    ) == failures.TEST_FAILURE


def test_precedence_timeout_beats_agent_error():
    """A killed agent also exits non-zero; the specific cause must win."""
    killed = dict(agent_ok=False, agent_error="timeout after 1200s", failed=True)
    assert failures.classify(**killed) == failures.TIMEOUT
    # Same non-zero exit, no timeout in the text -> the generic class.
    assert failures.classify(
        agent_ok=False, agent_error="segfault", failed=True
    ) == failures.AGENT_ERROR


def test_precedence_merge_conflict_beats_ci_and_guard_verdicts():
    assert failures.classify(
        merge_conflict=True, guard="incomplete", ci_steps=RED_TESTS, failed=True
    ) == failures.MERGE_CONFLICT


def test_precedence_lint_beats_test_failure_and_agent_error():
    """ruff runs first, and the cheaper certain fix is the one to report."""
    both_red = {"ruff": False, "pytest": False}
    assert failures.classify(
        agent_ok=False, ci_steps=both_red, remote="FAILURE", failed=True
    ) == failures.LINT


def test_precedence_a_concrete_ci_step_beats_remote_infra():
    assert failures.classify(
        ci_steps=RED_TESTS, remote="FAILURE", failed=True
    ) == failures.TEST_FAILURE


# --- detectors ----------------------------------------------------------------
@pytest.mark.parametrize(
    "text", ["timeout after 1200s", "the agent timed out", "Timed Out waiting", "killed after 30s"]
)
def test_is_timeout_matches_the_family(text):
    assert failures.is_timeout(text)


@pytest.mark.parametrize("text", ["", "everything is fine", "time is a flat circle"])
def test_is_timeout_does_not_over_match(text):
    assert not failures.is_timeout(text)


@pytest.mark.parametrize(
    "text",
    [
        "CONFLICT (content): Merge conflict in src/hsai/ai.py",
        "Automatic merge failed; fix conflicts and then commit the result.",
        "<<<<<<< HEAD",
    ],
)
def test_has_merge_conflict_matches_gits_vocabulary(text):
    assert failures.has_merge_conflict(text)


def test_has_merge_conflict_does_not_over_match():
    assert not failures.has_merge_conflict("resolved a conflict of interest")
    assert not failures.has_merge_conflict("")


# --- retry policy -------------------------------------------------------------
def test_action_for_reads_the_shipped_policy():
    cfg = load_config()
    policy = cfg.retry_policy
    assert policy, ".ai-swarm/core.yaml must ship a retry_policy"

    # The two structural classes block immediately and consume no attempt.
    for cls in (failures.WORKFLOW_TAMPER, failures.MERGE_CONFLICT):
        action = failures.action_for(cls, policy)
        assert action.name == failures.BLOCK_IMMEDIATELY
        assert action.blocks is True
        assert action.consumes_attempt is False

    # A red test is retried, with the previous failure quoted to the next worker.
    test_action = failures.action_for(failures.TEST_FAILURE, policy)
    assert test_action.name == failures.RETRY_WITH_REMEDIATION
    assert test_action.blocks is False
    assert test_action.consumes_attempt is True
    assert test_action.remediate is True

    assert failures.action_for(failures.TIMEOUT, policy).name == failures.ESCALATE_TIMEOUT
    assert failures.action_for(failures.LINT, policy).name == failures.DEMOTE_TIER


def test_action_for_degrades_safely():
    """A policy typo must never strand a ticket - it falls back, never raises."""
    assert failures.action_for("lint", None).name == failures.DEFAULT_ACTION
    assert failures.action_for("lint", {}).name == failures.DEFAULT_ACTION
    # unknown class -> the configured default
    assert failures.action_for(
        "nonsense", {"default": failures.RETRY_WITH_REMEDIATION, "classes": {}}
    ).name == failures.RETRY_WITH_REMEDIATION
    # unknown action name -> the configured default
    assert failures.action_for(
        "lint", {"default": failures.DEMOTE_TIER, "classes": {"lint": "teleport"}}
    ).name == failures.DEMOTE_TIER
    # unknown action AND unknown default -> the hardcoded fallback
    assert failures.action_for(
        "lint", {"default": "nope", "classes": {"lint": "teleport"}}
    ).name == failures.DEFAULT_ACTION


def test_shipped_policy_names_only_known_classes_and_actions():
    cfg = load_config()
    result = validate(cfg)
    assert result.ok
    assert not [w for w in result.warnings if "retry_policy" in w]

    classes = cfg.retry_policy["classes"]
    assert set(classes) == set(failures.CLASSES)   # no class left unrouted
    assert set(classes.values()) <= set(failures.ACTION_NAMES)


def test_validate_warns_about_a_bogus_retry_policy():
    broken = replace(
        load_config(),
        retry_policy={
            "default": "teleport",
            "classes": {"lint": "teleport", "gremlins": "demote_tier"},
        },
    )
    warnings = validate(broken).warnings
    assert any("unknown action 'teleport'" in w and "classes['lint']" in w for w in warnings)
    assert any("unknown failure class 'gremlins'" in w for w in warnings)
    assert any("retry_policy.default has unknown action 'teleport'" in w for w in warnings)


# --- the shared taxonomy table ------------------------------------------------
def test_render_taxonomy_table_orders_by_count():
    table = failures.render_taxonomy_table(
        {"lint": 1, "test_failure": 3, "workflow_tamper": 2}
    )
    lines = table.splitlines()
    assert lines[0] == "| failure class | count |"
    assert lines[1] == "| --- | --- |"
    assert [line.split("`")[1] for line in lines[2:]] == [
        "test_failure", "workflow_tamper", "lint",
    ]


def test_render_taxonomy_table_handles_an_empty_block():
    for counts in (None, {}, {"lint": 0}):
        assert failures.render_taxonomy_table(counts) == (
            "_No failures recorded in this window._"
        )


def test_failure_label():
    assert failures.failure_label(failures.TEST_FAILURE) == "failure:test_failure"
