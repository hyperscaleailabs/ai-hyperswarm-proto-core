import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from hsai import ledger, orchestrator, recall, review, trajectory
from hsai.config import load_config
from hsai.models import ModelChoice, Task, select
from hsai.orchestrator import (
    HEAL,
    IMPLEMENT,
    IMPROVE,
    _format_error_with_context,
    _phase_artifacts,
    _task_prompt,
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


def _reviewer_envelope(verdict: dict, *, prose: str = "Checked the diff.") -> str:
    """What the independent reviewer prints: prose plus a fenced JSON verdict."""
    return json.dumps(
        {
            "type": "result",
            "result": f"{prose}\n\n```json\n{json.dumps(verdict)}\n```\n",
            "usage": {"input_tokens": 400, "output_tokens": 60},
        }
    )


REVIEW_APPROVE = _reviewer_envelope(
    {
        "approve": True,
        "blocking": [],
        "advisory": ["consider naming the helper"],
        "rationale": "Every acceptance criterion is covered by code and a test.",
    }
)
REVIEW_BLOCK = _reviewer_envelope(
    {
        "approve": False,
        "blocking": ["src/hsai/widget.py: criterion 2 has no test proving it"],
        "advisory": [],
        "rationale": "The diff claims a criterion it never demonstrates.",
    }
)


def _claude_prompts(runner) -> list[str]:
    return [c[2] for c in runner.calls if c[:1] == ["claude"]]


def _worker_prompts(runner) -> list[str]:
    return [p for p in _claude_prompts(runner) if review.PROMPT_MARKER not in p]


def _review_prompts(runner) -> list[str]:
    return [p for p in _claude_prompts(runner) if review.PROMPT_MARKER in p]


def _iteration_records(cfg, root) -> list[ledger.LedgerRecord]:
    """Ledger records for the iteration itself, without the review's own line."""
    records = ledger.read_records(ledger.ledger_path(cfg, root))
    return [r for r in records if r.kind != "review"]


class FakeRunner:
    """Deterministic stand-in for a :class:`hsai.proc.Runner`.

    Never touches the network or spawns a real subprocess - every git/gh/claude
    invocation is matched by command prefix and answered with a canned `Proc`.
    `ci_sequence` gives the ruff+pytest outcome for the Nth `ci.run_local()`
    call (index 0 = ci_before, index 1 = ci_after, ...). A `claude` call is
    answered as the worker or as the independent reviewer depending on which
    prompt it carries.
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
        review_output: str = REVIEW_APPROVE,
    ) -> None:
        self.repo_root = repo_root
        self.agent_output = agent_output
        self.review_output = review_output
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
        self, cmd, *, cwd=None, env=None, env_remove=None, timeout=None, input_text=None
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
        if cmd[:2] == ["git", "diff"]:
            # What the review gate reads off the branch: paths, then the diff.
            if "--name-only" in cmd:
                return Proc(cmd, 0, "src/hsai/widget.py\ntests/test_widget.py\n", "")
            return Proc(cmd, 0, "diff --git a/src/hsai/widget.py\n+def widget(): ...\n", "")
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
            prompt = cmd[2] if len(cmd) > 2 else ""
            if review.PROMPT_MARKER in prompt:
                return Proc(cmd, 0, self.review_output, "")
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


def test_routing_columns_label_a_ledger_record_with_the_decision():
    """The ledger columns that make a cost record a training example."""
    cfg = load_config()
    choice = select(Task(kind="improve", title="migration: upgrade", est_files=9), cfg)
    columns = orchestrator._routing_columns(choice)

    assert columns["complexity_score"] == 6
    assert columns["est_files"] == 9
    assert columns["heavy_signals"] == 1 and columns["light_signals"] == 0
    assert columns["size_label"] == ""
    assert columns["demoted"] is False
    assert columns["strategy"] == "heuristic-v1"
    assert columns["shadow_strategy"] == "heuristic-v2"
    # Every column is a valid LedgerRecord field, so the record still parses.
    assert ledger.LedgerRecord(
        iteration=1, block=1, ticket=1, kind="improve", tier=choice.tier,
        model=choice.model, wall_clock_seconds=1.0, attempts=1, outcome="merged",
        **columns,
    ).complexity_score == 6


def test_routing_columns_of_a_featureless_choice_are_empty_not_zero():
    # A hand-built ModelChoice (synthesis, review) never saw the scorer; it must
    # not enter hsai.calibrate's training set disguised as a zero-scored task.
    columns = orchestrator._routing_columns(
        ModelChoice(tier="heavy", model="opus", rationale="x")
    )
    assert columns["complexity_score"] is None
    assert columns["shadow_tier"] is None and columns["shadow_strategy"] is None


def test_build_pr_body_renders_the_shadow_routing_line():
    """Shadow evaluation is only useful if a human sees it on the PR."""
    kwargs = dict(
        ticket=42, choice=None, lesson_note="2026-08-20-x",
        lesson_summary="s", ci_summary="green",
    )
    plain = ModelChoice(tier="standard", model="sonnet", rationale="score=3 -> standard")
    shadowed = replace(
        plain, shadow_tier="heavy", shadow_strategy="heuristic-v2",
    )
    agreeing = replace(
        plain, shadow_tier="standard", shadow_strategy="heuristic-v2",
    )

    body = build_pr_body(**{**kwargs, "choice": shadowed})
    assert "**shadow**: `heuristic-v2` would have chosen `heavy` (disagrees)" in body
    assert "routing is unchanged" in body
    # ...and it sits inside the Model used section, not tacked on elsewhere.
    assert body.index("## Model used") < body.index("**shadow**") < body.index("## CI")

    # An agreeing shadow still reports, so silence never means "not evaluated".
    assert "`standard` (agrees)" in build_pr_body(**{**kwargs, "choice": agreeing})

    # No shadow tier (a manually constructed choice) => no line at all, and the
    # traceability invariants are untouched either way.
    without = build_pr_body(**{**kwargs, "choice": plain})
    assert "**shadow**" not in without
    for text in (without, body):
        assert "Closes #42" in text
        assert "## Model used" in text
        assert "## Lesson learned" in text


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

    # one headless worker invocation plus one independent reviewer, no real
    # subprocess ever ran
    assert len(_worker_prompts(runner)) == 1
    assert len(_review_prompts(runner)) == 1
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

    assert len(_worker_prompts(runner)) == 1
    assert len(_review_prompts(runner)) == 1
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


# --- independent review gate (a second opinion before any PR opens) ---------

CODE_ISSUE = {
    "number": 9,
    "title": "feat: add widget",
    "labels": [{"name": "priority:P2"}],
    "assignees": [],
    "body": WELL_FORMED_BODY,
}


def _review_run(tmp_path, *, review_output, issue=None, remote_ci="SUCCESS", cfg=None):
    cfg = cfg or load_config()
    runner = FakeRunner(
        repo_root=str(tmp_path), ci_sequence=[True, True],
        open_issues=[dict(issue or CODE_ISSUE)],
        worktree_status="?? src/hsai/widget.py\n",
        review_output=review_output, remote_ci=remote_ci,
    )
    result = run_once(
        cfg, repo_dir=str(tmp_path), dry_run=False,
        runner=runner, ai_runner=runner, iteration=1,
    )
    return cfg, runner, result


def test_a_blocking_review_verdict_never_opens_a_pr_and_costs_one_attempt(tmp_path):
    cfg, runner, result = _review_run(tmp_path, review_output=REVIEW_BLOCK)

    # The gate refused the change: no branch pushed, no PR, no merge.
    assert result.review == "blocked"
    assert result.recovered is True
    assert result.pr is None and result.merged is False
    assert not any(c[:3] == ["gh", "pr", "create"] for c in runner.calls)
    assert not any(c[:3] == ["gh", "pr", "merge"] for c in runner.calls)
    assert not any(c[:2] == ["git", "push"] for c in runner.calls)

    # It routed through the SAME retry policy a red PR uses - attempts:1, still
    # unassigned, no new label and no new stall state.
    assert any("attempts:1" in c for c in runner.calls)
    assert any(
        c[:3] == ["gh", "issue", "edit"] and "--remove-assignee" in c for c in runner.calls
    )
    assert not any("blocked" in c for c in runner.calls)
    assert any("independent review" in n for n in result.notes)

    # Both the review and the blocked iteration are on the ledger.
    records = ledger.read_records(ledger.ledger_path(cfg, tmp_path))
    assert [(r.kind, r.outcome) for r in records] == [
        ("review", "blocked"), (IMPLEMENT, "review_blocked"),
    ]


def test_a_blocking_review_at_the_last_attempt_blocks_the_ticket_as_usual(tmp_path):
    issue = dict(CODE_ISSUE, labels=[{"name": "priority:P2"}, {"name": "attempts:1"}])
    _cfg, runner, result = _review_run(tmp_path, review_output=REVIEW_BLOCK, issue=issue)

    # max_ticket_attempts=2: the second failure hands the ticket to a human -
    # exactly what a second red CI would have done.
    assert result.recovered is True and result.pr is None
    edits = [c for c in runner.calls if c[:3] == ["gh", "issue", "edit"]]
    assert any("blocked" in c for c in edits)
    assert any("--remove-label" in c and "attempts:1" in c for c in edits)


def test_an_approving_verdict_is_recorded_on_the_pr_and_in_the_lesson(tmp_path):
    cfg, runner, result = _review_run(tmp_path, review_output=REVIEW_APPROVE)

    assert result.review == "approve" and result.merged is True
    assert "review=approve" in result.describe()

    pr_create = next(c for c in runner.calls if c[:3] == ["gh", "pr", "create"])
    pr_body = pr_create[pr_create.index("--body") + 1]
    lesson_text = Path(result.lesson_path).read_text()
    for text in (pr_body, lesson_text):
        assert "## Independent review" in text
        assert "**APPROVED**" in text
        assert "Every acceptance criterion is covered by code and a test." in text
        assert "consider naming the helper" in text        # advisory findings too

    # The reviewer is a different model on a different tier than the author.
    review_record = next(
        r for r in ledger.read_records(ledger.ledger_path(cfg, tmp_path))
        if r.kind == "review"
    )
    assert review_record.model != result.model
    assert review_record.tier != "standard"
    assert f"`{review_record.model}`" in pr_body


def test_the_reviewer_is_shown_the_criteria_and_the_branch_diff(tmp_path):
    _cfg, runner, _result = _review_run(tmp_path, review_output=REVIEW_APPROVE)

    prompt = _review_prompts(runner)[0]
    assert "- widget builds" in prompt and "- widget tested" in prompt
    assert "src/hsai/widget.py" in prompt
    assert "+def widget(): ..." in prompt
    # and it is told a different model wrote the change
    assert "You did not write it" in prompt


def test_a_red_local_build_skips_the_review_and_says_so(tmp_path):
    cfg = load_config()
    runner = FakeRunner(
        repo_root=str(tmp_path), ci_sequence=[True, False],
        open_issues=[dict(CODE_ISSUE)], worktree_status="?? src/hsai/widget.py\n",
        remote_ci="FAILURE",
    )

    result = run_once(
        cfg, repo_dir=str(tmp_path), dry_run=False,
        runner=runner, ai_runner=runner, iteration=1,
    )

    # Nothing to second-guess: the CI gate already owns this outcome.
    assert result.ci_after is False
    assert result.review == "skipped"
    assert _review_prompts(runner) == []
    assert not any(r.kind == "review" for r in ledger.read_records(
        ledger.ledger_path(cfg, tmp_path)
    ))
    pr_create = next(c for c in runner.calls if c[:3] == ["gh", "pr", "create"])
    body = pr_create[pr_create.index("--body") + 1]
    assert "## Independent review" in body
    assert "_(not run: local CI is red" in body


def test_an_approved_change_still_cannot_merge_without_a_green_remote_ci(tmp_path):
    """The gate is additive: approval is necessary, never sufficient."""
    cfg, runner, result = _review_run(
        tmp_path, review_output=REVIEW_APPROVE, remote_ci="FAILURE"
    )

    assert result.review == "approve"
    assert result.remote == "FAILURE"
    assert result.merged is False and result.recovered is True
    assert not any(c[:3] == ["gh", "pr", "merge"] for c in runner.calls)
    assert any(c[:3] == ["gh", "pr", "close"] for c in runner.calls)
    assert ledger.read_records(ledger.ledger_path(cfg, tmp_path))[-1].outcome == "recovered"


def test_disabling_the_review_gate_restores_the_pre_review_flow(tmp_path):
    cfg = replace(load_config(), review={"enabled": False})
    _cfg, runner, result = _review_run(
        tmp_path, review_output=REVIEW_BLOCK, cfg=cfg
    )

    # Even a blocking reviewer is never consulted, and the PR merges as before.
    assert _review_prompts(runner) == []
    assert result.review == "skipped" and result.merged is True
    assert [r.kind for r in ledger.read_records(ledger.ledger_path(cfg, tmp_path))] == [
        IMPLEMENT
    ]


def test_build_pr_body_always_carries_an_independent_review_section():
    choice = ModelChoice(tier="standard", model="sonnet", rationale="x")
    kwargs = dict(
        ticket=42, choice=choice, lesson_note="2026-08-12-note",
        lesson_summary="kept it small", ci_summary="green", kind=IMPLEMENT,
    )
    verdict = review.ReviewVerdict(
        approve=True, advisory=["nit"], rationale="Covered end to end.",
        reviewer_model="haiku", reviewer_tier="light",
    )
    body = build_pr_body(**kwargs, review_verdict=verdict.render())
    assert "## Independent review" in body
    assert "**APPROVED**" in body and "`haiku`" in body
    assert "Covered end to end." in body

    # A PR without a verdict says so rather than staying silent about it.
    plain = build_pr_body(**kwargs)
    assert "## Independent review" in plain
    assert "_(no independent review recorded)_" in plain


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
    # Exactly one trajectory file for the one worker invocation, sharded by block.
    assert len(_worker_prompts(runner)) == 1
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

    # One trajectory per WORKER run; the reviewer's own calls are metered in the
    # ledger, not replayed as authored work.
    assert len(_worker_prompts(runner)) == len(_trajectory_files(tmp_path)) == 2
    assert [p.name for p in _trajectory_files(tmp_path)] == ["1.json", "2.json"]
    # Every invocation asked the CLI for the structured envelope.
    claude_calls = [c for c in runner.calls if c[:1] == ["claude"]]
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
    # The independent review is metered like any other spend, so the block
    # carries one 'review' line next to the iteration's own.
    assert [r.kind for r in records] == ["review", IMPLEMENT]
    authored = _iteration_records(cfg, tmp_path)[0]
    assert authored.input_tokens == 1500 and authored.output_tokens == 320

    agg = ledger.aggregate_block(records, block=0)
    assert agg.input_tokens == 1900 and agg.output_tokens == 380   # 1500/320 + 400/60
    assert "2280 tokens" in agg.summary()      # no longer reporting zero
    # Tokens per merged PR is the block's efficiency signal (G4), and reviewing
    # a change is part of what delivering it costs.
    assert agg.merged_iterations == 1
    assert agg.tokens_per_merged_pr() == 2280
    assert "2280 tokens/merged PR" in agg.summary()


def test_run_once_honors_an_explicit_block_over_the_iteration_derived_default(tmp_path):
    """`hsai cycle` passes its own index explicitly rather than relying on the
    `iteration // 100` fallback, so real cycle 0 and an ad-hoc run never
    collide on the same block just because both happened to use small
    iteration numbers."""
    cfg = load_config()
    runner = FakeRunner(
        repo_root=str(tmp_path), ci_sequence=[True, True], open_issues=[dict(WIDGET_ISSUE)]
    )

    run_once(
        cfg, repo_dir=str(tmp_path), dry_run=False,
        runner=runner, ai_runner=runner, iteration=1, block=41355,
    )

    records = ledger.read_records(ledger.ledger_path(cfg, tmp_path))
    assert all(r.block == 41355 for r in records)


def test_adhoc_loop_iterations_do_not_pollute_cycle_zero(tmp_path):
    """`hsai loop` runs outside any governed cycle. Acceptance criterion: its
    iterations must not share a block index with a real cycle 0."""
    cfg = load_config()
    runner = FakeRunner(
        repo_root=str(tmp_path), ci_sequence=[True, True, True, True],
        open_issues=[dict(WIDGET_ISSUE)],
    )

    orchestrator.run_loop(
        cfg, repo_dir=str(tmp_path), max_iterations=2, runner=runner, ai_runner=runner,
    )

    records = ledger.read_records(ledger.ledger_path(cfg, tmp_path))
    assert records                                       # the loop did run
    assert all(r.block == orchestrator.AD_HOC_BLOCK for r in records)
    # A real cycle 0's aggregate stays untouched by the ad-hoc noise.
    cycle_zero = ledger.aggregate_block(records, block=0)
    assert cycle_zero.iterations == 0


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
    authored = _iteration_records(cfg, tmp_path)[0]
    assert authored.input_tokens is None and authored.output_tokens is None

    # The lesson says so explicitly rather than silently omitting telemetry.
    lesson_text = Path(result.lesson_path).read_text()
    assert "## Execution trace" in lesson_text
    assert "| telemetry | unavailable |" in lesson_text
    assert "| exit status | ok |" in lesson_text


def test_execution_trace_section_reports_turns_tools_and_tokens(tmp_path):
    """Acceptance criterion: every lesson from a model run carries turns,
    tools used, token counts, exit status, and duration."""
    cfg = load_config()
    runner = FakeRunner(
        repo_root=str(tmp_path), ci_sequence=[True, True], open_issues=[dict(WIDGET_ISSUE)],
    )

    result = run_once(
        cfg, repo_dir=str(tmp_path), dry_run=False,
        runner=runner, ai_runner=runner, iteration=21,
    )

    lesson_text = Path(result.lesson_path).read_text()
    assert "## Execution trace" in lesson_text
    assert "| tools used | `Read` |" in lesson_text     # AGENT_JSON's tool_use step
    assert "| tokens | 1500 in / 320 out |" in lesson_text
    assert "| exit status | ok |" in lesson_text
    assert "| telemetry | ok |" in lesson_text


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


# --- lesson recall: the knowledge base as an INPUT ---------------------------

RECALL_TICKET_TITLE = "feat: gate merges on the remote CI rollup"


def _seed_recall_corpus(root: Path) -> None:
    """Plant a small vault in the tree the iteration will read from."""
    lessons = root / "knowledge" / "lessons"
    lessons.mkdir(parents=True, exist_ok=True)
    (lessons / "2026-01-01-remote-ci-is-the-only-gate.md").write_text(
        "---\ntags:\n  - lesson\n  - outcome/fail\n  - kind/implement\n"
        "created: 2026-01-01\n---\n\n"
        "# Remote CI rollup is the only merge gate\n\n"
        "## Lesson learned\nLocal green is not remote green; poll the rollup first.\n"
    )
    (lessons / "2026-01-02-obsidian-vault-layout.md").write_text(
        "---\ntags:\n  - lesson\n  - outcome/pass\n  - kind/improve\n"
        "created: 2026-01-02\n---\n\n"
        "# Obsidian vault layout\n\n"
        "## Lesson learned\nWikilinks up to a MOC make the graph view useful.\n"
    )


def _implement_run(cfg, tmp_path, monkeypatch, iteration: int):
    """One fake-runner implement iteration; returns (prompt, pr_body, result)."""
    wt = _pin_worktree_path(monkeypatch, tmp_path, cfg, iteration)
    _seed_recall_corpus(wt)
    open_issues = [
        {
            "number": 7,
            "title": RECALL_TICKET_TITLE,
            "labels": [{"name": "priority:P2"}],
            "assignees": [],
            "body": WELL_FORMED_BODY,
        }
    ]
    runner = FakeRunner(
        repo_root=str(tmp_path), ci_sequence=[True, True], open_issues=open_issues,
        worktree_status="?? src/hsai/gate.py\n",
    )
    result = run_once(
        cfg, repo_dir=str(tmp_path), dry_run=False,
        runner=runner, ai_runner=runner, iteration=iteration,
    )
    prompt = next(c for c in runner.calls if c[:1] == ["claude"])[2]
    pr_create = next(c for c in runner.calls if c[:3] == ["gh", "pr", "create"])
    return prompt, pr_create[pr_create.index("--body") + 1], result


def _with_recall(**overrides):
    cfg = load_config()
    knowledge = dict(cfg.knowledge)
    knowledge["recall"] = {**(knowledge.get("recall") or {}), **overrides}
    return replace(cfg, knowledge=knowledge)


def test_task_prompt_renders_recalled_lessons_only_when_there_are_some():
    cfg = load_config()
    section = f"{recall.HEADING}:\n- [[a-note]] (fail/heal) - do not do that"
    for kind in (HEAL, IMPLEMENT, IMPROVE):
        bare = _task_prompt(kind, cfg, "t", "b")
        assert recall.HEADING not in bare
        # an empty recall changes nothing at all - byte-for-byte
        assert _task_prompt(kind, cfg, "t", "b", "") == bare

        with_lessons = _task_prompt(kind, cfg, "t", "b", section)
        assert with_lessons == f"{bare}\n\n{section}"
        assert with_lessons.endswith(section)   # the ticket stays the instruction


def test_build_pr_body_renders_prior_lessons_consulted():
    choice = ModelChoice(tier="standard", model="sonnet", rationale="x")
    kwargs = dict(
        ticket=42, choice=choice, lesson_note="2026-01-03-note",
        lesson_summary="s", ci_summary="green", kind=IMPLEMENT,
    )
    body = build_pr_body(**kwargs, recalled=("2026-01-01-a", "2026-01-02-b"))
    assert "## Prior lessons consulted" in body
    assert "- [[2026-01-01-a]]" in body and "- [[2026-01-02-b]]" in body

    # with nothing recalled the section vanishes and the surrounding text is
    # exactly what it was before recall existed
    plain = build_pr_body(**kwargs)
    assert "## Prior lessons consulted" not in plain
    assert plain == build_pr_body(**kwargs, recalled=())
    assert (
        "See [[2026-01-03-note]] in the knowledge base.\n\n## Reference-set evidence"
        in plain
    )


def test_recalled_lessons_reach_the_prompt_the_lesson_and_the_pr_body(
    tmp_path, monkeypatch
):
    cfg = load_config()
    budget = recall.RecallConfig.from_core(cfg)
    prompt, pr_body, result = _implement_run(cfg, tmp_path, monkeypatch, 1)

    # 1. the worker was shown prior lessons, bounded by the configured budget
    assert recall.HEADING in prompt
    injected = prompt[prompt.index(recall.HEADING):]   # recall is appended last
    assert len(injected) <= budget.max_chars
    assert 0 < injected.count("- [[") <= budget.k
    # failures outrank successes, so the failing note leads
    assert "[[2026-01-01-remote-ci-is-the-only-gate]]" in injected

    # 2. the retrieval is carried on the result...
    assert "2026-01-01-remote-ci-is-the-only-gate" in result.recalled
    assert len(result.recalled) == injected.count("- [[")

    # 3. ...written into the lesson's frontmatter...
    frontmatter = Path(result.lesson_path).read_text().split("---\n")[1]
    assert "recalled:" in frontmatter
    for name in result.recalled:
        assert f"  - {name}" in frontmatter

    # 4. ...and rendered on the PR for after-the-fact audit
    assert "## Prior lessons consulted" in pr_body
    for name in result.recalled:
        assert f"- [[{name}]]" in pr_body


def test_disabling_recall_restores_the_pre_change_prompt_and_pr_body(
    tmp_path, monkeypatch
):
    off = _with_recall(enabled=False)
    prompt, pr_body, result = _implement_run(off, tmp_path, monkeypatch, 2)

    # byte-identical to what _task_prompt produced before recall existed
    assert prompt == _task_prompt(IMPLEMENT, off, RECALL_TICKET_TITLE, WELL_FORMED_BODY)
    assert recall.HEADING not in prompt
    assert "## Prior lessons consulted" not in pr_body
    assert result.recalled == []
    assert "recalled:" not in Path(result.lesson_path).read_text().split("---\n")[1]


def test_dry_run_still_records_what_it_recalled(tmp_path):
    cfg = load_config()
    _seed_recall_corpus(tmp_path)

    result = run_once(cfg, repo_dir=str(tmp_path), dry_run=True, iteration=1)

    assert result.kind == IMPROVE
    assert result.recalled                       # retrieval runs without an agent
    assert "recalled:" in Path(result.lesson_path).read_text().split("---\n")[1]
