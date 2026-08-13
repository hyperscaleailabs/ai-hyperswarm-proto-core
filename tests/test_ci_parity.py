"""Local and remote CI must execute the same declared contract.

The definition of "a green build" lives once, in ``.ai-swarm/core.yaml``
(``ci.steps``). ``.github/workflows/ci.yml`` is only a caller. These tests fail
the build if a declared step stops being reachable from the workflow, or if the
workflow starts redefining green with a bespoke inline lint/test command -
the drift that once left `hsai repro-check` built but never run on GitHub.
"""
from pathlib import Path

import pytest
import yaml

from hsai import ci
from hsai.config import CIStep, load_config

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "ci.yml"


def _workflow_text() -> str:
    return WORKFLOW_PATH.read_text()


# Has the workflow been migrated to call the contract instead of restating it?
# Workers cannot land that migration themselves: the orchestrator reverts every
# uncommitted edit under .github/workflows/**, and the governed `ci-change`
# hatch added alongside these tests only covers *invocation* lines, not the
# structural rewrite. Until a `ci-change` ticket (or a human) lands it, the
# assertions that presuppose it are expected failures - strict, so they turn
# into ordinary green tests the moment the migration is in place, and into a
# red build if it is later undone.
WORKFLOW_DELEGATES = bool(ci.delegated_jobs(ci.workflow_run_lines(_workflow_text())))
needs_migration = pytest.mark.xfail(
    not WORKFLOW_DELEGATES,
    strict=True,
    reason=(
        ".github/workflows/ci.yml still defines the build inline instead of calling "
        "`hsai ci --scope remote`; landing that rewrite needs a `ci-change` ticket "
        "(see CONTRIBUTING.md)"
    ),
)


def _manifest_steps() -> tuple[CIStep, ...]:
    return load_config(REPO_ROOT).ci_steps


def _without_job(workflow_yaml: str, job: str) -> str:
    """The workflow with one job removed - the scratch-branch experiment, in code."""
    data = yaml.safe_load(workflow_yaml)
    data["jobs"].pop(job)
    return yaml.safe_dump(data)


# --- the real repo ---------------------------------------------------------------

def test_manifest_declares_the_contract():
    steps = _manifest_steps()
    assert {s.id for s in steps} >= {"ruff", "pytest", "sdlc-evidence", "repro-check"}
    assert all(s.command for s in steps)


@needs_migration
def test_every_remote_step_is_reachable_from_the_workflow():
    unreachable = ci.unreachable_steps(_manifest_steps(), _workflow_text())
    assert unreachable == [], f"declared but never run in CI: {[s.id for s in unreachable]}"


def test_both_scoped_steps_run_locally_and_remotely():
    steps = _manifest_steps()
    both = [s for s in steps if s.scope == "both"]
    assert {s.id for s in both} >= {"ruff", "pytest"}
    assert ci.unreachable_steps(both, _workflow_text()) == []
    assert set(ci.steps_for(steps, "local")) >= set(both)


@needs_migration
def test_workflow_has_no_bespoke_lint_or_test_commands():
    bespoke = ci.bespoke_run_lines(_workflow_text())
    assert bespoke == [], f"inline commands outside the manifest: {bespoke}"


@needs_migration
def test_workflow_delegates_to_the_repo_cli():
    lines = ci.workflow_run_lines(_workflow_text())
    assert any("hsai ci --scope remote" in line for line in lines)
    assert any(line.startswith("hsai repro-check") for line in lines)


@needs_migration
def test_repro_check_is_a_pull_request_gate():
    data = yaml.safe_load(_workflow_text())
    job = data["jobs"]["repro-check"]
    assert "pull_request" in job["if"]
    assert any(
        str(step.get("run", "")).startswith("hsai repro-check") for step in job["steps"]
    )
    # The guard re-runs the PR's tests against the pre-fix tree, so the base ref
    # and full history have to be on disk.
    assert any(step.get("with", {}).get("fetch-depth") == 0 for step in job["steps"])
    assert any("git fetch" in str(step.get("run", "")) for step in job["steps"])


# --- the detector itself ---------------------------------------------------------

@needs_migration
def test_removing_the_repro_check_job_goes_red():
    # Verification plan: delete the repro-check step from ci.yml -> parity red.
    stripped = _without_job(_workflow_text(), "repro-check")
    unreachable = ci.unreachable_steps(_manifest_steps(), stripped)
    assert [s.id for s in unreachable] == ["repro-check"]
    # ...and restoring it goes green again.
    assert ci.unreachable_steps(_manifest_steps(), _workflow_text()) == []


def test_removing_the_ci_job_orphans_every_both_scoped_step():
    stripped = _without_job(_workflow_text(), "ci")
    unreachable = {s.id for s in ci.unreachable_steps(_manifest_steps(), stripped)}
    assert {"ruff", "pytest", "sdlc-evidence"} <= unreachable


def test_a_both_scoped_step_absent_from_the_workflow_is_unreachable():
    steps = (CIStep(id="typecheck", command=("mypy", "src"), scope="both"),)
    workflow = "jobs:\n  ci:\n    steps:\n      - run: echo hi\n"
    assert [s.id for s in ci.unreachable_steps(steps, workflow)] == ["typecheck"]


def test_delegation_only_covers_the_matching_scope_and_job():
    remote_only = CIStep(id="evidence", command=("hsai", "evidence-check"), scope="remote")
    other_job = CIStep(id="repro", command=("hsai", "repro-check"), scope="remote", job="repro")
    workflow = "jobs:\n  ci:\n    steps:\n      - run: hsai ci --scope local\n"
    assert [s.id for s in ci.unreachable_steps((remote_only, other_job), workflow)] == [
        "evidence", "repro",
    ]

    remote_workflow = "jobs:\n  ci:\n    steps:\n      - run: hsai ci --scope remote\n"
    assert [s.id for s in ci.unreachable_steps((remote_only, other_job), remote_workflow)] == [
        "repro"
    ]


def test_inline_lint_or_test_commands_are_flagged():
    workflow = (
        "jobs:\n"
        "  ci:\n"
        "    steps:\n"
        "      - run: |\n"
        "          pip install -e \".[dev]\"\n"
        "          hsai ci --scope remote\n"
        "      - run: pytest -q\n"
        "      - run: echo body | grep -qi '## Model used'\n"
    )
    flagged = ci.bespoke_run_lines(workflow)
    assert flagged == ["pytest -q", "echo body | grep -qi '## Model used'"]


def test_install_lines_are_not_mistaken_for_verdict_commands():
    workflow = (
        "jobs:\n"
        "  ci:\n"
        "    steps:\n"
        "      - run: |\n"
        "          python -m pip install --upgrade pip\n"
        "          pip install -e \".[dev]\"\n"
    )
    assert ci.bespoke_run_lines(workflow) == []
