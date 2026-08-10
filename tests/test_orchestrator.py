import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from hsai import ledger, orchestrator, trajectory
from hsai.config import load_config
from hsai.models import ModelChoice
from hsai.orchestrator import (
    HEAL,
    IMPLEMENT,
    IMPROVE,
    _format_error_with_context,
    _phase_artifacts,
    build_pr_body,
    decide_path,
    run_once,
)
from hsai.proc import Proc

# The envelope `claude -p --output-format json` returns: the usage object the
# quota ledger needs, plus (when the CLI exposes it) the message stream the
# trajectory store turns into steps.
AGENT_JSON = json.dumps(
    {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "num_turns": 2,
        "result": "Implemented the widget and added a test.",
        "session_id": "5f1c",
        "usage": {"input_tokens": 1500, "output_tokens": 320},
        "messages": [
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "name": "Read",
                     "input": {"path": "src/hsai/widget.py"}}
                ],
            },
            {"role": "user", "content": [{"type": "tool_result", "content": "widget source"}]},
        ],
    }
)


class FakeRunner:
    """Deterministic stand-in for a :class:`hsai.proc.Runner`.

    Never touches the network or spawns a real subprocess - every git/gh/claude
    invocation is matched by command prefix and answered with a canned `Proc`.
    `ci_sequence` gives the ruff+pytest outcome for the Nth `ci.run_local()`
    call (index 0 = ci_before, index 1 = ci_after, ...).
    """

    def __init__(
        self,
        *,
        repo_root: str,
        ci_sequence: list[bool],
        open_issues: list[dict] | None = None,
        remote_ci: str = "SUCCESS",
        worktree_status: str = "",
        repro_fix_ok: bool = True,
        repro_parent_ok: bool = False,
        agent_output: str = AGENT_JSON,
    ) -> None:
        self.repo_root = repo_root
        self.agent_output = agent_output
        self.ci_sequence = ci_sequence
        self.open_issues = open_issues or []
        self.remote_ci = remote_ci
        self.worktree_status = worktree_status
        # Controls the targeted `pytest <files>` runs the repro guard makes:
        # `repro_fix_ok` is the outcome on the fix branch, `repro_parent_ok`
        # the outcome on the detached pre-fix (parent) worktree.
        self.repro_fix_ok = repro_fix_ok
        self.repro_parent_ok = repro_parent_ok
        self.calls: list[list[str]] = []
        self._ci_round = 0
        self._issue_seq = 100
        self._pr_seq = 200

    def __call__(
        self, cmd, *, cwd=None, env=None, timeout=None, input_text=None
    ) -> Proc:
        cmd = list(cmd)
        self.calls.append(cmd)
        return self._dispatch(cmd, cwd)

    def _dispatch(self, cmd: list[str], cwd: str | None = None) -> Proc:
        if cmd[:3] == ["gh", "api", "user"]:
            return Proc(cmd, 0, "hsai-bot\n", "")
        if cmd[:2] in (["git", "checkout"], ["git", "pull"], ["git", "fetch"]):
            return Proc(cmd, 0, "", "")
        if cmd[:3] == ["git", "rev-parse", "--show-toplevel"]:
            return Proc(cmd, 0, f"{self.repo_root}\n", "")
        if cmd[:2] == ["git", "merge-base"]:
            return Proc(cmd, 0, "parentsha\n", "")
        if cmd[:3] in (["git", "worktree", "add"], ["git", "worktree", "remove"]):
            return Proc(cmd, 0, "", "")
        if cmd[:2] == ["git", "status"]:
            return Proc(cmd, 0, self.worktree_status, "")
        if cmd[:2] in (["git", "add"], ["git", "commit"], ["git", "push"]):
            return Proc(cmd, 0, "", "")
        if cmd[:2] in (["git", "checkout"], ["git", "clean"]):
            return Proc(cmd, 0, "", "")
        if cmd[:2] == ["ruff", "check"]:
            ok = self.ci_sequence[self._ci_round]
            return Proc(cmd, 0 if ok else 1, "", "" if ok else "ruff: fake lint failure\n")
        if cmd == ["pytest"]:
            ok = self.ci_sequence[self._ci_round]
            self._ci_round += 1
            return Proc(cmd, 0 if ok else 1, "", "" if ok else "pytest: fake test failure\n")
        if cmd[:1] == ["pytest"]:
            # Targeted repro-guard run: distinguish fix-branch from the
            # detached pre-fix (parent) worktree by its cwd.
            is_parent = bool(cwd) and "repro-check-" in cwd
            ok = self.repro_parent_ok if is_parent else self.repro_fix_ok
            return Proc(cmd, 0 if ok else 1, "", "" if ok else "pytest: fake failure\n")
        if cmd[:3] == ["gh", "issue", "list"]:
            return Proc(cmd, 0, json.dumps(self.open_issues), "")
        if cmd[:3] == ["gh", "issue", "create"]:
            self._issue_seq += 1
            return Proc(cmd, 0, f"https://github.com/o/r/issues/{self._issue_seq}\n", "")
        if cmd[:3] == ["gh", "issue", "edit"]:
            return Proc(cmd, 0, "", "")
        if cmd[:1] == ["claude"]:
            return Proc(cmd, 0, self.agent_output, "")
        if cmd[:3] == ["gh", "pr", "create"]:
            self._pr_seq += 1
            return Proc(cmd, 0, f"https://github.com/o/r/pull/{self._pr_seq}\n", "")
        if cmd[:3] == ["gh", "pr", "merge"]:
            return Proc(cmd, 0, "", "")
        if cmd[:3] == ["gh", "pr", "view"]:
            concl = "SUCCESS" if self.remote_ci == "SUCCESS" else "FAILURE"
            rollup = {
                "statusCheckRollup": [
                    {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": concl}
                ]
            }
            return Proc(cmd, 0, json.dumps(rollup), "")
        if cmd[:3] == ["gh", "pr", "close"]:
            return Proc(cmd, 0, "", "")
        if cmd[:3] == ["gh", "pr", "comment"]:
            return Proc(cmd, 0, "", "")
        if cmd[:3] == ["gh", "issue", "view"]:
            num = int(cmd[3])
            match = next((i for i in self.open_issues if i.get("number") == num), None)
            data = match or {
                "number": num, "title": "", "labels": [], "assignees": [], "body": ""
            }
            return Proc(cmd, 0, json.dumps(data), "")
        raise AssertionError(f"FakeRunner: unhandled command {cmd!r}")


def test_decide_path():
    assert decide_path(ci_green=False, has_tickets=True) == HEAL
    assert decide_path(ci_green=False, has_tickets=False) == HEAL
    assert decide_path(ci_green=True, has_tickets=True) == IMPLEMENT
    assert decide_path(ci_green=True, has_tickets=False) == IMPROVE


def test_format_error_with_context():
    error = "connection timeout after 30s"
    result = _format_error_with_context(error, "implement", 42)
    assert result == "[phase=implement, ticket=#42] connection timeout after 30s"

    # without ticket
    result = _format_error_with_context(error, "heal", None)
    assert result == "[phase=heal] connection timeout after 30s"


def test_phase_artifacts_heal():
    artifacts = _phase_artifacts(HEAL)
    assert "Root cause" in artifacts
    assert "Regression test" in artifacts
    assert "CI returned to green" in artifacts


def test_phase_artifacts_implement():
    artifacts = _phase_artifacts(IMPLEMENT)
    assert "Feature/fix implemented" in artifacts
    assert "Tests added" in artifacts
    assert "Linting and tests passing" in artifacts


def test_phase_artifacts_improve():
    artifacts = _phase_artifacts(IMPROVE)
    assert "practice extracted" in artifacts
    assert "reference-set" in artifacts
    assert "Lesson recorded" in artifacts


def test_build_pr_body_requires_ticket():
    choice = ModelChoice(tier="standard", model="sonnet", rationale="x")
    with pytest.raises(ValueError):
        build_pr_body(
            ticket=0, choice=choice, lesson_note="n",
            lesson_summary="s", ci_summary="CI green",
        )


def test_build_pr_body_contains_traceability():
    choice = ModelChoice(tier="heavy", model="opus", rationale="score=4 -> heavy")
    body = build_pr_body(
        ticket=42, choice=choice, lesson_note="2026-07-25-do-thing",
        lesson_summary="kept it small", ci_summary="CI green (ruff=pass, pytest=pass)",
        references=("openai/swarm", "SWE-agent/SWE-agent"),
    )
    assert "Closes #42" in body          # ticket linkage
    assert "`opus`" in body              # model recorded
    assert "kept it small" in body       # lesson present
    assert "[[2026-07-25-do-thing]]" in body
    assert "openai/swarm" in body


def test_build_pr_body_includes_phase_artifacts():
    choice = ModelChoice(tier="standard", model="sonnet", rationale="x")
    body = build_pr_body(
        ticket=10, choice=choice, lesson_note="2026-07-26-test",
        lesson_summary="test", ci_summary="green",
        kind=HEAL,
    )
    assert "## Phase artifacts" in body
    assert "Root cause" in body
    assert "Regression test" in body

    # Test with IMPLEMENT phase
    body = build_pr_body(
        ticket=11, choice=choice, lesson_note="2026-07-26-test",
        lesson_summary="test", ci_summary="green",
        kind=IMPLEMENT,
    )
    assert "## Phase artifacts" in body
    assert "Feature/fix implemented" in body
    assert "Tests added" in body

    # Without kind, artifacts should not appear
    body = build_pr_body(
        ticket=12, choice=choice, lesson_note="2026-07-26-test",
        lesson_summary="test", ci_summary="green",
    )
    assert "## Phase artifacts" not in body


def test_run_once_dry_run_is_side_effect_free(tmp_path):
    cfg = load_config()
    result = run_once(cfg, repo_dir=str(tmp_path), dry_run=True, iteration=1)
    assert result.kind == IMPROVE          # no CI run, no tickets -> self-improve
    assert result.ci_before is True
    assert result.lesson_path                # a lesson was still recorded
    assert (tmp_path / "knowledge" / "lessons").exists()
    # dry-run never opens a PR
    assert result.pr is None
    assert result.merged is False


class _FixedUUID:
    hex = "abc123abc123abc123abc123abc123a"


def _pin_worktree_path(monkeypatch, tmp_path, cfg, branch_iteration: int = 1) -> Path:
    """Make the worktree path `run_once` will create deterministic, so a test
    can pre-seed a regression-test file into it before the guard runs."""
    monkeypatch.setattr("hsai.orchestrator.uuid4", lambda: _FixedUUID())
    monkeypatch.setattr(orchestrator, "time", SimpleNamespace(time=lambda: 1700000000))
    branch = f"hsai/iter-1700000000-{branch_iteration}-{_FixedUUID.hex[:6]}"
    return tmp_path / cfg.worktrees_dir / branch


def test_run_once_heal_path_with_fake_runner(tmp_path, monkeypatch):
    cfg = load_config()
    wt = _pin_worktree_path(monkeypatch, tmp_path, cfg)
    (wt / "tests").mkdir(parents=True)
    (wt / "tests" / "test_regression.py").write_text(
        "def test_regression():\n    assert True\n"
    )
    runner = FakeRunner(
        repo_root=str(tmp_path), ci_sequence=[False, True],
        worktree_status="?? tests/test_regression.py\n",
        repro_fix_ok=True, repro_parent_ok=False,
    )

    result = run_once(
        cfg, repo_dir=str(tmp_path), dry_run=False,
        runner=runner, ai_runner=runner, iteration=1,
    )

    # red CI, no tickets fetched yet -> heal, matching decide_path directly
    assert decide_path(ci_green=False, has_tickets=False) == HEAL
    assert result.kind == HEAL
    assert result.ci_before is False
    assert result.ci_after is True
    assert result.ticket and result.ticket > 0
    assert result.pr is not None
    assert result.merged is True

    # the heal ticket added a regression test that reproduces the bug: the
    # repro guard approved and recorded the transition
    assert any(n.startswith("repro guard: reproduced") for n in result.notes)
    assert result.recovered is False

    # a lesson is always written
    assert result.lesson_path
    assert Path(result.lesson_path).exists()
    lesson_text = Path(result.lesson_path).read_text()
    assert "tests/test_regression.py" in lesson_text
    assert "reproduced" in lesson_text

    # heal files its own ticket, never picks up an existing one
    assert any(c[:3] == ["gh", "issue", "create"] for c in runner.calls)
    assert not any(c[:3] == ["gh", "issue", "edit"] for c in runner.calls)

    # PR body is linked to the ticket and records model + lesson
    pr_create = next(c for c in runner.calls if c[:3] == ["gh", "pr", "create"])
    body = pr_create[pr_create.index("--body") + 1]
    assert f"Closes #{result.ticket}" in body
    assert result.model in body
    assert "Lesson learned" in body

    # exactly one headless claude invocation, no real subprocess ever ran
    claude_calls = [c for c in runner.calls if c[:1] == ["claude"]]
    assert len(claude_calls) == 1
    assert all(c[0] in {"git", "gh", "ruff", "pytest", "claude"} for c in runner.calls)


def test_repro_guard_blocks_heal_without_regression_test(tmp_path):
    cfg = load_config()
    runner = FakeRunner(repo_root=str(tmp_path), ci_sequence=[False, True])

    result = run_once(
        cfg, repo_dir=str(tmp_path), dry_run=False,
        runner=runner, ai_runner=runner, iteration=1,
    )

    # heal ticket added no test at all -> the guard blocks before a PR opens
    assert result.kind == HEAL
    assert result.recovered is True
    assert result.pr is None
    assert result.merged is False
    assert any("repro guard" in n and "no test file" in n for n in result.notes)
    assert not any(c[:3] == ["gh", "pr", "create"] for c in runner.calls)


def test_repro_guard_blocks_when_new_test_also_passes_on_pre_fix_tree(tmp_path, monkeypatch):
    cfg = load_config()
    wt = _pin_worktree_path(monkeypatch, tmp_path, cfg)
    (wt / "tests").mkdir(parents=True)
    (wt / "tests" / "test_regression.py").write_text(
        "def test_regression():\n    assert True\n"
    )
    runner = FakeRunner(
        repo_root=str(tmp_path), ci_sequence=[False, True],
        worktree_status="?? tests/test_regression.py\n",
        # The "new" test passes even on the pre-fix tree -> no real bug proven.
        repro_fix_ok=True, repro_parent_ok=True,
    )

    result = run_once(
        cfg, repo_dir=str(tmp_path), dry_run=False,
        runner=runner, ai_runner=runner, iteration=1,
    )

    assert result.kind == HEAL
    assert result.recovered is True
    assert result.pr is None
    assert result.merged is False
    assert any("does not reproduce a real bug" in n for n in result.notes)
    assert not any(c[:3] == ["gh", "pr", "create"] for c in runner.calls)


WELL_FORMED_BODY = """## Problem
Widget missing.

## Proposal
Build the widget.

## Acceptance criteria
- [ ] widget builds
- [ ] widget tested

## Verification plan
- [ ] pytest green
"""


def test_run_once_implement_path_with_fake_runner(tmp_path):
    cfg = load_config()
    open_issues = [
        {
            "number": 7,
            "title": "add widget",
            "labels": [{"name": "priority:P2"}],
            "assignees": [],
            "body": WELL_FORMED_BODY,
        }
    ]
    runner = FakeRunner(
        repo_root=str(tmp_path), ci_sequence=[True, True], open_issues=open_issues
    )

    result = run_once(
        cfg, repo_dir=str(tmp_path), dry_run=False,
        runner=runner, ai_runner=runner, iteration=1,
    )

    # green CI, open ticket available -> implement
    assert decide_path(ci_green=True, has_tickets=True) == IMPLEMENT
    assert result.kind == IMPLEMENT
    assert result.ticket == 7
    assert result.ci_before is True
    assert result.ci_after is True
    assert result.pr is not None
    assert result.merged is True

    # a lesson is always written
    assert result.lesson_path
    assert Path(result.lesson_path).exists()

    # implement assigns the existing ticket, never files a new one
    assert any(c[:3] == ["gh", "issue", "edit"] for c in runner.calls)
    assert not any(c[:3] == ["gh", "issue", "create"] for c in runner.calls)

    # PR body is linked to the ticket and records model + lesson
    pr_create = next(c for c in runner.calls if c[:3] == ["gh", "pr", "create"])
    body = pr_create[pr_create.index("--body") + 1]
    assert "Closes #7" in body
    assert result.model in body
    assert "Lesson learned" in body

    claude_calls = [c for c in runner.calls if c[:1] == ["claude"]]
    assert len(claude_calls) == 1
    assert all(c[0] in {"git", "gh", "ruff", "pytest", "claude"} for c in runner.calls)


def test_run_once_records_remote_ci_in_lesson_before_merging(tmp_path):
    cfg = load_config()
    open_issues = [
        {
            "number": 7,
            "title": "add widget",
            "labels": [{"name": "priority:P2"}],
            "assignees": [],
            "body": "Implement the widget end to end.",
        }
    ]
    runner = FakeRunner(
        repo_root=str(tmp_path), ci_sequence=[True, True], open_issues=open_issues,
        remote_ci="SUCCESS",
    )

    result = run_once(
        cfg, repo_dir=str(tmp_path), dry_run=False,
        runner=runner, ai_runner=runner, iteration=1,
    )

    assert result.remote == "SUCCESS"
    assert result.merged is True

    # the lesson written to disk carries the true remote CI conclusion (#14)
    lesson_text = Path(result.lesson_path).read_text()
    assert "| remote CI | SUCCESS |" in lesson_text

    # the explicit poll (gh pr view) is the pre-merge gate: it happens before
    # auto-merge is armed (gh pr merge), not the other way around
    view_idx = next(i for i, c in enumerate(runner.calls) if c[:3] == ["gh", "pr", "view"])
    merge_idx = next(i for i, c in enumerate(runner.calls) if c[:3] == ["gh", "pr", "merge"])
    assert view_idx < merge_idx

    # the lesson update is pushed to the branch before the merge is armed
    commit_msgs = [
        c[c.index("-m") + 1] for c in runner.calls if c[:2] == ["git", "commit"]
    ]
    assert any("record remote CI outcome" in m for m in commit_msgs)


def test_run_once_recovers_when_remote_ci_fails(tmp_path):
    cfg = load_config()
    open_issues = [
        {
            "number": 7,
            "title": "add widget",
            "labels": [{"name": "priority:P2"}],
            "assignees": [],
            "body": WELL_FORMED_BODY,
        }
    ]
    runner = FakeRunner(
        repo_root=str(tmp_path), ci_sequence=[True, True],
        open_issues=open_issues, remote_ci="FAILURE",
    )

    result = run_once(
        cfg, repo_dir=str(tmp_path), dry_run=False,
        runner=runner, ai_runner=runner, iteration=1,
    )

    assert result.kind == IMPLEMENT
    assert result.remote == "FAILURE"
    assert result.merged is False
    assert result.recovered is True
    # PR closed and the ticket returned to the backlog (#11)
    assert any(c[:3] == ["gh", "pr", "close"] for c in runner.calls)
    assert any(
        c[:3] == ["gh", "issue", "edit"] and "--remove-assignee" in c for c in runner.calls
    )
    # retry counter bumped (attempts:1, below max_ticket_attempts=2)
    assert any("attempts:1" in c for c in runner.calls)


def test_run_once_requeues_on_remote_ci_timeout(tmp_path, monkeypatch):
    """A TIMEOUT is infrastructure latency, not a verdict: the PR stays open,
    the branch stays intact, and the ticket's attempts label is untouched."""
    cfg = load_config()
    open_issues = [
        {
            "number": 7,
            "title": "add widget",
            "labels": [{"name": "priority:P2"}],
            "assignees": [],
            "body": WELL_FORMED_BODY,
        }
    ]
    runner = FakeRunner(
        repo_root=str(tmp_path), ci_sequence=[True, True], open_issues=open_issues,
    )
    monkeypatch.setattr(orchestrator.ci, "wait_remote", lambda *a, **k: orchestrator.ci.TIMEOUT)

    result = run_once(
        cfg, repo_dir=str(tmp_path), dry_run=False,
        runner=runner, ai_runner=runner, iteration=1,
    )

    assert result.remote == "TIMEOUT"
    assert result.merged is False
    assert result.recovered is False
    assert result.requeued is True
    assert any("requeued" in n for n in result.notes)

    # the PR is never closed and its branch is never deleted
    assert not any(c[:3] == ["gh", "pr", "close"] for c in runner.calls)
    # the ticket is released back to the backlog...
    assert any(
        c[:3] == ["gh", "issue", "edit"] and "--remove-assignee" in c for c in runner.calls
    )
    # ...but WITHOUT consuming an attempt: no attempts:N label is ever added
    assert not any(
        c[:3] == ["gh", "issue", "edit"] and any(a.startswith("attempts:") for a in c)
        for c in runner.calls
    )
    assert not any(
        c[:3] == ["gh", "issue", "edit"] and "blocked" in c for c in runner.calls
    )
    # the ledger records the distinct `timeout` outcome, not `recovered`
    records = ledger.read_records(ledger.ledger_path(cfg, tmp_path))
    assert [r.outcome for r in records] == ["timeout"]


def test_run_once_requeues_on_bare_pending(tmp_path, monkeypatch):
    """A raw PENDING (defensive: `wait_remote` should not normally return it)
    is routed exactly like TIMEOUT - requeued, never treated as a FAILURE."""
    cfg = load_config()
    open_issues = [dict(WIDGET_ISSUE)]
    runner = FakeRunner(
        repo_root=str(tmp_path), ci_sequence=[True, True], open_issues=open_issues,
    )
    monkeypatch.setattr(orchestrator.ci, "wait_remote", lambda *a, **k: orchestrator.ci.PENDING)

    result = run_once(
        cfg, repo_dir=str(tmp_path), dry_run=False,
        runner=runner, ai_runner=runner, iteration=1,
    )

    assert result.requeued is True
    assert result.merged is False and result.recovered is False
    assert not any(c[:3] == ["gh", "pr", "close"] for c in runner.calls)


@pytest.mark.parametrize(
    "remote,expect_merge",
    [("SUCCESS", True), ("FAILURE", False), ("PENDING", False), ("TIMEOUT", False)],
)
def test_no_code_path_merges_a_pr_that_is_not_success(tmp_path, monkeypatch, remote, expect_merge):
    """Drives SUCCESS/FAILURE/PENDING/TIMEOUT through `run_once`: `gh pr merge`
    is invoked if and only if remote CI concluded SUCCESS."""
    cfg = load_config()
    open_issues = [dict(WIDGET_ISSUE)]
    runner = FakeRunner(
        repo_root=str(tmp_path), ci_sequence=[True, True], open_issues=open_issues,
    )
    monkeypatch.setattr(orchestrator.ci, "wait_remote", lambda *a, **k: remote)

    result = run_once(
        cfg, repo_dir=str(tmp_path), dry_run=False,
        runner=runner, ai_runner=runner, iteration=1,
    )

    assert result.merged is expect_merge
    assert any(c[:3] == ["gh", "pr", "merge"] for c in runner.calls) is expect_merge


def test_worktree_is_removed_even_when_the_agent_call_raises(tmp_path):
    """The `finally` in `run_once` runs on every exit path, including a raised
    exception from mid-iteration (here: the `claude` invocation itself)."""
    cfg = load_config()
    open_issues = [dict(WIDGET_ISSUE)]

    class RaisingRunner(FakeRunner):
        def _dispatch(self, cmd, cwd=None):
            if cmd[:1] == ["claude"]:
                raise RuntimeError("boom: simulated agent crash mid-iteration")
            return super()._dispatch(cmd, cwd)

    runner = RaisingRunner(
        repo_root=str(tmp_path), ci_sequence=[True, True], open_issues=open_issues,
    )

    with pytest.raises(RuntimeError, match="boom"):
        run_once(
            cfg, repo_dir=str(tmp_path), dry_run=False,
            runner=runner, ai_runner=runner, iteration=1,
        )

    assert any(c[:3] == ["git", "worktree", "remove"] for c in runner.calls)


def test_worktree_is_removed_when_ci_check_raises(tmp_path):
    """Same guarantee, exercised from an earlier step (the CI recheck)."""
    cfg = load_config()
    open_issues = [dict(WIDGET_ISSUE)]

    class RaisingRunner(FakeRunner):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self._pytest_calls = 0

        def _dispatch(self, cmd, cwd=None):
            if cmd == ["pytest"]:
                self._pytest_calls += 1
                if self._pytest_calls == 2:  # the post-agent re-check (step 6)
                    raise RuntimeError("boom: simulated CI runner crash")
            return super()._dispatch(cmd, cwd)

    runner = RaisingRunner(
        repo_root=str(tmp_path), ci_sequence=[True, True], open_issues=open_issues,
    )

    with pytest.raises(RuntimeError, match="boom"):
        run_once(
            cfg, repo_dir=str(tmp_path), dry_run=False,
            runner=runner, ai_runner=runner, iteration=1,
        )

    assert any(c[:3] == ["git", "worktree", "remove"] for c in runner.calls)


def test_dry_run_never_removes_the_repo_itself(tmp_path):
    """`wt is repo_dir` in dry-run, so the `finally` must never call
    `remove_worktree` there - that would delete the caller's own repo."""
    cfg = load_config()
    result = run_once(cfg, repo_dir=str(tmp_path), dry_run=True, iteration=1)
    assert result.pr is None
    assert (tmp_path / "knowledge" / "lessons").exists()  # the repo is intact


def test_workflow_edits_are_reverted(tmp_path):
    cfg = load_config()
    open_issues = [
        {
            "number": 7,
            "title": "add widget",
            "labels": [{"name": "priority:P2"}],
            "assignees": [],
            "body": WELL_FORMED_BODY,
        }
    ]
    runner = FakeRunner(
        repo_root=str(tmp_path), ci_sequence=[True, True], open_issues=open_issues,
        worktree_status=" M .github/workflows/ci.yml\n?? src/hsai/new.py\n",
    )

    result = run_once(
        cfg, repo_dir=str(tmp_path), dry_run=False,
        runner=runner, ai_runner=runner, iteration=1,
    )

    # #12: workflow edits are restored so local CI can't diverge from remote
    assert any(c[:3] == ["git", "checkout", "HEAD"] for c in runner.calls)
    assert any(c[:2] == ["git", "clean"] for c in runner.calls)
    assert any("reverted workflow edits" in n for n in result.notes)


def test_completeness_guard_blocks_knowledge_only_diff_on_code_ticket(tmp_path):
    cfg = load_config()
    open_issues = [
        {
            "number": 9,
            "title": "feat: add widget",
            "labels": [{"name": "priority:P2"}],
            "assignees": [],
            "body": WELL_FORMED_BODY,
        }
    ]
    runner = FakeRunner(
        repo_root=str(tmp_path), ci_sequence=[True, True], open_issues=open_issues,
        worktree_status="?? knowledge/lessons/2026-07-26-fake.md\n",
    )

    result = run_once(
        cfg, repo_dir=str(tmp_path), dry_run=False,
        runner=runner, ai_runner=runner, iteration=1,
    )

    # knowledge-only diff on a feat: ticket -> recovered, never a PR
    assert result.recovered is True
    assert result.pr is None
    assert result.merged is False
    assert any("completeness guard" in n for n in result.notes)
    assert not any(c[:3] == ["gh", "pr", "create"] for c in runner.calls)
    # ticket returned to backlog with an attempt recorded
    assert any(
        c[:3] == ["gh", "issue", "edit"] and "--remove-assignee" in c for c in runner.calls
    )


# --- trajectory store (one durable record per agent run) --------------------

WIDGET_ISSUE = {
    "number": 7,
    "title": "add widget",
    "labels": [{"name": "priority:P2"}],
    "assignees": [],
    "body": WELL_FORMED_BODY,
}


def _trajectory_files(root: Path) -> list[Path]:
    return sorted((root / ".hsai" / "traj").glob("*/*.json"))


def test_agent_run_is_persisted_as_a_trajectory(tmp_path):
    cfg = load_config()
    runner = FakeRunner(
        repo_root=str(tmp_path), ci_sequence=[True, True], open_issues=[dict(WIDGET_ISSUE)]
    )

    result = run_once(
        cfg, repo_dir=str(tmp_path), dry_run=False,
        runner=runner, ai_runner=runner, iteration=4,
    )

    assert result.merged is True
    # Exactly one trajectory file for the one `claude` invocation, sharded by block.
    assert len([c for c in runner.calls if c[:1] == ["claude"]]) == 1
    assert [p.name for p in _trajectory_files(tmp_path)] == ["4.json"]
    assert _trajectory_files(tmp_path)[0].parent.name == "0"   # block 4 // 100

    traj = trajectory.load(tmp_path, "4")
    assert traj.iteration == 4 and traj.ticket == 7 and traj.kind == IMPLEMENT
    assert traj.block == 0
    assert traj.model == result.model and traj.tier
    assert "add widget" in traj.prompt              # the prompt is reconstructable
    assert traj.prompt_digest                       # and fingerprinted
    assert traj.session_id == "5f1c"                # per-run id, when exposed
    assert [s.kind for s in traj.steps] == ["tool_use", "tool_result", "result"]
    assert traj.usage == {"input_tokens": 1500, "output_tokens": 320}
    assert traj.exit_status == "ok" and traj.ok is True
    assert traj.outcome == "merged"                 # final outcome is folded back in
    assert any(n == "trajectory=4" for n in result.notes)


def test_trajectories_are_sharded_by_block(tmp_path):
    cfg = load_config()
    runner = FakeRunner(
        repo_root=str(tmp_path), ci_sequence=[True, True], open_issues=[dict(WIDGET_ISSUE)]
    )

    # `hsai cycle` numbers a block's runs block*100 + n.
    run_once(
        cfg, repo_dir=str(tmp_path), dry_run=False,
        runner=runner, ai_runner=runner, iteration=703,
    )

    assert (tmp_path / ".hsai" / "traj" / "7" / "703.json").is_file()
    assert trajectory.load(tmp_path, "703").block == 7


def test_trajectory_is_written_when_the_completeness_guard_aborts(tmp_path):
    cfg = load_config()
    issue = dict(WIDGET_ISSUE, number=9, title="feat: add widget")
    runner = FakeRunner(
        repo_root=str(tmp_path), ci_sequence=[True, True], open_issues=[issue],
        worktree_status="?? knowledge/lessons/2026-07-26-fake.md\n",
    )

    result = run_once(
        cfg, repo_dir=str(tmp_path), dry_run=False,
        runner=runner, ai_runner=runner, iteration=5,
    )

    # The run the guard aborted is exactly the one worth replaying.
    assert result.recovered is True and result.pr is None
    assert [p.name for p in _trajectory_files(tmp_path)] == ["5.json"]
    traj = trajectory.load(tmp_path, "5")
    assert traj.outcome == "incomplete"
    assert traj.steps
    # The ledger record for the aborted run carries its token cost too: an
    # abort still spent quota.
    records = ledger.read_records(ledger.ledger_path(cfg, tmp_path))
    assert [r.outcome for r in records] == ["incomplete"]
    assert records[0].input_tokens == 1500 and records[0].output_tokens == 320


def test_trajectory_is_written_when_the_repro_guard_aborts(tmp_path):
    cfg = load_config()
    runner = FakeRunner(repo_root=str(tmp_path), ci_sequence=[False, True])

    result = run_once(
        cfg, repo_dir=str(tmp_path), dry_run=False,
        runner=runner, ai_runner=runner, iteration=6,
    )

    assert result.kind == HEAL and result.recovered is True and result.pr is None
    assert [p.name for p in _trajectory_files(tmp_path)] == ["6.json"]
    traj = trajectory.load(tmp_path, "6")
    assert traj.outcome == "no_repro"
    assert traj.kind == HEAL and "auto-heal" in traj.prompt
    assert traj.ticket == result.ticket

    records = ledger.read_records(ledger.ledger_path(cfg, tmp_path))
    assert [r.outcome for r in records] == ["no_repro"]
    assert records[0].input_tokens == 1500


def test_one_trajectory_file_per_agent_invocation(tmp_path):
    cfg = load_config()
    runner = FakeRunner(
        repo_root=str(tmp_path), ci_sequence=[True, True, True, True],
        open_issues=[dict(WIDGET_ISSUE)],
    )

    for i in (1, 2):
        run_once(
            cfg, repo_dir=str(tmp_path), dry_run=False,
            runner=runner, ai_runner=runner, iteration=i,
        )

    claude_calls = [c for c in runner.calls if c[:1] == ["claude"]]
    assert len(claude_calls) == len(_trajectory_files(tmp_path)) == 2
    assert [p.name for p in _trajectory_files(tmp_path)] == ["1.json", "2.json"]
    # Every invocation asked the CLI for the structured envelope.
    assert all("--output-format" in c and "json" in c for c in claude_calls)


def test_dry_run_writes_no_trajectory(tmp_path):
    cfg = load_config()
    # A dry run makes no model call, so it must leave no artifact behind.
    run_once(cfg, repo_dir=str(tmp_path), dry_run=True, iteration=1)
    assert not (tmp_path / ".hsai" / "traj").exists()
    assert _trajectory_files(tmp_path) == []


def test_token_counts_reach_the_ledger_and_the_block_aggregate(tmp_path):
    cfg = load_config()
    runner = FakeRunner(
        repo_root=str(tmp_path), ci_sequence=[True, True], open_issues=[dict(WIDGET_ISSUE)]
    )

    run_once(
        cfg, repo_dir=str(tmp_path), dry_run=False,
        runner=runner, ai_runner=runner, iteration=3,
    )

    records = ledger.read_records(ledger.ledger_path(cfg, tmp_path))
    assert len(records) == 1
    assert records[0].input_tokens == 1500 and records[0].output_tokens == 320

    agg = ledger.aggregate_block(records, block=0)
    assert agg.input_tokens == 1500 and agg.output_tokens == 320
    assert "1820 tokens" in agg.summary()      # no longer reporting zero
    # Tokens per merged PR is the block's efficiency signal (G4).
    assert agg.merged_iterations == 1
    assert agg.tokens_per_merged_pr() == 1820
    assert "1820 tokens/merged PR" in agg.summary()


def test_trajectory_digest_reaches_the_lesson_and_the_pr_body(tmp_path):
    """The audit trail is visible on the PR, not only on local disk."""
    cfg = load_config()
    runner = FakeRunner(
        repo_root=str(tmp_path), ci_sequence=[True, True], open_issues=[dict(WIDGET_ISSUE)]
    )

    result = run_once(
        cfg, repo_dir=str(tmp_path), dry_run=False,
        runner=runner, ai_runner=runner, iteration=14,
    )

    pr_create = next(c for c in runner.calls if c[:3] == ["gh", "pr", "create"])
    pr_body = pr_create[pr_create.index("--body") + 1]
    lesson_text = Path(result.lesson_path).read_text()

    for text in (pr_body, lesson_text):
        assert "tokens=1500in/320out" in text     # what the run cost
        assert "duration=" in text                # how long it took
        assert "exit=ok" in text                  # how it ended
        assert "hsai traj 14" in text             # where the full record is
    assert "## Trajectory" in pr_body


def test_non_json_agent_output_still_produces_an_iteration_and_trajectory(tmp_path):
    """An older `claude` binary prints plain text: degrade, never break."""
    cfg = load_config()
    runner = FakeRunner(
        repo_root=str(tmp_path), ci_sequence=[True, True], open_issues=[dict(WIDGET_ISSUE)],
        agent_output="widget added, tests green\n",
    )

    result = run_once(
        cfg, repo_dir=str(tmp_path), dry_run=False,
        runner=runner, ai_runner=runner, iteration=8,
    )

    assert result.merged is True and result.pr is not None
    assert Path(result.lesson_path).exists()

    traj = trajectory.load(tmp_path, "8")
    assert traj.usage is None                       # nothing to report, not a crash
    assert [s.kind for s in traj.steps] == ["output"]
    assert traj.steps[0].text == "widget added, tests green"

    # The ledger record still exists; its token columns are simply null.
    records = ledger.read_records(ledger.ledger_path(cfg, tmp_path))
    assert records[0].input_tokens is None and records[0].output_tokens is None


LOUD_AGENT_JSON = json.dumps(
    {
        "result": "TAIL-MARKER: finished the widget.",
        "usage": {"input_tokens": 10, "output_tokens": 2},
        "messages": [
            {"role": "assistant", "content": f"EARLY-MARKER-{i}: internal repo detail"}
            for i in range(8)
        ],
    }
)


def test_only_a_redacted_excerpt_of_the_trajectory_reaches_the_knowledge_base(tmp_path):
    cfg = load_config()
    runner = FakeRunner(
        repo_root=str(tmp_path), ci_sequence=[True, True], open_issues=[dict(WIDGET_ISSUE)],
        agent_output=LOUD_AGENT_JSON,
    )

    result = run_once(
        cfg, repo_dir=str(tmp_path), dry_run=False,
        runner=runner, ai_runner=runner, iteration=2,
    )

    lesson_text = Path(result.lesson_path).read_text()
    traj = trajectory.load(tmp_path, "2")
    assert len(traj.steps) == 9

    # The lesson quotes the tail (and points at the post-mortem command)...
    assert "TAIL-MARKER" in lesson_text
    assert "hsai traj 2" in lesson_text
    assert "earlier step(s) elided" in lesson_text
    # ...but never the whole run, nor the prompt, nor the raw payload.
    assert "EARLY-MARKER-0" not in lesson_text
    assert traj.prompt not in lesson_text
    assert traj.to_json() not in lesson_text

    # Nothing anywhere under knowledge/ carries the full payload.
    knowledge = Path(result.lesson_path).parents[1]
    for note in knowledge.rglob("*.md"):
        text = note.read_text()
        assert "EARLY-MARKER-0" not in text
        assert traj.to_json() not in text


def test_agent_secrets_never_reach_the_knowledge_base(tmp_path):
    cfg = load_config()
    payload = json.dumps({"result": "used ANTHROPIC_API_KEY=sk-ant-abcdef0123456789 to run"})
    runner = FakeRunner(
        repo_root=str(tmp_path), ci_sequence=[True, True], open_issues=[dict(WIDGET_ISSUE)],
        agent_output=payload,
    )

    result = run_once(
        cfg, repo_dir=str(tmp_path), dry_run=False,
        runner=runner, ai_runner=runner, iteration=9,
    )

    assert "sk-ant-abcdef0123456789" not in Path(result.lesson_path).read_text()
    assert "sk-ant-abcdef0123456789" not in trajectory.load(tmp_path, "9").steps[0].text


def test_trajectories_are_gitignored():
    gitignore = Path(__file__).resolve().parents[1] / ".gitignore"
    ignored = [line.strip() for line in gitignore.read_text().splitlines()]
    assert ".hsai/traj/" in ignored


def test_home_paths_never_reach_the_written_trajectory(tmp_path):
    """The worker prompt names an absolute worktree path; the artifact must not."""
    cfg = load_config()
    runner = FakeRunner(
        repo_root=str(tmp_path), ci_sequence=[True, True], open_issues=[dict(WIDGET_ISSUE)],
        agent_output=json.dumps({
            "result": "done",
            "usage": {"input_tokens": 5, "output_tokens": 1},
            "messages": [{
                "role": "assistant",
                "content": "ran in /Users/someuser/repo with token=ghp_0123456789abcdefghij",
            }],
        }),
    )

    run_once(
        cfg, repo_dir=str(tmp_path), dry_run=False,
        runner=runner, ai_runner=runner, iteration=13,
    )

    written = (tmp_path / ".hsai" / "traj" / "0" / "13.json").read_text()
    assert "/Users/someuser" not in written
    assert "ghp_0123456789abcdefghij" not in written
    assert "~/repo" in written                       # still readable, just anonymised


def test_malformed_ticket_is_refused_and_labeled(tmp_path):
    cfg = load_config()
    open_issues = [
        {
            "number": 11,
            "title": "feat: vague wish",
            "labels": [{"name": "priority:P2"}],
            "assignees": [],
            "body": "make everything better somehow",
        }
    ]
    runner = FakeRunner(
        repo_root=str(tmp_path), ci_sequence=[True, True], open_issues=open_issues,
    )

    result = run_once(
        cfg, repo_dir=str(tmp_path), dry_run=False,
        runner=runner, ai_runner=runner, iteration=1,
    )

    # the vague ticket was not implemented; loop fell through to self-improve
    assert result.kind == IMPROVE
    labeled = [
        c for c in runner.calls
        if c[:3] == ["gh", "issue", "edit"] and "needs-refinement" in c and "11" in c
    ]
    assert labeled, "vague ticket should be labeled needs-refinement"
