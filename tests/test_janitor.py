"""Loop janitor: classification (pure), scan wrappers, and reap (side effects)."""
from __future__ import annotations

import json

from hsai import github, janitor
from hsai.config import load_config
from hsai.proc import Proc

# --- parse_worktree_list -------------------------------------------------------

PORCELAIN = """worktree /repo
HEAD 0100f4bc01c8c72968a1723430b4d5e8f2cbcbca
branch refs/heads/main

worktree /repo/.hsai/worktrees/hsai/iter-orphaned
HEAD 22a78a75afdb25f52e9b89e9b6410824277b38e5
branch refs/heads/hsai/iter-orphaned

worktree /repo/.hsai/worktrees/hsai/iter-locked
HEAD 93729d4d963c37eb70d52aa05d58b102eb52109b
branch refs/heads/hsai/iter-locked
locked machine is still working

worktree /repo/.hsai/worktrees/hsai/iter-gone
HEAD b1a0ac8a983b91797d4a9ffb20cc5571cd176ba8
branch refs/heads/hsai/iter-gone
prunable gitdir file points to non-existent location

worktree /repo/.hsai/worktrees/hsai/repro-check-abcd1234
HEAD deadbeefdeadbeefdeadbeefdeadbeefdeadbeef
detached
"""


def test_parse_worktree_list_reads_every_block():
    entries = janitor.parse_worktree_list(PORCELAIN)
    assert [e.path for e in entries] == [
        "/repo",
        "/repo/.hsai/worktrees/hsai/iter-orphaned",
        "/repo/.hsai/worktrees/hsai/iter-locked",
        "/repo/.hsai/worktrees/hsai/iter-gone",
        "/repo/.hsai/worktrees/hsai/repro-check-abcd1234",
    ]
    by_path = {e.path: e for e in entries}
    assert by_path["/repo"].branch == "main"
    assert by_path["/repo/.hsai/worktrees/hsai/iter-locked"].locked is True
    assert by_path["/repo/.hsai/worktrees/hsai/iter-gone"].prunable is True
    assert by_path["/repo/.hsai/worktrees/hsai/repro-check-abcd1234"].branch == ""


def test_parse_worktree_list_empty_output():
    assert janitor.parse_worktree_list("") == []
    assert janitor.parse_worktree_list("\n\n") == []


# --- classify_worktree: one branch per outcome --------------------------------

def _entry(**kw):
    return janitor.WorktreeEntry(path="/repo/.hsai/worktrees/hsai/x", branch="hsai/iter-x", **kw)


def test_classify_worktree_locked_is_active_regardless_of_everything_else():
    entry = _entry(locked=True)
    c = janitor.classify_worktree(entry, open_pr_branches=frozenset(), ahead_of_main=5)
    assert c.status == janitor.ACTIVE and "locked" in c.reason


def test_classify_worktree_open_pr_is_active():
    entry = _entry()
    c = janitor.classify_worktree(
        entry, open_pr_branches=frozenset({"hsai/iter-x"}), ahead_of_main=3
    )
    assert c.status == janitor.ACTIVE and "open PR" in c.reason


def test_classify_worktree_detached_is_ambiguous():
    entry = janitor.WorktreeEntry(path="/repo/.hsai/worktrees/hsai/x", branch="")
    c = janitor.classify_worktree(entry, open_pr_branches=frozenset(), ahead_of_main=None)
    assert c.status == janitor.AMBIGUOUS and "detached" in c.reason


def test_classify_worktree_undetermined_ahead_is_ambiguous():
    entry = _entry()
    c = janitor.classify_worktree(entry, open_pr_branches=frozenset(), ahead_of_main=None)
    assert c.status == janitor.AMBIGUOUS and "could not determine" in c.reason


def test_classify_worktree_unpushed_commits_no_pr_is_ambiguous():
    entry = _entry()
    c = janitor.classify_worktree(entry, open_pr_branches=frozenset(), ahead_of_main=2)
    assert c.status == janitor.AMBIGUOUS and "2 commit(s) ahead" in c.reason


def test_classify_worktree_no_pr_and_zero_ahead_is_orphaned():
    entry = _entry()
    c = janitor.classify_worktree(entry, open_pr_branches=frozenset(), ahead_of_main=0)
    assert c.status == janitor.ORPHANED and "no commits ahead" in c.reason


# --- scan_worktrees: end to end with a fake git runner -------------------------

class _GitRunner:
    """Answers `git worktree list --porcelain` and per-branch `rev-list --count`."""

    def __init__(self, *, porcelain: str, ahead: dict[str, str] | None = None):
        self.porcelain = porcelain
        self.ahead = ahead or {}
        self.calls: list[list[str]] = []

    def __call__(self, cmd, *, cwd=None, env=None, env_remove=None, timeout=None, input_text=None):
        cmd = list(cmd)
        self.calls.append(cmd)
        if cmd[:3] == ["git", "worktree", "list"]:
            return Proc(cmd, 0, self.porcelain, "")
        if cmd[:2] == ["git", "rev-list"]:
            spec = cmd[-1]  # "origin/main..<branch>"
            branch = spec.split("..", 1)[1]
            return Proc(cmd, 0, self.ahead.get(branch, "0") + "\n", "")
        if cmd[:3] == ["gh", "pr", "list"]:
            return Proc(cmd, 0, "[]", "")
        return Proc(cmd, 0, "", "")


_AHEAD = {"hsai/iter-orphaned": "0", "hsai/iter-locked": "0", "hsai/iter-gone": "3"}


def test_scan_worktrees_only_considers_entries_under_worktrees_dir():
    cfg = load_config()
    runner = _GitRunner(porcelain=PORCELAIN, ahead=_AHEAD)
    results = janitor.scan_worktrees(cfg, repo_root="/repo", runner=runner)
    paths = [c.entry.path for c in results]
    assert "/repo" not in paths  # the main checkout is never even considered
    assert len(paths) == 4


def test_scan_worktrees_classifies_the_full_mix():
    cfg = load_config()
    runner = _GitRunner(porcelain=PORCELAIN, ahead=_AHEAD)
    by_path = {
        c.entry.path: c for c in janitor.scan_worktrees(cfg, repo_root="/repo", runner=runner)
    }
    assert by_path["/repo/.hsai/worktrees/hsai/iter-orphaned"].status == janitor.ORPHANED
    assert by_path["/repo/.hsai/worktrees/hsai/iter-locked"].status == janitor.ACTIVE
    # 3 commits ahead of origin/main with no open PR: unpushed work, never auto-reaped.
    assert by_path["/repo/.hsai/worktrees/hsai/iter-gone"].status == janitor.AMBIGUOUS
    # detached HEAD (no branch): can never be proven safe either way.
    assert by_path["/repo/.hsai/worktrees/hsai/repro-check-abcd1234"].status == janitor.AMBIGUOUS


def test_scan_worktrees_open_pr_branch_makes_it_active():
    cfg = load_config()

    class _WithPr(_GitRunner):
        def __call__(self, cmd, **kw):
            cmd = list(cmd)
            if cmd[:3] == ["gh", "pr", "list"]:
                return Proc(cmd, 0, json.dumps([
                    {"number": 5, "title": "t", "body": "", "headRefName": "hsai/iter-orphaned"}
                ]), "")
            return super().__call__(cmd, **kw)

    runner = _WithPr(porcelain=PORCELAIN)
    by_path = {
        c.entry.path: c for c in janitor.scan_worktrees(cfg, repo_root="/repo", runner=runner)
    }
    assert by_path["/repo/.hsai/worktrees/hsai/iter-orphaned"].status == janitor.ACTIVE


# --- derive_ttl_seconds: config-driven -----------------------------------------

def test_derive_ttl_seconds_uses_config_timeouts_and_multiplier():
    cfg = load_config()
    cfg.janitor.clear()
    cfg.janitor.update({"safety_multiplier": 2.0})
    ttl = janitor.derive_ttl_seconds(cfg)
    assert ttl == (float(cfg.agent_timeout) + cfg.ci_remote_timeout) * 2.0


def test_derive_ttl_seconds_default_multiplier_is_three():
    cfg = load_config()
    cfg.janitor.clear()
    ttl = janitor.derive_ttl_seconds(cfg)
    assert ttl == (float(cfg.agent_timeout) + cfg.ci_remote_timeout) * 3.0


def test_core_yaml_configures_janitor_safety_multiplier():
    cfg = load_config()
    assert cfg.janitor.get("safety_multiplier", janitor.DEFAULT_SAFETY_MULTIPLIER) > 0


# --- classify_claim: fresh / stranded / human, and the never-touch-human rule --

def _issue(number=1, assignees=(), updated_at="", labels=()):
    return github.Issue(
        number=number, title=f"feat: thing {number}", labels=tuple(labels),
        assignees=tuple(assignees), body="", updated_at=updated_at,
    )


def test_classify_claim_unassigned_is_not_a_claim():
    assert janitor.classify_claim(
        _issue(assignees=()), now=1000.0, ttl_seconds=100.0,
        referenced=frozenset(), login="hsai-bot",
    ) is None


def test_classify_claim_fresh_within_ttl():
    issue = _issue(assignees=["hsai-bot"], updated_at="1970-01-01T00:16:00+00:00")  # t=960
    c = janitor.classify_claim(
        issue, now=1000.0, ttl_seconds=100.0, referenced=frozenset(), login="hsai-bot",
    )
    assert c.status == janitor.FRESH and "within" in c.reason


def test_classify_claim_fresh_when_an_open_pr_references_it_even_past_ttl():
    issue = _issue(number=7, assignees=["hsai-bot"], updated_at="1970-01-01T00:00:00+00:00")
    c = janitor.classify_claim(
        issue, now=100000.0, ttl_seconds=100.0, referenced=frozenset({7}), login="hsai-bot",
    )
    assert c.status == janitor.FRESH and "open PR" in c.reason


def test_classify_claim_stranded_past_ttl_no_pr_loop_owned():
    issue = _issue(assignees=["hsai-bot"], updated_at="1970-01-01T00:00:00+00:00")
    c = janitor.classify_claim(
        issue, now=100000.0, ttl_seconds=100.0, referenced=frozenset(), login="hsai-bot",
    )
    assert c.status == janitor.STRANDED and "past the" in c.reason


def test_classify_claim_unknown_age_is_fresh_never_a_stale_guess():
    issue = _issue(assignees=["hsai-bot"], updated_at="")
    c = janitor.classify_claim(
        issue, now=100000.0, ttl_seconds=100.0, referenced=frozenset(), login="hsai-bot",
    )
    assert c.status == janitor.FRESH and "unknown" in c.reason


def test_classify_claim_human_assignee_is_never_touched_even_past_ttl():
    issue = _issue(assignees=["a-human"], updated_at="1970-01-01T00:00:00+00:00")
    c = janitor.classify_claim(
        issue, now=100000.0, ttl_seconds=100.0, referenced=frozenset(), login="hsai-bot",
    )
    assert c.status == janitor.HUMAN and "a-human" in c.reason


def test_classify_claim_mixed_assignees_is_human_even_with_the_loop_login_present():
    """The loop's own login plus ANY other assignee => human. Never unassign a human."""
    issue = _issue(assignees=["hsai-bot", "a-human"], updated_at="1970-01-01T00:00:00+00:00")
    c = janitor.classify_claim(
        issue, now=100000.0, ttl_seconds=100.0, referenced=frozenset(), login="hsai-bot",
    )
    assert c.status == janitor.HUMAN


# --- scan_claims wrapper --------------------------------------------------------

class _GhIssueRunner:
    def __init__(self, *, issues, prs=None):
        self.issues = issues
        self.prs = prs or []
        self.calls: list[list[str]] = []

    def __call__(self, cmd, *, cwd=None, env=None, env_remove=None, timeout=None, input_text=None):
        cmd = list(cmd)
        self.calls.append(cmd)
        if cmd[:3] == ["gh", "issue", "list"]:
            return Proc(cmd, 0, json.dumps(self.issues), "")
        if cmd[:3] == ["gh", "pr", "list"]:
            return Proc(cmd, 0, json.dumps(self.prs), "")
        return Proc(cmd, 0, "", "")


def test_scan_claims_skips_unassigned_and_classifies_the_rest():
    cfg = load_config()
    runner = _GhIssueRunner(issues=[
        {"number": 1, "title": "feat: a", "labels": [], "assignees": [], "body": "",
         "updatedAt": ""},
        {"number": 2, "title": "feat: b", "labels": [], "assignees": [{"login": "hsai-bot"}],
         "body": "", "updatedAt": "1970-01-01T00:00:00+00:00"},
    ])
    results = janitor.scan_claims(cfg, login="hsai-bot", now=100000.0, ttl_seconds=10.0, runner=runner)
    assert [c.issue.number for c in results] == [2]
    assert results[0].status == janitor.STRANDED


# --- ReclaimPlan: actionable vs blocked -----------------------------------------

def _wc(status, path="/repo/.hsai/worktrees/hsai/x", branch="hsai/iter-x"):
    return janitor.WorktreeClassification(
        janitor.WorktreeEntry(path=path, branch=branch), status, "reason"
    )


def _cc(status, number=1):
    return janitor.ClaimClassification(_issue(number=number), status, "reason")


def test_plan_is_neither_actionable_nor_blocked_when_empty():
    plan = janitor.ReclaimPlan()
    assert not plan.actionable and not plan.blocked


def test_plan_actionable_when_something_orphaned_or_stranded():
    assert janitor.ReclaimPlan(worktrees=[_wc(janitor.ORPHANED)]).actionable
    assert janitor.ReclaimPlan(claims=[_cc(janitor.STRANDED)]).actionable


def test_plan_blocked_when_only_ambiguous_debris_exists():
    plan = janitor.ReclaimPlan(worktrees=[_wc(janitor.AMBIGUOUS)])
    assert plan.blocked and not plan.actionable


def test_plan_not_blocked_when_ambiguous_debris_coexists_with_actionable_items():
    plan = janitor.ReclaimPlan(
        worktrees=[_wc(janitor.AMBIGUOUS), _wc(janitor.ORPHANED, path="/repo/.hsai/worktrees/hsai/y", branch="hsai/iter-y")],
    )
    assert plan.actionable and not plan.blocked


def test_exit_code_matches_blocked():
    assert janitor.exit_code(janitor.ReclaimPlan(worktrees=[_wc(janitor.AMBIGUOUS)])) == 1
    assert janitor.exit_code(janitor.ReclaimPlan()) == 0
    assert janitor.exit_code(janitor.ReclaimPlan(worktrees=[_wc(janitor.ORPHANED)])) == 0


def test_render_plan_lists_reap_and_skip_lines():
    plan = janitor.ReclaimPlan(
        worktrees=[_wc(janitor.ORPHANED), _wc(janitor.AMBIGUOUS, path="/repo/.hsai/worktrees/hsai/y")],
        claims=[_cc(janitor.STRANDED, number=9), _cc(janitor.HUMAN, number=10)],
    )
    text = janitor.render_plan(plan)
    assert "[reap] /repo/.hsai/worktrees/hsai/x" in text
    assert "[skip: needs a human] /repo/.hsai/worktrees/hsai/y" in text
    assert "[reap] #9" in text
    assert "[skip: human-assigned] #10" in text


def test_render_plan_says_nothing_reclaimable_when_not_actionable():
    assert "nothing safely reclaimable" in janitor.render_plan(janitor.ReclaimPlan())


# --- reap(): only orphaned/stranded ever produce a destructive command ---------

class _ReapRunner:
    """Records every command; flags anything destructive for the dry-run guard."""

    DESTRUCTIVE_PREFIXES = (
        ["git", "worktree", "remove"], ["git", "worktree", "prune"],
        ["git", "push", "origin", "--delete"], ["gh", "issue", "edit"],
    )

    def __init__(self):
        self.calls: list[list[str]] = []

    def __call__(self, cmd, *, cwd=None, env=None, env_remove=None, timeout=None, input_text=None):
        cmd = list(cmd)
        self.calls.append(cmd)
        return Proc(cmd, 0, "", "")

    def destructive_calls(self):
        return [
            c for c in self.calls
            if any(c[: len(p)] == p for p in self.DESTRUCTIVE_PREFIXES)
        ]


def test_reap_removes_orphaned_worktree_deletes_its_branch_and_prunes():
    cfg = load_config()
    runner = _ReapRunner()
    plan = janitor.ReclaimPlan(worktrees=[_wc(janitor.ORPHANED)])

    report = janitor.reap(cfg, plan, repo_root="/repo", login="hsai-bot", runner=runner)

    assert report.worktrees_removed == ["/repo/.hsai/worktrees/hsai/x"]
    assert report.branches_deleted == ["hsai/iter-x"]
    assert ["git", "worktree", "remove", "--force", "/repo/.hsai/worktrees/hsai/x"] in runner.calls
    assert ["git", "push", "origin", "--delete", "hsai/iter-x"] in runner.calls
    assert ["git", "worktree", "prune"] in runner.calls


def test_reap_never_touches_active_ambiguous_fresh_or_human_entries():
    cfg = load_config()
    runner = _ReapRunner()
    plan = janitor.ReclaimPlan(
        worktrees=[_wc(janitor.ACTIVE), _wc(janitor.AMBIGUOUS, path="/repo/.hsai/worktrees/hsai/y")],
        claims=[_cc(janitor.FRESH, number=1), _cc(janitor.HUMAN, number=2)],
    )

    report = janitor.reap(cfg, plan, repo_root="/repo", login="hsai-bot", runner=runner)

    assert report.is_empty
    assert runner.destructive_calls() == []


def test_reap_reopens_a_stranded_claim_below_max_attempts():
    cfg = load_config()  # core.yaml sets max_ticket_attempts=2
    runner = _ReapRunner()
    issue = github.Issue(number=42, title="feat: x", labels=(), assignees=("hsai-bot",), body="")
    plan = janitor.ReclaimPlan(claims=[janitor.ClaimClassification(issue, janitor.STRANDED, "r")])

    report = janitor.reap(cfg, plan, repo_root="/repo", login="hsai-bot", runner=runner)

    assert report.tickets_reopened == [42]
    assert report.tickets_blocked == []
    edit = next(c for c in runner.calls if c[:3] == ["gh", "issue", "edit"])
    assert "--add-label" in edit and "attempts:1" in edit
    assert ["gh", "issue", "edit", "42", "--repo", cfg.repo_slug,
            "--remove-assignee", "hsai-bot"] in runner.calls


def test_reap_blocks_a_stranded_claim_at_max_attempts_instead_of_reopening():
    cfg = load_config()  # core.yaml sets max_ticket_attempts=2
    runner = _ReapRunner()
    issue = github.Issue(
        number=42, title="feat: x", labels=("attempts:1",), assignees=("hsai-bot",), body="",
    )
    plan = janitor.ReclaimPlan(claims=[janitor.ClaimClassification(issue, janitor.STRANDED, "r")])

    report = janitor.reap(cfg, plan, repo_root="/repo", login="hsai-bot", runner=runner)

    assert report.tickets_blocked == [42]
    assert report.tickets_reopened == []
    edit = next(c for c in runner.calls if c[:3] == ["gh", "issue", "edit"])
    assert "--add-label" in edit and "blocked" in edit
    assert "--remove-label" in edit and "attempts:1" in edit


def test_render_reclaimed_summarizes_every_kind_of_recovery():
    report = janitor.ReclaimReport(
        worktrees_removed=["/repo/.hsai/worktrees/hsai/x"],
        branches_deleted=["hsai/iter-x"],
        tickets_reopened=[42],
        tickets_blocked=[43],
    )
    text = janitor.render_reclaimed(report)
    assert "/repo/.hsai/worktrees/hsai/x" in text
    assert "hsai/iter-x" in text
    assert "#42" in text and "#43" in text


def test_render_reclaimed_empty_report():
    assert "_none this block_" in janitor.render_reclaimed(janitor.ReclaimReport())


# --- run_janitor: dry-run performs zero side effects ----------------------------

FULL_PORCELAIN = """worktree /repo
HEAD 0100f4bc01c8c72968a1723430b4d5e8f2cbcbca
branch refs/heads/main

worktree /repo/.hsai/worktrees/hsai/iter-orphaned
HEAD 22a78a75afdb25f52e9b89e9b6410824277b38e5
branch refs/heads/hsai/iter-orphaned
"""


class _FullRunner:
    """A realistic combined git/gh fake: one orphaned worktree, one stranded claim."""

    DESTRUCTIVE_PREFIXES = (
        ["git", "worktree", "remove"], ["git", "worktree", "prune"],
        ["git", "push", "origin", "--delete"], ["gh", "issue", "edit"],
    )

    def __init__(self):
        self.calls: list[list[str]] = []

    def __call__(self, cmd, *, cwd=None, env=None, env_remove=None, timeout=None, input_text=None):
        cmd = list(cmd)
        self.calls.append(cmd)
        if cmd[:3] == ["git", "worktree", "list"]:
            return Proc(cmd, 0, FULL_PORCELAIN, "")
        if cmd[:2] == ["git", "rev-list"]:
            return Proc(cmd, 0, "0\n", "")
        if cmd[:3] == ["gh", "pr", "list"]:
            return Proc(cmd, 0, "[]", "")
        if cmd[:3] == ["gh", "issue", "list"]:
            return Proc(cmd, 0, json.dumps([
                {"number": 42, "title": "feat: x", "labels": [],
                 "assignees": [{"login": "hsai-bot"}], "body": "",
                 "updatedAt": "1970-01-01T00:00:00+00:00"},
            ]), "")
        if cmd[:3] == ["gh", "api", "user"]:
            return Proc(cmd, 0, "hsai-bot\n", "")
        return Proc(cmd, 0, "", "")

    def destructive_calls(self):
        return [
            c for c in self.calls
            if any(c[: len(p)] == p for p in self.DESTRUCTIVE_PREFIXES)
        ]


def test_dry_run_finds_the_same_plan_but_issues_zero_destructive_commands():
    cfg = load_config()
    runner = _FullRunner()

    result = janitor.run_janitor(
        cfg, repo_root="/repo", dry_run=True, ttl_seconds=10.0, now=100000.0, runner=runner,
    )

    assert result.dry_run is True
    assert result.plan.orphaned_worktrees, "the plan should still find the orphaned worktree"
    assert result.plan.stranded_claims, "the plan should still find the stranded claim"
    assert result.report.is_empty, "dry-run must report zero reclaims"
    assert runner.destructive_calls() == []


def test_live_run_actually_reaps_what_the_plan_found():
    cfg = load_config()
    runner = _FullRunner()

    result = janitor.run_janitor(
        cfg, repo_root="/repo", dry_run=False, ttl_seconds=10.0, now=100000.0, runner=runner,
    )

    assert result.dry_run is False
    assert result.report.worktrees_removed == ["/repo/.hsai/worktrees/hsai/iter-orphaned"]
    assert result.report.branches_deleted == ["hsai/iter-orphaned"]
    assert result.report.tickets_reopened == [42]  # core.yaml's max_ticket_attempts=2, prior=0
    assert result.report.tickets_blocked == []
    assert runner.destructive_calls(), "a live run must issue at least one real change"


# --- hsai doctor's health signal -------------------------------------------------

def test_health_counts_reflects_orphaned_and_stranded_totals():
    cfg = load_config()
    runner = _FullRunner()
    health = janitor.health_counts(cfg, repo_root="/repo", runner=runner)
    assert health.orphaned_worktrees == 1
    assert health.stranded_claims == 1
    assert "orphaned_worktrees=1" in health.summary()
