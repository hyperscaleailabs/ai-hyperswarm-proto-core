import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from hsai import orchestrator
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
    ) -> None:
        self.repo_root = repo_root
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
            return Proc(cmd, 0, "ok\n", "")
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

## Synthesis rationale
Combines openai/swarm with SWE-agent/SWE-agent.
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


CITING_BODY = """## Problem
Every PR claims the same three repos as evidence.

## Proposal
Resolve provenance from the ticket's practice citation.

## Acceptance criteria
- [ ] references come from the cited card
- [ ] the pinned top-3 triple is gone

## Verification plan
- [ ] pytest green

## Practices
- PR-0003
"""

# The repos the old hardcode stamped on every PR regardless of content.
_OLD_HARDCODED_TRIPLE = ("langchain-ai/langchain", "FoundationAgents/MetaGPT", "crewAIInc/crewAI")


def _seed_practice_cards(wt: Path) -> None:
    """Copy the real vault's practice cards into the fake worktree."""
    src = Path(__file__).resolve().parents[1] / "knowledge" / "practices"
    dst = wt / "knowledge" / "practices"
    dst.mkdir(parents=True)
    for card in src.glob("*.md"):
        (dst / card.name).write_text(card.read_text())


def test_pr_and_lesson_cite_the_tickets_practice_card_not_the_pinned_triple(
    tmp_path, monkeypatch
):
    cfg = load_config()
    wt = _pin_worktree_path(monkeypatch, tmp_path, cfg)
    _seed_practice_cards(wt)
    open_issues = [
        {
            "number": 21,
            "title": "feat: resolve evidence from practice cards",
            "labels": [{"name": "priority:P2"}],
            "assignees": [],
            "body": CITING_BODY,
        }
    ]
    runner = FakeRunner(
        repo_root=str(tmp_path), ci_sequence=[True, True], open_issues=open_issues,
        worktree_status="?? src/hsai/practices.py\n",
    )

    result = run_once(
        cfg, repo_dir=str(tmp_path), dry_run=False,
        runner=runner, ai_runner=runner, iteration=1,
    )

    assert result.kind == IMPLEMENT
    assert result.ticket == 21
    assert any("evidence: practices" in n for n in result.notes)

    # PR-0003 was observed in microsoft/JARVIS - that, and only that, is what
    # the PR and the lesson may claim as reference-set evidence.
    pr_create = next(c for c in runner.calls if c[:3] == ["gh", "pr", "create"])
    pr_body = pr_create[pr_create.index("--body") + 1]
    lesson_text = Path(result.lesson_path).read_text()

    for text in (pr_body, lesson_text):
        assert "microsoft/JARVIS" in text
        for stale in _OLD_HARDCODED_TRIPLE:
            assert stale not in text, f"{stale} was stamped on unrelated work"

    # the lesson links back to the card itself, so the graph connects
    assert "[[PR-0003-a-controller-routes-each-task-to-the-cheapest-capable-model]]" in lesson_text


def test_code_ticket_without_a_resolvable_citation_is_labeled_needs_refinement(tmp_path):
    cfg = load_config()
    open_issues = [
        {
            "number": 22,
            "title": "feat: well-formed but uncited",
            "labels": [{"name": "priority:P2"}],
            "assignees": [],
            # Well-formed, but names no practice card and no pinned repo.
            "body": "## Acceptance criteria\n- [ ] a\n- [ ] b\n\n## Verification plan\n- [ ] pytest",
        }
    ]
    runner = FakeRunner(
        repo_root=str(tmp_path), ci_sequence=[True, True], open_issues=open_issues,
    )

    result = run_once(
        cfg, repo_dir=str(tmp_path), dry_run=False,
        runner=runner, ai_runner=runner, iteration=1,
    )

    # refused rather than implemented on invented provenance
    assert result.kind == IMPROVE
    assert result.ticket != 22
    assert [
        c for c in runner.calls
        if c[:3] == ["gh", "issue", "edit"] and "needs-refinement" in c and "22" in c
    ]


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
