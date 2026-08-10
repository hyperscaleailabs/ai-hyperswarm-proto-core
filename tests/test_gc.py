import json

from hsai import gc
from hsai.config import load_config
from hsai.proc import Proc

PORCELAIN = """worktree /repo
HEAD aaa
branch refs/heads/main

worktree /repo/.hsai/worktrees/hsai/iter-old-merged
HEAD bbb
branch refs/heads/hsai/iter-old-merged

worktree /repo/.hsai/worktrees/hsai/iter-old-nopr
HEAD ccc
branch refs/heads/hsai/iter-old-nopr

worktree /repo/.hsai/worktrees/hsai/iter-old-openpr
HEAD ddd
branch refs/heads/hsai/iter-old-openpr

worktree /repo/.hsai/worktrees/hsai/iter-fresh
HEAD eee
branch refs/heads/hsai/iter-fresh

worktree /repo/.hsai/worktrees/detached-thing
HEAD fff
detached
"""


def test_list_worktrees_parses_porcelain_output():
    def runner(cmd, **kwargs):
        return Proc(cmd, 0, PORCELAIN, "")

    entries = gc.list_worktrees(runner=runner)
    assert [e.path for e in entries] == [
        "/repo",
        "/repo/.hsai/worktrees/hsai/iter-old-merged",
        "/repo/.hsai/worktrees/hsai/iter-old-nopr",
        "/repo/.hsai/worktrees/hsai/iter-old-openpr",
        "/repo/.hsai/worktrees/hsai/iter-fresh",
        "/repo/.hsai/worktrees/detached-thing",
    ]
    assert entries[0].branch == "main"
    assert entries[1].branch == "hsai/iter-old-merged"
    assert entries[-1].branch == ""  # detached


class _GcRunner:
    """Answers the read-only calls `plan_gc` makes; records mutating ones."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, cmd, *, cwd=None, **kwargs):
        cmd = list(cmd)
        self.calls.append(cmd)
        if cmd[:3] == ["git", "rev-parse", "--show-toplevel"]:
            return Proc(cmd, 0, "/repo\n", "")
        if cmd[:3] == ["git", "worktree", "list"]:
            return Proc(cmd, 0, PORCELAIN, "")
        if cmd[:3] == ["git", "branch", "--merged"]:
            return Proc(cmd, 0, "  main\n  hsai/iter-old-merged\n", "")
        if cmd[:3] == ["gh", "pr", "list"]:
            head = cmd[cmd.index("--head") + 1]
            prs = [] if head != "hsai/iter-old-openpr" else [{"number": 99}]
            return Proc(cmd, 0, json.dumps(prs), "")
        if cmd[:3] in (["git", "worktree", "remove"], ["git", "branch", "-D"]):
            return Proc(cmd, 0, "", "")
        raise AssertionError(f"unhandled command {cmd!r}")


def _mtimes(now: float) -> dict[str, float]:
    old = now - 100_000  # far older than any sane --older-than threshold
    fresh = now - 10     # created moments ago
    return {
        "/repo/.hsai/worktrees/hsai/iter-old-merged": old,
        "/repo/.hsai/worktrees/hsai/iter-old-nopr": old,
        "/repo/.hsai/worktrees/hsai/iter-old-openpr": old,
        "/repo/.hsai/worktrees/hsai/iter-fresh": fresh,
        "/repo/.hsai/worktrees/detached-thing": old,
    }


def test_plan_gc_only_flags_stale_worktrees_under_the_configured_dir():
    cfg = load_config()
    now = 1_000_000.0
    mtimes = _mtimes(now)
    runner = _GcRunner()

    plan = gc.plan_gc(
        cfg, older_than_seconds=3600, now=now, cwd="/repo",
        runner=runner, getmtime=lambda p: mtimes[p],
    )

    # the main checkout is never touched; the fresh worktree is under the
    # threshold and is left alone; the detached one has no branch to delete
    assert sorted(plan.stale_worktrees) == sorted([
        "/repo/.hsai/worktrees/hsai/iter-old-merged",
        "/repo/.hsai/worktrees/hsai/iter-old-nopr",
        "/repo/.hsai/worktrees/hsai/iter-old-openpr",
        "/repo/.hsai/worktrees/detached-thing",
    ])
    assert "/repo" not in plan.stale_worktrees
    assert "/repo/.hsai/worktrees/hsai/iter-fresh" not in plan.stale_worktrees


def test_plan_gc_removes_merged_branches_and_branches_with_no_open_pr():
    cfg = load_config()
    now = 1_000_000.0
    mtimes = _mtimes(now)
    runner = _GcRunner()

    plan = gc.plan_gc(
        cfg, older_than_seconds=3600, now=now, cwd="/repo",
        runner=runner, getmtime=lambda p: mtimes[p],
    )

    assert "hsai/iter-old-merged" in plan.removable_branches      # merged
    assert "hsai/iter-old-nopr" in plan.removable_branches         # no open PR
    assert "hsai/iter-old-openpr" not in plan.removable_branches   # open PR, not merged
    assert plan.kept_branches == ["hsai/iter-old-openpr"]


def test_plan_gc_never_deletes_branches_read_only():
    """`plan_gc` (used for --dry-run) must never mutate anything."""
    cfg = load_config()
    now = 1_000_000.0
    mtimes = _mtimes(now)
    runner = _GcRunner()

    gc.plan_gc(
        cfg, older_than_seconds=3600, now=now, cwd="/repo",
        runner=runner, getmtime=lambda p: mtimes[p],
    )

    mutating = [
        c for c in runner.calls
        if c[:3] in (["git", "worktree", "remove"], ["git", "branch", "-D"])
    ]
    assert mutating == []


def test_apply_gc_removes_worktrees_and_removable_branches_only():
    plan = gc.GcPlan(
        stale_worktrees=["/repo/.hsai/worktrees/hsai/iter-a", "/repo/.hsai/worktrees/hsai/iter-b"],
        removable_branches=["hsai/iter-a"],
        kept_branches=["hsai/iter-b"],
    )
    runner = _GcRunner()

    gc.apply_gc(plan, cwd="/repo", runner=runner)

    removed_worktrees = [c[4] for c in runner.calls if c[:3] == ["git", "worktree", "remove"]]
    assert sorted(removed_worktrees) == sorted(plan.stale_worktrees)
    deleted_branches = [c[3] for c in runner.calls if c[:3] == ["git", "branch", "-D"]]
    assert deleted_branches == ["hsai/iter-a"]   # the kept branch is never deleted
