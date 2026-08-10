"""`hsai gc`: reclaim orphaned worktrees and their stale local branches.

`run_once` cleans up its own worktree in a ``finally`` block (see
:mod:`hsai.orchestrator`), so in steady state this command should find
nothing to do. It exists for two reasons: the backlog this repo already
accumulated before that fix landed (worktrees leaked by an iteration that
raised between worktree creation and its old, non-``finally`` cleanup call),
and as a standing safety net for anything that escapes the normal lifecycle
entirely (a hard process kill skips ``finally`` too).

A worktree only qualifies once it is BOTH registered under
``cfg.worktrees_dir`` and older than the ``--older-than`` threshold - a
worktree an in-flight iteration is actively using is never touched. Deleting
its local ``hsai/iter-*`` branch is a separate, more conservative decision:
only once the branch is merged into the default branch, or has no open PR at
all, since either case means nothing can still land through it.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from . import gitops
from .config import CoreConfig
from .proc import Runner, run

BRANCH_PREFIX = "hsai/iter-"


@dataclass(frozen=True)
class WorktreeEntry:
    path: str
    branch: str = ""  # "" for a detached worktree (never touched by gc)


def list_worktrees(*, cwd: str | None = None, runner: Runner = run) -> list[WorktreeEntry]:
    """Parse ``git worktree list --porcelain`` into structured entries."""
    p = runner(["git", "worktree", "list", "--porcelain"], cwd=cwd)
    entries: list[WorktreeEntry] = []
    path = ""
    branch = ""
    for line in p.stdout.splitlines():
        if line.startswith("worktree "):
            if path:
                entries.append(WorktreeEntry(path=path, branch=branch))
            path = line[len("worktree "):].strip()
            branch = ""
        elif line.startswith("branch "):
            ref = line[len("branch "):].strip()
            # Strip only the `refs/heads/` prefix - branch names themselves
            # contain `/` (e.g. `hsai/iter-...`), so `rsplit` would truncate them.
            prefix = "refs/heads/"
            branch = ref[len(prefix):] if ref.startswith(prefix) else ref
    if path:
        entries.append(WorktreeEntry(path=path, branch=branch))
    return entries


def _is_merged(branch: str, default_branch: str, *, cwd: str | None, runner: Runner) -> bool:
    p = runner(["git", "branch", "--merged", f"origin/{default_branch}"], cwd=cwd)
    names = {ln.strip().lstrip("* +").strip() for ln in p.stdout.splitlines() if ln.strip()}
    return branch in names


def _has_open_pr(repo: str, branch: str, *, cwd: str | None, runner: Runner) -> bool:
    p = runner(
        ["gh", "pr", "list", "--repo", repo, "--head", branch, "--state", "open",
         "--json", "number"],
        cwd=cwd,
    )
    try:
        return bool(json.loads(p.stdout or "[]"))
    except json.JSONDecodeError:
        # Can't confirm there's no open PR - the conservative read is "keep it".
        return True


@dataclass
class GcPlan:
    """What ``hsai gc`` would do (``--dry-run``) or did (without it)."""

    stale_worktrees: list[str] = field(default_factory=list)
    removable_branches: list[str] = field(default_factory=list)
    kept_branches: list[str] = field(default_factory=list)  # stale, but has an open PR


def plan_gc(
    cfg: CoreConfig,
    *,
    older_than_seconds: float,
    now: float,
    cwd: str | None = None,
    runner: Runner = run,
    getmtime=None,
) -> GcPlan:
    """Decide what to remove without touching anything (read-only)."""
    getmtime = getmtime or os.path.getmtime
    root_str = gitops.repo_root(cwd=cwd, runner=runner) or (cwd or ".")
    root = Path(root_str).resolve()
    worktrees_root = str((root / cfg.worktrees_dir).resolve())

    stale: list[WorktreeEntry] = []
    for entry in list_worktrees(cwd=cwd, runner=runner):
        resolved = str(Path(entry.path).resolve())
        if resolved == str(root):
            continue  # never touch the main checkout
        if not (resolved == worktrees_root or resolved.startswith(worktrees_root + os.sep)):
            continue  # not one of ours
        try:
            age = now - getmtime(entry.path)
        except OSError:
            # Registered in git but gone from disk: definitely stale.
            age = older_than_seconds
        if age >= older_than_seconds:
            stale.append(entry)

    removable: list[str] = []
    kept: list[str] = []
    for entry in stale:
        if not entry.branch or not entry.branch.startswith(BRANCH_PREFIX):
            continue
        if _is_merged(entry.branch, cfg.default_branch, cwd=cwd, runner=runner):
            removable.append(entry.branch)
        elif not _has_open_pr(cfg.repo_slug, entry.branch, cwd=cwd, runner=runner):
            removable.append(entry.branch)
        else:
            kept.append(entry.branch)

    return GcPlan(
        stale_worktrees=[e.path for e in stale],
        removable_branches=removable,
        kept_branches=kept,
    )


def apply_gc(plan: GcPlan, *, cwd: str | None = None, runner: Runner = run) -> None:
    """Execute a plan: remove worktrees, then delete the branches it cleared."""
    for path in plan.stale_worktrees:
        gitops.remove_worktree(path, cwd=cwd, runner=runner)
    for branch in plan.removable_branches:
        runner(["git", "branch", "-D", branch], cwd=cwd)
