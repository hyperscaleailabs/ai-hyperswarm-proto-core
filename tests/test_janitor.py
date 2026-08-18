"""hsai.janitor: pure classification, the scan/build/reap execution layer.

Fixtures mirror the failure modes the ticket names explicitly: a killed
iteration leaves an assigned ticket, a worktree, and (sometimes) a pushed
branch behind. Every test that exercises ``reap`` proves either that a
destructive command was issued for exactly the right reason, or - for
``--dry-run`` and for human/ambiguous state - that none was.
"""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime

from hsai import github, janitor
from hsai.config import load_config
from hsai.proc import Proc


# --- pure classification: worktrees ------------------------------------------

def _wt(**kwargs) -> janitor.WorktreeEntry:
    base = {"path": "/repo/.hsai/worktrees/hsai/iter-1-1-abc", "branch": "hsai/iter-1-1-abc"}
    base.update(kwargs)
    return janitor.WorktreeEntry(**base)


def test_locked_worktree_is_active_regardless_of_everything_else():
    v = janitor.classify_worktree(_wt(locked=True, commits_ahead=5), ttl_seconds=100)
    assert v.status == janitor.ACTIVE
    assert "locked" in v.reason


def test_worktree_with_an_open_pr_is_active():
    v = janitor.classify_worktree(_wt(has_open_pr=True, age_seconds=99999), ttl_seconds=100)
    assert v.status == janitor.ACTIVE
    assert "open PR" in v.reason


def test_worktree_with_commits_ahead_and_no_pr_is_ambiguous_not_reclaimed():
    v = janitor.classify_worktree(_wt(commits_ahead=2, age_seconds=99999), ttl_seconds=100)
    assert v.status == janitor.AMBIGUOUS
    assert "commit(s) ahead" in v.reason


def test_worktree_with_no_derivable_age_is_ambiguous():
    v = janitor.classify_worktree(_wt(age_seconds=None), ttl_seconds=100)
    assert v.status == janitor.AMBIGUOUS
    assert "no derivable age" in v.reason


def test_young_worktree_with_no_pr_and_no_commits_is_active_not_orphaned():
    """The whole point of the TTL: a worker mid-run has no PR and no commits
    yet - it must never look identical to an abandoned one."""
    v = janitor.classify_worktree(_wt(age_seconds=10), ttl_seconds=4500)
    assert v.status == janitor.ACTIVE
    assert "still running" in v.reason


def test_old_worktree_with_no_pr_and_no_commits_is_orphaned():
    v = janitor.classify_worktree(_wt(age_seconds=99999), ttl_seconds=4500)
    assert v.status == janitor.ORPHANED
    assert "no open PR, no commits ahead" in v.reason


# --- pure classification: ticket claims --------------------------------------

def _issue(**kwargs) -> github.Issue:
    base = {
        "number": 9, "title": "feat: x", "labels": (), "assignees": ("hsai-bot",),
        "updated_at": "2026-08-18T00:00:00Z",
    }
    base.update(kwargs)
    return github.Issue(**base)


def _epoch(iso: str) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


NOW = _epoch("2026-08-18T00:00:00Z")  # for readable fixtures below


def test_claim_assigned_to_a_human_is_never_stranded():
    issue = _issue(assignees=("a-human",), updated_at="2020-01-01T00:00:00Z")
    v = janitor.classify_claim(
        issue, now=NOW, ttl_seconds=100, loop_login="hsai-bot", referenced_by_open_pr=False
    )
    assert v.status == janitor.HUMAN
    assert "never touched" in v.reason


def test_claim_with_any_non_loop_assignee_is_human_even_if_the_loop_is_also_listed():
    issue = _issue(assignees=("hsai-bot", "a-human"), updated_at="2020-01-01T00:00:00Z")
    v = janitor.classify_claim(
        issue, now=NOW, ttl_seconds=100, loop_login="hsai-bot", referenced_by_open_pr=False
    )
    assert v.status == janitor.HUMAN


def test_claim_referenced_by_an_open_pr_is_fresh_regardless_of_age():
    issue = _issue(updated_at="2020-01-01T00:00:00Z")
    v = janitor.classify_claim(
        issue, now=NOW, ttl_seconds=100, loop_login="hsai-bot", referenced_by_open_pr=True
    )
    assert v.status == janitor.FRESH


def test_claim_within_ttl_is_fresh():
    issue = _issue(updated_at="2026-08-18T00:00:00Z")
    v = janitor.classify_claim(
        issue, now=NOW + 50, ttl_seconds=100, loop_login="hsai-bot", referenced_by_open_pr=False
    )
    assert v.status == janitor.FRESH


def test_claim_past_ttl_with_no_pr_and_the_loops_own_login_is_stranded():
    issue = _issue(updated_at="2026-08-18T00:00:00Z")
    v = janitor.classify_claim(
        issue, now=NOW + 200, ttl_seconds=100, loop_login="hsai-bot", referenced_by_open_pr=False
    )
    assert v.status == janitor.STRANDED
    assert "no open PR" in v.reason


def test_claim_with_unparseable_timestamp_is_ambiguous():
    issue = _issue(updated_at="not-a-timestamp")
    v = janitor.classify_claim(
        issue, now=NOW, ttl_seconds=100, loop_login="hsai-bot", referenced_by_open_pr=False
    )
    assert v.status == janitor.AMBIGUOUS
    assert "no updatedAt" in v.reason


def test_claim_with_no_timestamp_is_ambiguous():
    issue = _issue(updated_at="")
    v = janitor.classify_claim(
        issue, now=NOW, ttl_seconds=100, loop_login="hsai-bot", referenced_by_open_pr=False
    )
    assert v.status == janitor.AMBIGUOUS


# --- TTL derivation -----------------------------------------------------------

def test_derive_ttl_from_core_yaml_defaults():
    cfg = load_config()
    # core.yaml: agent_timeout_seconds=1200, ci_remote_timeout_seconds=300,
    # janitor.ttl_safety_multiplier=3.0 -> (1200+300)*3 = 4500
    assert janitor.derive_ttl_seconds(cfg) == 4500.0


def test_derive_ttl_falls_back_when_agent_timeout_is_unset():
    cfg = load_config()
    cfg = replace(cfg, agent_timeout=None)
    expected = (janitor.DEFAULT_AGENT_TIMEOUT_SECONDS + cfg.ci_remote_timeout) * 3.0
    assert janitor.derive_ttl_seconds(cfg) == expected


def test_derive_ttl_respects_a_custom_safety_multiplier():
    cfg = load_config()
    cfg = replace(cfg, janitor={"ttl_safety_multiplier": 1.0})
    assert janitor.derive_ttl_seconds(cfg) == cfg.agent_timeout + cfg.ci_remote_timeout


# --- git worktree list --porcelain parsing -----------------------------------

def test_parse_worktree_list_handles_multiple_entries_locked_and_detached():
    text = (
        "worktree /repo\n"
        "HEAD deadbeef\n"
        "branch refs/heads/main\n"
        "\n"
        "worktree /repo/.hsai/worktrees/hsai/iter-1-1-abc\n"
        "HEAD cafebabe\n"
        "branch refs/heads/hsai/iter-1-1-abc\n"
        "locked\n"
        "\n"
        "worktree /repo/.hsai/worktrees/repro-check-x\n"
        "HEAD f00dcafe\n"
        "detached\n"
    )
    entries = janitor.parse_worktree_list(text)
    assert len(entries) == 3
    assert entries[0]["worktree"] == "/repo"
    assert entries[0]["branch"] == "refs/heads/main"
    assert entries[1]["locked"] is True
    assert entries[1]["branch"] == "refs/heads/hsai/iter-1-1-abc"
    assert entries[2]["detached"] is True
    assert "branch" not in entries[2]


# --- the execution layer: scan / build_plan / reap ---------------------------

class _FakeRunner:
    """Answers every git/gh call the janitor's execution layer makes."""

    def __init__(self, *, porcelain="", prs=(), issues=(), login="hsai-bot",
                 rev_counts=None, push_delete_fail=()):
        self.calls: list[list[str]] = []
        self.porcelain = porcelain
        self.prs = list(prs)
        self.issues = list(issues)
        self.login = login
        self.rev_counts = rev_counts or {}
        self.push_delete_fail = set(push_delete_fail)

    def __call__(self, cmd, **kwargs):
        cmd = list(cmd)
        self.calls.append(cmd)
        if cmd[:3] == ["git", "worktree", "list"]:
            return Proc(cmd, 0, self.porcelain, "")
        if cmd[:2] == ["git", "rev-list"]:
            branch = cmd[-1].split("..", 1)[1]
            return Proc(cmd, 0, f"{self.rev_counts.get(branch, 0)}\n", "")
        if cmd[:3] == ["git", "rev-parse", "--show-toplevel"]:
            return Proc(cmd, 0, "/repo\n", "")
        if cmd[:3] == ["git", "worktree", "remove"]:
            return Proc(cmd, 0, "", "")
        if cmd[:3] == ["git", "worktree", "prune"]:
            return Proc(cmd, 0, "", "")
        if cmd[:3] == ["git", "push", "origin"] and "--delete" in cmd:
            branch = cmd[-1]
            if branch in self.push_delete_fail:
                return Proc(cmd, 1, "", "remote ref does not exist")
            return Proc(cmd, 0, "", "")
        if cmd[:3] == ["gh", "pr", "list"]:
            data = [
                {"number": pr["number"], "title": pr.get("title", ""),
                 "body": pr.get("body", ""), "headRefName": pr["head_ref"]}
                for pr in self.prs
            ]
            return Proc(cmd, 0, json.dumps(data), "")
        if cmd[:3] == ["gh", "issue", "list"]:
            return Proc(cmd, 0, json.dumps(self.issues), "")
        if cmd[:2] == ["gh", "api"]:
            return Proc(cmd, 0, f"{self.login}\n", "")
        if cmd[:3] == ["gh", "issue", "edit"]:
            return Proc(cmd, 0, "", "")
        return Proc(cmd, 0, "", "")

    def calls_like(self, *prefix: str) -> list[list[str]]:
        return [c for c in self.calls if c[: len(prefix)] == list(prefix)]


def _porcelain(*entries: tuple[str, str]) -> str:
    """Build porcelain text for a set of (path, branch) worktrees."""
    blocks = []
    for path, branch in entries:
        blocks.append(f"worktree {path}\nHEAD deadbeef\nbranch refs/heads/{branch}\n")
    return "\n".join(blocks)


def test_scan_worktrees_only_considers_the_managed_worktrees_dir():
    cfg = load_config()
    porcelain = (
        "worktree /repo\nHEAD deadbeef\nbranch refs/heads/main\n"
        "\n"
        f"worktree /repo/.hsai/worktrees/hsai/iter-1-1-abc\nHEAD cafe\n"
        f"branch refs/heads/hsai/iter-1-1-abc\n"
    )
    runner = _FakeRunner(porcelain=porcelain)
    verdicts = janitor.scan_worktrees(cfg, now=1.0, ttl_seconds=4500, repo_dir="/repo", runner=runner)
    # Only the entry under .hsai/worktrees is considered - never the main checkout.
    assert len(verdicts) == 1
    assert verdicts[0].entry.path == "/repo/.hsai/worktrees/hsai/iter-1-1-abc"


def test_scan_worktrees_classifies_orphaned_when_old_no_pr_no_commits():
    cfg = load_config()
    branch = "hsai/iter-1-1-abc"
    porcelain = _porcelain((f"/repo/.hsai/worktrees/{branch}", branch))
    runner = _FakeRunner(porcelain=porcelain, rev_counts={branch: 0})
    verdicts = janitor.scan_worktrees(
        cfg, now=1.0 + 99999, ttl_seconds=4500, repo_dir="/repo", runner=runner
    )
    assert len(verdicts) == 1
    assert verdicts[0].status == janitor.ORPHANED


def test_scan_worktrees_classifies_active_when_branch_has_an_open_pr():
    cfg = load_config()
    branch = "hsai/iter-1-1-abc"
    porcelain = _porcelain((f"/repo/.hsai/worktrees/{branch}", branch))
    runner = _FakeRunner(
        porcelain=porcelain, rev_counts={branch: 2},
        prs=[{"number": 5, "head_ref": branch}],
    )
    verdicts = janitor.scan_worktrees(
        cfg, now=1.0 + 99999, ttl_seconds=4500, repo_dir="/repo", runner=runner
    )
    assert verdicts[0].status == janitor.ACTIVE


def test_scan_claims_classifies_fresh_stranded_and_human():
    cfg = load_config()
    issues = [
        {"number": 1, "title": "feat: a", "labels": [], "body": "",
         "assignees": [{"login": "hsai-bot"}], "updatedAt": "2026-08-18T00:00:00Z"},
        {"number": 2, "title": "feat: b", "labels": [], "body": "",
         "assignees": [{"login": "hsai-bot"}], "updatedAt": "2020-01-01T00:00:00Z"},
        {"number": 3, "title": "feat: c", "labels": [], "body": "",
         "assignees": [{"login": "a-human"}], "updatedAt": "2020-01-01T00:00:00Z"},
    ]
    runner = _FakeRunner(issues=issues, login="hsai-bot")
    verdicts = janitor.scan_claims(cfg, now=NOW, ttl_seconds=100, loop_login="hsai-bot", runner=runner)
    by_number = {v.issue.number: v.status for v in verdicts}
    assert by_number[1] == janitor.FRESH
    assert by_number[2] == janitor.STRANDED
    assert by_number[3] == janitor.HUMAN


def test_scan_claims_ignores_unassigned_issues():
    cfg = load_config()
    issues = [
        {"number": 1, "title": "feat: a", "labels": [], "body": "", "assignees": [],
         "updatedAt": "2020-01-01T00:00:00Z"},
    ]
    runner = _FakeRunner(issues=issues)
    verdicts = janitor.scan_claims(cfg, now=NOW, ttl_seconds=100, loop_login="hsai-bot", runner=runner)
    assert verdicts == []


def test_scan_claims_treats_a_ticket_referenced_by_an_open_pr_as_fresh():
    cfg = load_config()
    issues = [
        {"number": 1, "title": "feat: a", "labels": [], "body": "",
         "assignees": [{"login": "hsai-bot"}], "updatedAt": "2020-01-01T00:00:00Z"},
    ]
    prs = [{"number": 50, "head_ref": "hsai/iter-1-1-abc", "body": "Closes #1"}]
    runner = _FakeRunner(issues=issues, prs=prs)
    verdicts = janitor.scan_claims(cfg, now=NOW, ttl_seconds=100, loop_login="hsai-bot", runner=runner)
    assert verdicts[0].status == janitor.FRESH


# --- build_plan + reap: dry-run is genuinely inert ---------------------------

DESTRUCTIVE_PREFIXES = [
    ["git", "worktree", "remove"],
    ["git", "worktree", "prune"],
    ["git", "push", "origin", "--delete"],
    ["gh", "issue", "edit"],
]


def _is_destructive(cmd: list[str]) -> bool:
    return any(cmd[: len(p)] == p for p in DESTRUCTIVE_PREFIXES)


def test_dry_run_plan_performs_zero_destructive_commands():
    cfg = load_config()
    branch = "hsai/iter-1-1-abc"
    porcelain = _porcelain((f"/repo/.hsai/worktrees/{branch}", branch))
    issues = [
        {"number": 9, "title": "feat: x", "labels": [], "body": "",
         "assignees": [{"login": "hsai-bot"}], "updatedAt": "2020-01-01T00:00:00Z"},
    ]
    runner = _FakeRunner(porcelain=porcelain, rev_counts={branch: 0}, issues=issues)

    plan = janitor.build_plan(cfg, now=NOW, repo_dir="/repo", runner=runner)
    # Everything found is genuinely reclaimable in this fixture...
    assert plan.has_decision()
    # ...but build_plan (used for --dry-run) never calls reap, so nothing
    # destructive was ever issued.
    assert not any(_is_destructive(c) for c in runner.calls)
    assert plan.removed_worktrees == [] and plan.returned_tickets == []


def test_reap_under_dry_run_flag_is_a_true_no_op():
    cfg = load_config()
    branch = "hsai/iter-1-1-abc"
    porcelain = _porcelain((f"/repo/.hsai/worktrees/{branch}", branch))
    runner = _FakeRunner(porcelain=porcelain, rev_counts={branch: 0})
    plan = janitor.build_plan(cfg, now=NOW, repo_dir="/repo", runner=runner)
    calls_before = len(runner.calls)

    result = janitor.reap(cfg, plan, repo_dir="/repo", dry_run=True, runner=runner)

    assert result is plan
    assert len(runner.calls) == calls_before          # not one more call was made
    assert plan.removed_worktrees == []


# --- reap: real reclamation ----------------------------------------------------

def test_reap_removes_an_orphaned_worktree_and_deletes_its_pushed_branch():
    cfg = load_config()
    branch = "hsai/iter-1-1-abc"
    porcelain = _porcelain((f"/repo/.hsai/worktrees/{branch}", branch))
    runner = _FakeRunner(porcelain=porcelain, rev_counts={branch: 0})
    plan = janitor.build_plan(cfg, now=NOW, repo_dir="/repo", runner=runner)

    janitor.reap(cfg, plan, repo_dir="/repo", dry_run=False, runner=runner)

    assert plan.removed_worktrees == [f"/repo/.hsai/worktrees/{branch}"]
    assert plan.deleted_branches == [branch]
    assert runner.calls_like("git", "worktree", "remove")
    assert runner.calls_like("git", "worktree", "prune")


def test_reap_removes_the_worktree_even_when_the_branch_was_never_pushed():
    cfg = load_config()
    branch = "hsai/iter-1-1-abc"
    porcelain = _porcelain((f"/repo/.hsai/worktrees/{branch}", branch))
    runner = _FakeRunner(
        porcelain=porcelain, rev_counts={branch: 0}, push_delete_fail={branch}
    )
    plan = janitor.build_plan(cfg, now=NOW, repo_dir="/repo", runner=runner)

    janitor.reap(cfg, plan, repo_dir="/repo", dry_run=False, runner=runner)

    assert plan.removed_worktrees == [f"/repo/.hsai/worktrees/{branch}"]
    assert plan.deleted_branches == []      # push --delete failed -> not counted


def test_reap_never_removes_a_worktree_with_an_open_pr():
    cfg = load_config()
    branch = "hsai/iter-1-1-abc"
    porcelain = _porcelain((f"/repo/.hsai/worktrees/{branch}", branch))
    runner = _FakeRunner(
        porcelain=porcelain, rev_counts={branch: 3}, prs=[{"number": 5, "head_ref": branch}],
    )
    plan = janitor.build_plan(cfg, now=NOW, repo_dir="/repo", runner=runner)
    assert plan.worktrees[0].status == janitor.ACTIVE

    janitor.reap(cfg, plan, repo_dir="/repo", dry_run=False, runner=runner)

    assert not runner.calls_like("git", "worktree", "remove")
    assert plan.removed_worktrees == []


def test_reap_returns_a_stranded_ticket_to_the_backlog_with_attempts_incremented():
    cfg = load_config()
    issues = [
        {"number": 9, "title": "feat: x", "labels": [], "body": "",
         "assignees": [{"login": "hsai-bot"}], "updatedAt": "2020-01-01T00:00:00Z"},
    ]
    runner = _FakeRunner(issues=issues)
    plan = janitor.build_plan(cfg, now=NOW, repo_dir="/repo", runner=runner)
    assert plan.stranded_claims and plan.stranded_claims[0].issue.number == 9

    janitor.reap(cfg, plan, repo_dir="/repo", dry_run=False, runner=runner)

    assert plan.returned_tickets == [9]
    assert plan.blocked_tickets == []
    edits = runner.calls_like("gh", "issue", "edit")
    assert any("attempts:1" in c for c in edits)
    assert any("--remove-assignee" in c for c in edits)


def test_reap_labels_blocked_instead_of_reopening_when_attempts_are_exhausted():
    cfg = load_config()  # max_ticket_attempts=2
    issues = [
        {"number": 9, "title": "feat: x", "labels": [{"name": "attempts:1"}], "body": "",
         "assignees": [{"login": "hsai-bot"}], "updatedAt": "2020-01-01T00:00:00Z"},
    ]
    runner = _FakeRunner(issues=issues)
    plan = janitor.build_plan(cfg, now=NOW, repo_dir="/repo", runner=runner)

    janitor.reap(cfg, plan, repo_dir="/repo", dry_run=False, runner=runner)

    assert plan.returned_tickets == []
    assert plan.blocked_tickets == [9]
    edits = runner.calls_like("gh", "issue", "edit")
    assert any("blocked" in c for c in edits)
    assert any("--remove-label" in c and "attempts:1" in c for c in edits)


def test_reap_never_touches_a_human_assigned_claim():
    cfg = load_config()
    issues = [
        {"number": 9, "title": "feat: x", "labels": [], "body": "",
         "assignees": [{"login": "a-human"}], "updatedAt": "2020-01-01T00:00:00Z"},
    ]
    runner = _FakeRunner(issues=issues)
    plan = janitor.build_plan(cfg, now=NOW, repo_dir="/repo", runner=runner)
    assert plan.human_claims and not plan.stranded_claims

    janitor.reap(cfg, plan, repo_dir="/repo", dry_run=False, runner=runner)

    assert not runner.calls_like("gh", "issue", "edit")
    assert plan.returned_tickets == [] and plan.blocked_tickets == []


# --- plan rendering + decision gate -------------------------------------------

def test_plan_render_lists_reclaimable_and_ambiguous_entries():
    cfg = load_config()
    issues = [
        {"number": 9, "title": "feat: x", "labels": [], "body": "",
         "assignees": [{"login": "hsai-bot"}], "updatedAt": "not-a-timestamp"},
    ]
    runner = _FakeRunner(issues=issues)
    plan = janitor.build_plan(cfg, now=NOW, repo_dir="/repo", runner=runner)
    text = plan.render()
    assert "AMBIGUOUS ticket #9" in text
    assert "claims: 1 scanned" in text


def test_plan_has_no_decision_and_no_ambiguous_when_repo_is_clean():
    cfg = load_config()
    runner = _FakeRunner()
    plan = janitor.build_plan(cfg, now=NOW, repo_dir="/repo", runner=runner)
    assert not plan.has_decision()
    assert not plan.has_ambiguous()


def test_plan_has_ambiguous_but_no_decision_when_everything_is_uncertain():
    cfg = load_config()
    branch = "hsai/iter-1-1-abc"
    porcelain = _porcelain((f"/repo/.hsai/worktrees/{branch}", branch))
    runner = _FakeRunner(porcelain=porcelain, rev_counts={branch: 4})  # ahead, no PR
    plan = janitor.build_plan(cfg, now=NOW, repo_dir="/repo", runner=runner)
    assert not plan.has_decision()
    assert plan.has_ambiguous()
