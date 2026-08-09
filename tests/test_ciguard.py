"""Unit tests for the governed CI-workflow change channel.

Every branch of :func:`hsai.ciguard.classify_workflow_diff` is exercised here
with plain text in and a verdict out - no git, no network, no `gh`.
"""
from pathlib import Path

import pytest

from hsai import ci, ciguard
from hsai.ciguard import (
    CIPolicy,
    WorkflowParseError,
    check_parity,
    classify_workflow_diff,
    parse_workflow,
    policy_from_config,
    workflow_commands,
    workflow_paths,
)
from hsai.config import load_config

REPO_ROOT = Path(__file__).resolve().parents[1]

WORKFLOW = ".github/workflows/ci.yml"
LABELS = ("priority:P2", "ci-change")

BASE_WORKFLOW = """\
name: CI
on:
  pull_request:
jobs:
  ci:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Install
        run: |
          python -m pip install --upgrade pip
          pip install -e ".[dev]"
      - name: Lint (ruff)
        run: ruff check .
      - name: Test (pytest)
        run: pytest
"""

# A legal edit: caps the job's runtime - no gate moves in either direction.
TIMEOUT_WORKFLOW = BASE_WORKFLOW.replace(
    "    runs-on: ubuntu-latest",
    "    runs-on: ubuntu-latest\n    timeout-minutes: 20",
)

# Illegal: the pytest gate is gone from the remote lane.
NO_PYTEST_WORKFLOW = BASE_WORKFLOW.replace(
    "      - name: Test (pytest)\n        run: pytest\n", ""
)

# Illegal: a remote-only gate (`mypy`) that `ci.run_local` does not run - the
# exact divergence that motivated the original blanket revert.
MYPY_WORKFLOW = BASE_WORKFLOW + """\
      - name: Types (mypy)
        run: mypy src
"""


# --- parsing ------------------------------------------------------------------
def test_parse_workflow_extracts_run_steps_only():
    steps = parse_workflow(BASE_WORKFLOW)
    assert [s.name for s in steps] == ["Install", "Lint (ruff)", "Test (pytest)"]
    # a multi-line `run:` block becomes one command per line
    assert steps[0].commands == (
        "python -m pip install --upgrade pip",
        'pip install -e ".[dev]"',
    )
    assert "ruff check ." in workflow_commands(BASE_WORKFLOW)


@pytest.mark.parametrize(
    "text",
    [
        "",                                   # empty file (workflow deleted)
        "just a string",                      # not a mapping
        "name: CI\non: push\n",               # no jobs
        "name: CI\njobs:\n  ci: nope\n",      # malformed job
        "name: CI\njobs:\n  ci:\n    steps:\n      - oops\n",  # malformed step
        "name: CI\njobs: [",                  # invalid YAML
    ],
)
def test_parse_workflow_rejects_non_workflows(text):
    with pytest.raises(WorkflowParseError):
        parse_workflow(text)


def test_workflow_paths_filters_to_the_workflow_dir():
    assert workflow_paths(
        [".github/workflows/ci.yml", "src/hsai/ci.py", ".github/dependabot.yml"]
    ) == [".github/workflows/ci.yml"]


# --- parity -------------------------------------------------------------------
def test_parity_holds_between_base_workflow_and_run_local():
    diff = check_parity(BASE_WORKFLOW, ci.local_commands())
    assert diff.ok
    assert diff.mirrored == ("ruff check .", "pytest")
    # setup commands are exempt, not gates
    assert 'pip install -e ".[dev]"' in diff.exempt
    assert "agree" in diff.reason()


def test_parity_flags_a_remote_only_gate():
    diff = check_parity(MYPY_WORKFLOW, ci.local_commands())
    assert not diff.ok
    assert diff.remote_only == ("mypy src",)
    assert "remote-only gate" in diff.reason()


def test_parity_flags_a_dropped_local_gate():
    diff = check_parity(NO_PYTEST_WORKFLOW, ci.local_commands())
    assert not diff.ok
    assert diff.local_only == ("pytest",)
    assert "not declared in the workflow" in diff.reason()


def test_parity_fails_closed_on_an_unreadable_workflow():
    diff = check_parity("name: CI\njobs: [", ci.local_commands())
    assert not diff.ok and diff.error


def test_real_repo_workflow_and_run_local_agree():
    """The shipped ci.yml mirrors `ci.run_local` - the invariant, on this repo."""
    cfg = load_config(str(REPO_ROOT))
    policy = policy_from_config(cfg)
    text = (REPO_ROOT / policy.workflow_path).read_text()
    diff = check_parity(text, ci.local_commands(), policy=policy)
    assert diff.ok, ciguard.render_parity(diff, workflow_path=policy.workflow_path)


def test_render_parity_is_readable():
    report = ciguard.render_parity(check_parity(MYPY_WORKFLOW, ci.local_commands()))
    assert "DIVERGED" in report and "REMOTE ONLY  mypy src" in report
    assert "AGREE" in ciguard.render_parity(check_parity(BASE_WORKFLOW, ci.local_commands()))


# --- policy -------------------------------------------------------------------
def test_policy_comes_from_core_yaml():
    policy = policy_from_config(load_config(str(REPO_ROOT)))
    assert policy.change_label == "ci-change"
    assert policy.workflow_path == WORKFLOW
    assert "ruff check ." in policy.required_steps and "pytest" in policy.required_steps
    assert "SDLC evidence (PR body)" in policy.parity_exempt_steps


def test_policy_falls_back_to_safe_defaults():
    policy = policy_from_config(object())
    assert policy == CIPolicy()


def test_the_change_label_is_part_of_the_standard_label_set():
    """A policy that gates on a label nobody can apply would be a dead channel."""
    from hsai.github import STANDARD_LABELS

    assert ciguard.CI_CHANGE_LABEL in STANDARD_LABELS
    assert policy_from_config(load_config(str(REPO_ROOT))).change_label in STANDARD_LABELS


# --- verdicts -----------------------------------------------------------------
def test_no_workflow_paths_is_a_no_op():
    verdict = classify_workflow_diff(["src/hsai/ci.py"], LABELS, BASE_WORKFLOW, BASE_WORKFLOW)
    assert verdict.allowed and verdict.paths == ()


def test_unchanged_workflow_needs_no_permission():
    verdict = classify_workflow_diff([WORKFLOW], (), BASE_WORKFLOW, BASE_WORKFLOW)
    assert verdict.allowed
    assert "unchanged" in verdict.reason


def test_missing_ticket_fails_closed():
    verdict = classify_workflow_diff([WORKFLOW], None, BASE_WORKFLOW, TIMEOUT_WORKFLOW)
    assert not verdict.allowed
    assert "fail closed" in verdict.reason
    assert verdict.action == "revert"


def test_edit_without_the_ci_change_label_is_reverted():
    verdict = classify_workflow_diff([WORKFLOW], ("priority:P2",), BASE_WORKFLOW, TIMEOUT_WORKFLOW)
    assert not verdict.allowed
    assert "`ci-change` label" in verdict.reason


def test_valid_edit_on_a_ci_change_ticket_is_allowed():
    verdict = classify_workflow_diff([WORKFLOW], LABELS, BASE_WORKFLOW, TIMEOUT_WORKFLOW)
    assert verdict.allowed
    assert verdict.paths == (WORKFLOW,)
    assert verdict.parity is not None and verdict.parity.ok
    rendered = verdict.render()
    assert "`allow`" in rendered and WORKFLOW in rendered and "parity" in rendered


def test_removing_a_required_step_is_rejected():
    verdict = classify_workflow_diff([WORKFLOW], LABELS, BASE_WORKFLOW, NO_PYTEST_WORKFLOW)
    assert not verdict.allowed
    assert "drops required step(s)" in verdict.reason and "`pytest`" in verdict.reason


def test_adding_a_remote_only_gate_is_rejected():
    verdict = classify_workflow_diff([WORKFLOW], LABELS, BASE_WORKFLOW, MYPY_WORKFLOW)
    assert not verdict.allowed
    assert "local/remote CI would diverge" in verdict.reason
    assert "`mypy src`" in verdict.reason
    assert verdict.parity is not None and verdict.parity.remote_only == ("mypy src",)


def test_unparseable_workflow_is_rejected():
    verdict = classify_workflow_diff([WORKFLOW], LABELS, BASE_WORKFLOW, "name: CI\njobs: [")
    assert not verdict.allowed
    assert "fail closed" in verdict.reason


def test_deleting_the_workflow_is_rejected():
    verdict = classify_workflow_diff([WORKFLOW], LABELS, BASE_WORKFLOW, "")
    assert not verdict.allowed


def test_a_second_workflow_lane_is_rejected():
    verdict = classify_workflow_diff(
        [WORKFLOW, ".github/workflows/nightly.yml"], LABELS, BASE_WORKFLOW, TIMEOUT_WORKFLOW
    )
    assert not verdict.allowed
    assert "outside the governed lane" in verdict.reason
    assert "nightly.yml" in verdict.reason


def test_a_locally_mirrored_new_gate_is_allowed():
    """Parity is symmetric: add the gate to `run_local` and the edit passes."""
    verdict = classify_workflow_diff(
        [WORKFLOW], LABELS, BASE_WORKFLOW, MYPY_WORKFLOW,
        local=("ruff check .", "pytest", "mypy src"),
    )
    assert verdict.allowed
    assert verdict.parity is not None and "mypy src" in verdict.parity.mirrored
