"""``hsai gc``: reclaim registered-but-stale worktrees and their local branches.

The `try/finally` cleanup in :func:`hsai.orchestrator.run_once` makes future
worktree leaks structural fixes, not just discipline - but it cannot help a
worktree orphaned by something the `finally` never runs for (a killed
process, an interrupted machine) or one left behind before this fix shipped.
This module is the safety net: it finds registered worktrees under
``cfg.worktrees_dir`` that are old enough to be certainly abandoned, removes
them, and - separately, and more conservatively - deletes their local
``hsai/iter-*`` branch only when that branch is merged or has no open PR, so
a branch backing a still-open (e.g. requeued-after-TIMEOUT) PR is never torn
out from under it.

Defaults to ``dry_run=True``: reporting what *would* be reclaimed is always
safe; deleting worktrees and branches is only done when explicitly asked.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field

from . import gitops
from .config import CoreConfig
from .proc import Runner, run

# The loop always names its branches `hsai/iter-<epoch-seconds>-<n>-<hex>`;
# the embedded timestamp is the creation time, immune to any later mtime
# bump from a stray write inside the worktree.
_BRANCH_TS_RE = re.compile(r"iter-(\d+)-")


@dataclass(frozen=True)
class WorktreeEntry:
    path: str
    branch: str
    age_hours: float | None  # None when the branch carries no parseable timestamp


@dataclass
class GcResult:
    dry_run: bool = True
    removed_worktrees: list[str] = field(default_factory=list)
    removed_branches: list[str] = field(default_factory=list)
    kept_branches: list[tuple[str, str]] = field(default_factory=list)  # (branch, reason)

    def summary(self) -> str:
        verb = "would remove" if self.dry_run else "removed"
        lines = [
            f"gc ({'dry-run' if self.dry_run else 'live'}): "
            f"{verb} {len(self.removed_worktrees)} worktree(s), "
            f"{len(self.removed_branches)} branch(es)",
        ]
        for p in self.removed_worktrees:
            lines.append(f"  worktree: {p}")
        for b in self.removed_branches:
            lines.append(f"  branch:   {b}")
        for b, reason in self.kept_branches:
            lines.append(f"  kept branch: {b} ({reason})")
        return "\n".join(lines)


def _parse_worktree_list(output: str) -> list[tuple[str, str]]:
    """Parse `git worktree list --porcelain` into (path, branch) pairs.

    ``branch`` is "" for a detached worktree - e.g. a `repro-check-*` scratch
    worktree that its own `try/finally` (see :mod:`hsai.repro`) failed to
    remove because the process was killed. It carries no `hsai/iter-*`
    branch, so `discover_stale` below treats it as unconditionally stale
    (nothing to preserve) but never attempts to delete a branch for it.
    """
    entries: list[tuple[str, str]] = []
    path: str | None = None
    branch = ""
    for line in output.splitlines():
        if line.startswith("worktree "):
            if path is not None:
                entries.append((path, branch))
            path = line[len("worktree "):].strip()
            branch = ""
        elif line.startswith("branch "):
            ref = line[len("branch "):].strip()
            branch = ref.rsplit("/", 1)[-1] if ref.startswith("refs/heads/") else ref
    if path is not None:
        entries.append((path, branch))
    return entries


def _age_hours(branch: str, *, now: float) -> float | None:
    m = _BRANCH_TS_RE.search(branch or "")
    if not m:
        return None
    return max(0.0, (now - int(m.group(1))) / 3600.0)


def discover_stale(
    *,
    repo_root: str,
    worktrees_dir: str,
    stale_hours: float,
    runner: Runner = run,
    now: float | None = None,
) -> list[WorktreeEntry]:
    """Registered worktrees under ``worktrees_dir`` at least ``stale_hours`` old.

    A worktree whose branch carries no `hsai/iter-<epoch>-` timestamp cannot
    be a loop-owned iteration worktree (the loop always names them this way),
    so it is treated as unconditionally stale rather than silently skipped.
    """
    now = time.time() if now is None else now
    p = runner(["git", "worktree", "list", "--porcelain"], cwd=repo_root)
    marker = f"/{worktrees_dir.strip('/')}/"
    stale: list[WorktreeEntry] = []
    for path, branch in _parse_worktree_list(p.stdout):
        if marker not in path.replace("\\", "/"):
            continue  # the main checkout, or a worktree outside our management
        age = _age_hours(branch, now=now)
        if age is None or age >= stale_hours:
            stale.append(WorktreeEntry(path=path, branch=branch, age_hours=age))
    return stale


def _branch_is_merged(
    branch: str, default_branch: str, *, repo_root: str, runner: Runner
) -> bool:
    p = runner(
        ["git", "merge-base", "--is-ancestor", branch, f"origin/{default_branch}"],
        cwd=repo_root,
    )
    return p.ok


def _branch_has_open_pr(branch: str, repo: str, *, runner: Runner) -> bool:
    p = runner(
        [
            "gh", "pr", "list", "--repo", repo, "--head", branch,
            "--state", "open", "--json", "number",
        ],
    )
    try:
        return bool(json.loads(p.stdout or "[]"))
    except json.JSONDecodeError:
        return False


def run_gc(
    cfg: CoreConfig,
    *,
    repo_root: str,
    stale_hours: float | None = None,
    dry_run: bool = True,
    runner: Runner = run,
    now: float | None = None,
) -> GcResult:
    """Find and (unless ``dry_run``) remove stale worktrees + safe-to-drop branches."""
    threshold = cfg.worktree_gc_stale_hours if stale_hours is None else stale_hours
    stale = discover_stale(
        repo_root=repo_root, worktrees_dir=cfg.worktrees_dir,
        stale_hours=threshold, runner=runner, now=now,
    )
    result = GcResult(dry_run=dry_run)
    for entry in stale:
        result.removed_worktrees.append(entry.path)
        if not dry_run:
            gitops.remove_worktree(entry.path, cwd=repo_root, runner=runner)

        if not entry.branch:
            continue

        merged = _branch_is_merged(
            entry.branch, cfg.default_branch, repo_root=repo_root, runner=runner
        )
        has_open_pr = (
            False if merged else _branch_has_open_pr(entry.branch, cfg.repo_slug, runner=runner)
        )
        if merged or not has_open_pr:
            result.removed_branches.append(entry.branch)
            if not dry_run:
                runner(["git", "branch", "-D", entry.branch], cwd=repo_root)
        else:
            result.kept_branches.append((entry.branch, "has an open PR"))
    return result
