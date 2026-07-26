import json
from pathlib import Path

import pytest

from hsai.config import load_config
from hsai.models import ModelChoice
from hsai.orchestrator import (
    HEAL,
    IMPLEMENT,
    IMPROVE,
    build_pr_body,
    decide_path,
    run_once,
)
from hsai.proc import Proc


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
        remote_conclusion: str = "success",
    ) -> None:
        self.repo_root = repo_root
        self.ci_sequence = ci_sequence
        self.open_issues = open_issues or []
        self.remote_conclusion = remote_conclusion
        self.calls: list[list[str]] = []
        self._ci_round = 0
        self._issue_seq = 100
        self._pr_seq = 200

    def __call__(
        self, cmd, *, cwd=None, env=None, timeout=None, input_text=None
    ) -> Proc:
        cmd = list(cmd)
        self.calls.append(cmd)
        return self._dispatch(cmd)

    def _dispatch(self, cmd: list[str]) -> Proc:
        if cmd[:3] == ["gh", "api", "user"]:
            return Proc(cmd, 0, "hsai-bot\n", "")
        if cmd[:2] in (["git", "checkout"], ["git", "pull"], ["git", "fetch"]):
            return Proc(cmd, 0, "", "")
        if cmd[:3] == ["git", "rev-parse", "--show-toplevel"]:
            return Proc(cmd, 0, f"{self.repo_root}\n", "")
        if cmd[:3] in (["git", "worktree", "add"], ["git", "worktree", "remove"]):
            return Proc(cmd, 0, "", "")
        if cmd[:2] in (["git", "add"], ["git", "commit"], ["git", "push"]):
            return Proc(cmd, 0, "", "")
        if cmd[:2] == ["ruff", "check"]:
            ok = self.ci_sequence[self._ci_round]
            return Proc(cmd, 0 if ok else 1, "", "" if ok else "ruff: fake lint failure\n")
        if cmd == ["pytest"]:
            ok = self.ci_sequence[self._ci_round]
            self._ci_round += 1
            return Proc(cmd, 0 if ok else 1, "", "" if ok else "pytest: fake test failure\n")
        if cmd[:2] == ["gh", "api"] and "check-runs" in cmd[2]:
            return Proc(cmd, 0, f"{self.remote_conclusion}\n", "")
        if cmd[:3] == ["gh", "issue", "list"]:
            return Proc(cmd, 0, json.dumps(self.open_issues), "")
        if cmd[:3] == ["gh", "issue", "create"]:
            self._issue_seq += 1
            return Proc(cmd, 0, f"https://github.com/o/r/issues/{self._issue_seq}\n", "")
        if cmd[:3] == ["gh", "issue", "edit"]:
            return Proc(cmd, 0, "", "")
        if cmd[:1] == ["claude"]:
            return Proc(cmd, 0, "ok\n", "")
        if cmd[:3] == ["gh", "pr", "create"]:
            self._pr_seq += 1
            return Proc(cmd, 0, f"https://github.com/o/r/pull/{self._pr_seq}\n", "")
        if cmd[:3] == ["gh", "pr", "merge"]:
            return Proc(cmd, 0, "", "")
        raise AssertionError(f"FakeRunner: unhandled command {cmd!r}")


def test_decide_path():
    assert decide_path(ci_green=False, has_tickets=True) == HEAL
    assert decide_path(ci_green=False, has_tickets=False) == HEAL
    assert decide_path(ci_green=True, has_tickets=True) == IMPLEMENT
    assert decide_path(ci_green=True, has_tickets=False) == IMPROVE


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


def test_run_once_heal_path_with_fake_runner(tmp_path):
    cfg = load_config()
    runner = FakeRunner(repo_root=str(tmp_path), ci_sequence=[False, True])

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

    # a lesson is always written
    assert result.lesson_path
    assert Path(result.lesson_path).exists()

    # heal files its own ticket, never picks up an existing one
    assert any(c[:3] == ["gh", "issue", "create"] for c in runner.calls)
    assert not any(c[:3] == ["gh", "issue", "edit"] for c in runner.calls)

    # PR body is linked to the ticket and records model + lesson
    pr_create = next(c for c in runner.calls if c[:3] == ["gh", "pr", "create"])
    body = pr_create[pr_create.index("--body") + 1]
    assert f"Closes #{result.ticket}" in body
    assert result.model in body
    assert "Lesson learned" in body

    # remote CI was polled and the conclusion recorded on the result + lesson
    assert result.remote_ci == "success"
    assert result.remote_ci_ok is True
    assert "success" in Path(result.lesson_path).read_text()
    assert any(c[:2] == ["gh", "api"] and "check-runs" in c[2] for c in runner.calls)

    # exactly one headless claude invocation, no real subprocess ever ran
    claude_calls = [c for c in runner.calls if c[:1] == ["claude"]]
    assert len(claude_calls) == 1
    assert all(c[0] in {"git", "gh", "ruff", "pytest", "claude"} for c in runner.calls)


def test_run_once_withholds_merge_when_remote_ci_fails(tmp_path):
    """The remote CI gate is explicit: a red `gh checks` result blocks the
    merge even though the local ruff+pytest run and the agent both succeeded.
    """
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
        remote_conclusion="failure",
    )

    result = run_once(
        cfg, repo_dir=str(tmp_path), dry_run=False,
        runner=runner, ai_runner=runner, iteration=1,
    )

    assert result.ci_after is True                # local run was green
    assert result.remote_ci == "failure"
    assert result.remote_ci_ok is False
    assert result.pr is not None                   # PR is still opened, linked + traceable
    assert result.merged is False                  # but merge is withheld
    assert not any(c[:3] == ["gh", "pr", "merge"] for c in runner.calls)

    lesson_text = Path(result.lesson_path).read_text()
    assert "outcome/fail" in lesson_text
    assert "failure" in lesson_text


def test_run_once_implement_path_with_fake_runner(tmp_path):
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

    # remote CI conclusion is recorded too
    assert result.remote_ci == "success"
    assert result.remote_ci_ok is True

    claude_calls = [c for c in runner.calls if c[:1] == ["claude"]]
    assert len(claude_calls) == 1
    assert all(c[0] in {"git", "gh", "ruff", "pytest", "claude"} for c in runner.calls)
