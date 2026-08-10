import json

from hsai import gc
from hsai.config import load_config
from hsai.proc import Proc

NOW = 1_000_000.0
STALE_TS = int(NOW - 20 * 3600)   # 20h old
FRESH_TS = int(NOW - 1 * 3600)    # 1h old

STALE_BRANCH = f"hsai/iter-{STALE_TS}-1-aaaaaa"
FRESH_BRANCH = f"hsai/iter-{FRESH_TS}-2-bbbbbb"

WORKTREE_LIST = f"""worktree /repo
HEAD 0100f4b0000000000000000000000000000000
branch refs/heads/main

worktree /repo/.hsai/worktrees/hsai/iter-{STALE_TS}-1-aaaaaa
HEAD deadbeefdeadbeefdeadbeefdeadbeefdeadbeef
branch refs/heads/{STALE_BRANCH}

worktree /repo/.hsai/worktrees/hsai/iter-{FRESH_TS}-2-bbbbbb
HEAD cafebabecafebabecafebabecafebabecafebabe
branch refs/heads/{FRESH_BRANCH}

worktree /repo/.hsai/worktrees/repro-check-ffffffff
HEAD 1111111111111111111111111111111111111111
detached
"""


class _FakeRunner:
    """Answers `git worktree list`, `git merge-base --is-ancestor`, `gh pr
    list`, and the removal commands `run_gc` issues, recording every call."""

    def __init__(self, *, merged_branches=(), open_pr_branches=()):
        self.merged_branches = set(merged_branches)
        self.open_pr_branches = set(open_pr_branches)
        self.calls: list[list[str]] = []

    def __call__(self, cmd, *, cwd=None, env=None, timeout=None, input_text=None) -> Proc:
        cmd = list(cmd)
        self.calls.append(cmd)
        if cmd[:3] == ["git", "worktree", "list"]:
            return Proc(cmd, 0, WORKTREE_LIST, "")
        if cmd[:3] == ["git", "worktree", "remove"]:
            return Proc(cmd, 0, "", "")
        if cmd[:2] == ["git", "merge-base"]:
            branch = cmd[3]
            ok = branch in self.merged_branches
            return Proc(cmd, 0 if ok else 1, "", "")
        if cmd[:2] == ["gh", "pr"] and cmd[2] == "list":
            branch = cmd[cmd.index("--head") + 1]
            data = [{"number": 1}] if branch in self.open_pr_branches else []
            return Proc(cmd, 0, json.dumps(data), "")
        if cmd[:2] == ["git", "branch"]:
            return Proc(cmd, 0, "", "")
        raise AssertionError(f"_FakeRunner: unhandled command {cmd!r}")


def test_discover_stale_skips_the_main_checkout_and_fresh_worktrees():
    runner = _FakeRunner()
    stale = gc.discover_stale(
        repo_root="/repo", worktrees_dir=".hsai/worktrees",
        stale_hours=12, runner=runner, now=NOW,
    )
    branches = {e.branch for e in stale}
    assert STALE_BRANCH in branches
    assert FRESH_BRANCH not in branches
    assert "main" not in branches
    # the detached repro-check leftover has no parseable timestamp -> stale
    assert any(e.branch == "" for e in stale)


def test_gc_dry_run_reports_but_changes_nothing():
    cfg = load_config()
    runner = _FakeRunner(merged_branches={STALE_BRANCH})
    res = gc.run_gc(cfg, repo_root="/repo", stale_hours=12, dry_run=True, runner=runner, now=NOW)

    assert res.dry_run is True
    assert any(STALE_BRANCH in p for p in res.removed_worktrees)
    assert STALE_BRANCH in res.removed_branches
    # nothing was actually removed
    assert not any(c[:3] == ["git", "worktree", "remove"] for c in runner.calls)
    assert not any(c[:2] == ["git", "branch"] for c in runner.calls)


def test_gc_live_removes_worktree_and_merged_branch():
    cfg = load_config()
    runner = _FakeRunner(merged_branches={STALE_BRANCH})
    res = gc.run_gc(cfg, repo_root="/repo", stale_hours=12, dry_run=False, runner=runner, now=NOW)

    assert res.dry_run is False
    assert any(
        c[:3] == ["git", "worktree", "remove"] and STALE_BRANCH in c[-1]
        for c in runner.calls
    )
    assert any(
        c[:3] == ["git", "branch", "-D"] and STALE_BRANCH in c for c in runner.calls
    )
    assert STALE_BRANCH in res.removed_branches


def test_gc_keeps_a_branch_with_an_open_pr_even_if_stale():
    """A stale worktree whose branch backs a still-open PR (e.g. a
    requeued-after-TIMEOUT PR) has its worktree reclaimed but its branch kept -
    gc must never tear a live PR's branch out from under it."""
    cfg = load_config()
    runner = _FakeRunner(open_pr_branches={STALE_BRANCH})
    res = gc.run_gc(cfg, repo_root="/repo", stale_hours=12, dry_run=False, runner=runner, now=NOW)

    assert any(
        c[:3] == ["git", "worktree", "remove"] and STALE_BRANCH in c[-1]
        for c in runner.calls
    )
    assert not any(c[:3] == ["git", "branch", "-D"] for c in runner.calls)
    assert STALE_BRANCH not in res.removed_branches
    assert any(b == STALE_BRANCH for b, _ in res.kept_branches)


def test_gc_summary_is_human_readable():
    cfg = load_config()
    runner = _FakeRunner(merged_branches={STALE_BRANCH})
    res = gc.run_gc(cfg, repo_root="/repo", stale_hours=12, dry_run=True, runner=runner, now=NOW)
    text = res.summary()
    assert "dry-run" in text
    assert STALE_BRANCH in text
