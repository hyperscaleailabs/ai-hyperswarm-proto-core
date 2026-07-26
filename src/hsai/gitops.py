"""Git operations: worktrees, syncing, branching, committing, pushing.

Each worker gets its own worktree so parallel workers never share a checkout.
"""
from __future__ import annotations

from pathlib import Path

from .proc import Proc, Runner, run


def _git(args: list[str], *, cwd: str | None, runner: Runner) -> Proc:
    return runner(["git", *args], cwd=cwd)


def repo_root(cwd: str | None = None, runner: Runner = run) -> str:
    p = _git(["rev-parse", "--show-toplevel"], cwd=cwd, runner=runner)
    return p.stdout.strip()


def sync_main(default_branch: str, *, cwd: str | None = None, runner: Runner = run) -> Proc:
    """Fetch the latest default branch from origin.

    Deliberately does NOT check out or mutate the shared working tree: workers
    create their worktrees from ``origin/<default_branch>``, which is what makes
    running several workers against one clone safe.
    """
    return _git(["fetch", "origin", default_branch], cwd=cwd, runner=runner)


def create_worktree(
    worktrees_dir: str,
    branch: str,
    *,
    base: str = "origin/main",
    cwd: str | None = None,
    runner: Runner = run,
) -> tuple[Proc, str]:
    """Create a fresh worktree on a new ``branch`` off ``base``.

    Returns the process result and the worktree path.
    """
    root = repo_root(cwd=cwd, runner=runner) or (cwd or ".")
    wt_path = str(Path(root) / worktrees_dir / branch)
    proc = _git(
        ["worktree", "add", "-b", branch, wt_path, base],
        cwd=cwd,
        runner=runner,
    )
    return proc, wt_path


def remove_worktree(wt_path: str, *, cwd: str | None = None, runner: Runner = run) -> Proc:
    return _git(["worktree", "remove", "--force", wt_path], cwd=cwd, runner=runner)


def has_changes(*, cwd: str, runner: Runner = run) -> bool:
    p = _git(["status", "--porcelain"], cwd=cwd, runner=runner)
    return bool(p.stdout.strip())


def changed_paths(*, cwd: str, runner: Runner = run) -> list[str]:
    """Paths changed in the worktree (modified, added, or untracked)."""
    p = _git(["status", "--porcelain"], cwd=cwd, runner=runner)
    paths: list[str] = []
    for line in p.stdout.splitlines():
        entry = line[3:] if len(line) > 3 else line.strip()
        if "->" in entry:  # rename: "old -> new"
            entry = entry.split("->", 1)[1]
        entry = entry.strip().strip('"')
        if entry:
            paths.append(entry)
    return paths


def restore_pathspec(pathspec: str, *, cwd: str, runner: Runner = run) -> None:
    """Discard both tracked edits and new files under ``pathspec``."""
    _git(["checkout", "HEAD", "--", pathspec], cwd=cwd, runner=runner)
    _git(["clean", "-fd", pathspec], cwd=cwd, runner=runner)


def commit_all(message: str, *, cwd: str, runner: Runner = run) -> Proc:
    _git(["add", "-A"], cwd=cwd, runner=runner)
    return _git(["commit", "-m", message], cwd=cwd, runner=runner)


def push_branch(branch: str, *, cwd: str, runner: Runner = run) -> Proc:
    return _git(["push", "-u", "origin", branch], cwd=cwd, runner=runner)
