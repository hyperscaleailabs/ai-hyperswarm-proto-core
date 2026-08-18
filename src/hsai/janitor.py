"""The loop janitor: reclaim stranded tickets, orphaned worktrees, dead branches.

A killed iteration (machine sleep, launchd kill, crash, hard budget halt)
leaves permanent debris behind: the ticket stays assigned (and therefore
invisible to every worker, which only considers *unassigned* tickets), its
worktree stays on disk and in ``git worktree list``, and its branch stays on
the remote. Nothing reclaims any of that on its own.

This module is deliberately split in two:

- Pure decision logic (:func:`classify_worktree`, :func:`classify_claim`) that
  takes plain data in and returns a verdict out - no subprocess, no I/O, fully
  unit-testable with fixtures.
- A thin execution layer (:func:`scan_worktrees`, :func:`scan_claims`,
  :func:`build_plan`, :func:`reap`) that gathers the real git/gh state and
  either reports the plan (``--dry-run``) or carries it out.

Every classification lands in one of three buckets:

- **active** - a live lock, an open PR on the branch, or (for a worktree with
  no PR and no commits yet) still inside its TTL window - it may simply be
  mid-run.
- **orphaned** (worktrees) / **stranded** (claims) - safe to reclaim.
- **ambiguous** - never touched. Reported for a human, exactly like the
  ``blocked`` ticket policy: the janitor would rather leave debris behind than
  destroy work it cannot prove is dead.

A claim is only ever reclaimed when it is assigned to *this loop's own*
GitHub login - never a human's - and a worktree is only ever removed when its
branch has no open PR.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import github, gitops
from .config import CoreConfig
from .proc import Runner, run

# --- classification vocabulary -------------------------------------------------
ACTIVE = "active"
ORPHANED = "orphaned"
AMBIGUOUS = "ambiguous"

FRESH = "fresh"
STRANDED = "stranded"
HUMAN = "human-assigned"

# execution.agent_timeout_seconds is optional in core.yaml (None disables the
# hard cap); the janitor still needs *some* number to derive a TTL from, so it
# falls back to the value core.yaml ships with today.
DEFAULT_AGENT_TIMEOUT_SECONDS = 1200.0
DEFAULT_SAFETY_MULTIPLIER = 3.0

# Iteration branches are named `hsai/iter-<epoch>-<block*100+i>-<hex>` (see
# orchestrator.run_once). The embedded epoch doubles as a reliable "run
# started at" timestamp - no separate bookkeeping (a lock file, a registry) is
# needed to tell a brand-new worktree apart from an abandoned one.
_ITER_BRANCH_RE = re.compile(r"^hsai/iter-(\d+)-")


def derive_ttl_seconds(cfg: CoreConfig) -> float:
    """How long a claim/worktree may sit unresolved before it is "stranded".

    ``execution.agent_timeout_seconds`` covers the agent's own run;
    ``execution.ci_remote_timeout_seconds`` covers the wait for the PR's
    remote checks to conclude. Summed and multiplied by a safety factor
    (``janitor.ttl_safety_multiplier``, default :data:`DEFAULT_SAFETY_MULTIPLIER`)
    so one slow-but-legitimate iteration is never mistaken for an abandoned one.
    """
    agent_timeout = cfg.agent_timeout or DEFAULT_AGENT_TIMEOUT_SECONDS
    multiplier = float(cfg.janitor.get("ttl_safety_multiplier", DEFAULT_SAFETY_MULTIPLIER))
    return (float(agent_timeout) + float(cfg.ci_remote_timeout)) * multiplier


def _branch_epoch(branch: str) -> float | None:
    m = _ITER_BRANCH_RE.match(branch)
    return float(m.group(1)) if m else None


# --- worktrees -------------------------------------------------------------


@dataclass(frozen=True)
class WorktreeEntry:
    """One ``git worktree list`` entry, plus the context needed to judge it."""

    path: str
    branch: str = ""          # short branch name; "" when detached
    locked: bool = False      # `git worktree lock` is currently held
    commits_ahead: int = 0    # commits on `branch` not in origin/<default_branch>
    has_open_pr: bool = False
    # Seconds since the branch's embedded creation epoch; None when it cannot
    # be derived (a non-standard branch name).
    age_seconds: float | None = None


@dataclass(frozen=True)
class WorktreeVerdict:
    entry: WorktreeEntry
    status: str  # ACTIVE | ORPHANED | AMBIGUOUS
    reason: str


def classify_worktree(entry: WorktreeEntry, *, ttl_seconds: float) -> WorktreeVerdict:
    """Pure classification: active, orphaned (safe to reclaim), or ambiguous."""
    if entry.locked:
        return WorktreeVerdict(entry, ACTIVE, "worktree is locked")
    if entry.has_open_pr:
        return WorktreeVerdict(entry, ACTIVE, "branch has an open PR")
    if entry.commits_ahead > 0:
        return WorktreeVerdict(
            entry, AMBIGUOUS,
            f"{entry.commits_ahead} commit(s) ahead of origin/main but no open PR "
            "- needs a human",
        )
    # No PR, no commits: either still mid-run (young) or truly abandoned.
    if entry.age_seconds is None:
        return WorktreeVerdict(
            entry, AMBIGUOUS, "branch name has no derivable age - needs a human"
        )
    if entry.age_seconds < ttl_seconds:
        return WorktreeVerdict(
            entry, ACTIVE,
            f"age {entry.age_seconds:.0f}s < ttl {ttl_seconds:.0f}s - likely still running",
        )
    return WorktreeVerdict(
        entry, ORPHANED,
        f"age {entry.age_seconds:.0f}s >= ttl {ttl_seconds:.0f}s, no open PR, no commits ahead",
    )


def parse_worktree_list(text: str) -> list[dict]:
    """Parse ``git worktree list --porcelain`` into one dict per entry."""
    entries: list[dict] = []
    current: dict = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        key, _, value = line.partition(" ")
        if key == "worktree":
            if current:
                entries.append(current)
            current = {"worktree": value}
        elif key == "branch":
            current["branch"] = value  # "refs/heads/<name>"
        elif key == "detached":
            current["detached"] = True
        elif key == "locked":
            current["locked"] = True
        elif key == "prunable":
            current["prunable"] = True
    if current:
        entries.append(current)
    return entries


def scan_worktrees(
    cfg: CoreConfig,
    *,
    now: float,
    ttl_seconds: float,
    repo_dir: str = ".",
    runner: Runner = run,
) -> list[WorktreeVerdict]:
    """Gather real worktree/branch/PR state and classify every managed entry.

    Only worktrees under ``cfg.worktrees_dir`` are considered - the main
    checkout (and anything else ``git worktree list`` reports) is never in
    scope for reclamation.
    """
    porcelain = gitops.list_worktrees_porcelain(cwd=repo_dir, runner=runner)
    root = gitops.repo_root(cwd=repo_dir, runner=runner) or repo_dir
    prefix = str(Path(root) / cfg.worktrees_dir) + "/"
    open_pr_branches = {
        pr.head_ref for pr in github.list_open_prs(cfg.repo_slug, runner=runner)
    }

    verdicts: list[WorktreeVerdict] = []
    for raw in parse_worktree_list(porcelain):
        path = raw.get("worktree", "")
        if not path.startswith(prefix):
            continue
        branch_ref = raw.get("branch", "")
        branch = branch_ref.removeprefix("refs/heads/") if branch_ref else ""
        commits_ahead = 0
        if branch:
            commits_ahead = gitops.rev_list_count(
                f"origin/{cfg.default_branch}..{branch}", cwd=repo_dir, runner=runner
            )
        epoch = _branch_epoch(branch)
        entry = WorktreeEntry(
            path=path,
            branch=branch,
            locked=bool(raw.get("locked", False)),
            commits_ahead=commits_ahead,
            has_open_pr=branch in open_pr_branches,
            age_seconds=(now - epoch) if epoch is not None else None,
        )
        verdicts.append(classify_worktree(entry, ttl_seconds=ttl_seconds))
    return verdicts


# --- ticket claims -----------------------------------------------------------


@dataclass(frozen=True)
class ClaimVerdict:
    issue: github.Issue
    status: str  # FRESH | STRANDED | HUMAN | AMBIGUOUS
    reason: str


def _issue_age_seconds(updated_at: str, *, now: float) -> float | None:
    if not updated_at:
        return None
    try:
        dt = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    return now - dt.timestamp()


def classify_claim(
    issue: github.Issue,
    *,
    now: float,
    ttl_seconds: float,
    loop_login: str,
    referenced_by_open_pr: bool,
) -> ClaimVerdict:
    """Pure classification of one already-assigned open ticket.

    Never returns STRANDED for a ticket assigned to anyone but ``loop_login`` -
    a human's claim is never touched, full stop.
    """
    if any(a != loop_login for a in issue.assignees):
        return ClaimVerdict(
            issue, HUMAN,
            f"assigned to {', '.join(issue.assignees) or '(unknown)'}, not the loop's own "
            f"login ({loop_login!r}) - never touched",
        )
    if referenced_by_open_pr:
        return ClaimVerdict(issue, FRESH, "an open PR references this ticket")
    age = _issue_age_seconds(issue.updated_at, now=now)
    if age is None:
        return ClaimVerdict(issue, AMBIGUOUS, "no updatedAt timestamp to judge age - needs a human")
    if age < ttl_seconds:
        return ClaimVerdict(issue, FRESH, f"age {age:.0f}s < ttl {ttl_seconds:.0f}s")
    return ClaimVerdict(
        issue, STRANDED,
        f"age {age:.0f}s >= ttl {ttl_seconds:.0f}s, no open PR references it, "
        "assignee is the loop's own login",
    )


def scan_claims(
    cfg: CoreConfig, *, now: float, ttl_seconds: float, loop_login: str, runner: Runner = run
) -> list[ClaimVerdict]:
    """Gather every assigned open ticket and classify its claim."""
    repo = cfg.repo_slug
    issues = [i for i in github.list_open_issues(repo, runner=runner) if i.assignees]
    referenced = github.referenced_tickets(github.list_open_prs(repo, runner=runner))
    return [
        classify_claim(
            i, now=now, ttl_seconds=ttl_seconds, loop_login=loop_login,
            referenced_by_open_pr=i.number in referenced,
        )
        for i in issues
    ]


# --- the plan: build, render, reap -------------------------------------------


@dataclass
class ReapPlan:
    ttl_seconds: float
    loop_login: str
    worktrees: list[WorktreeVerdict] = field(default_factory=list)
    claims: list[ClaimVerdict] = field(default_factory=list)
    # Populated by reap(); empty for a --dry-run (or unexecuted) plan.
    removed_worktrees: list[str] = field(default_factory=list)
    deleted_branches: list[str] = field(default_factory=list)
    returned_tickets: list[int] = field(default_factory=list)
    blocked_tickets: list[int] = field(default_factory=list)

    @property
    def active_worktrees(self) -> list[WorktreeVerdict]:
        return [v for v in self.worktrees if v.status == ACTIVE]

    @property
    def orphaned_worktrees(self) -> list[WorktreeVerdict]:
        return [v for v in self.worktrees if v.status == ORPHANED]

    @property
    def ambiguous_worktrees(self) -> list[WorktreeVerdict]:
        return [v for v in self.worktrees if v.status == AMBIGUOUS]

    @property
    def fresh_claims(self) -> list[ClaimVerdict]:
        return [v for v in self.claims if v.status == FRESH]

    @property
    def stranded_claims(self) -> list[ClaimVerdict]:
        return [v for v in self.claims if v.status == STRANDED]

    @property
    def human_claims(self) -> list[ClaimVerdict]:
        return [v for v in self.claims if v.status == HUMAN]

    @property
    def ambiguous_claims(self) -> list[ClaimVerdict]:
        return [v for v in self.claims if v.status == AMBIGUOUS]

    def has_decision(self) -> bool:
        """True once the plan found something it can safely reclaim."""
        return bool(self.orphaned_worktrees or self.stranded_claims)

    def has_ambiguous(self) -> bool:
        return bool(self.ambiguous_worktrees or self.ambiguous_claims)

    def render(self) -> str:
        lines = [f"janitor: ttl={self.ttl_seconds:.0f}s login={self.loop_login!r}"]
        lines.append(
            f"worktrees: {len(self.worktrees)} scanned "
            f"({len(self.active_worktrees)} active, {len(self.orphaned_worktrees)} orphaned, "
            f"{len(self.ambiguous_worktrees)} ambiguous)"
        )
        for v in self.orphaned_worktrees:
            lines.append(
                f"  - RECLAIM worktree {v.entry.path} (branch={v.entry.branch or '-'}): {v.reason}"
            )
        for v in self.ambiguous_worktrees:
            lines.append(
                f"  - AMBIGUOUS worktree {v.entry.path} (branch={v.entry.branch or '-'}): "
                f"{v.reason}"
            )
        lines.append(
            f"claims: {len(self.claims)} scanned "
            f"({len(self.fresh_claims)} fresh, {len(self.stranded_claims)} stranded, "
            f"{len(self.human_claims)} human-assigned, {len(self.ambiguous_claims)} ambiguous)"
        )
        for v in self.stranded_claims:
            lines.append(f"  - RECLAIM ticket #{v.issue.number} {v.issue.title}: {v.reason}")
        for v in self.ambiguous_claims:
            lines.append(f"  - AMBIGUOUS ticket #{v.issue.number} {v.issue.title}: {v.reason}")
        if self.removed_worktrees or self.deleted_branches or self.returned_tickets \
                or self.blocked_tickets:
            lines.append("executed:")
            lines += [f"  - removed worktree {p}" for p in self.removed_worktrees]
            lines += [f"  - deleted branch {b}" for b in self.deleted_branches]
            lines += [f"  - #{n} returned to backlog (attempts incremented)" for n in self.returned_tickets]
            lines += [f"  - #{n} labelled blocked (max attempts reached)" for n in self.blocked_tickets]
        return "\n".join(lines)


def build_plan(
    cfg: CoreConfig,
    *,
    now: float,
    repo_dir: str = ".",
    ttl_seconds: float | None = None,
    runner: Runner = run,
) -> ReapPlan:
    """Scan real state and classify it. Read-only: no git/gh mutation."""
    ttl = ttl_seconds if ttl_seconds is not None else derive_ttl_seconds(cfg)
    login = github.current_login(runner=runner)
    worktrees = scan_worktrees(cfg, now=now, ttl_seconds=ttl, repo_dir=repo_dir, runner=runner)
    claims = scan_claims(cfg, now=now, ttl_seconds=ttl, loop_login=login, runner=runner)
    return ReapPlan(ttl_seconds=ttl, loop_login=login, worktrees=worktrees, claims=claims)


def reap(
    cfg: CoreConfig,
    plan: ReapPlan,
    *,
    repo_dir: str = ".",
    dry_run: bool = False,
    runner: Runner = run,
) -> ReapPlan:
    """Carry out ``plan``: remove orphaned worktrees/branches, unassign stranded
    claims. A no-op under ``dry_run`` - the plan is returned unchanged so the
    caller can still print/inspect it.

    Attempts/blocked policy mirrors ``orchestrator._recover_failed``: a
    stranded ticket is unassigned with its ``attempts:N`` label incremented,
    unless that increment would meet ``max_ticket_attempts``, in which case it
    is labelled ``blocked`` instead of being returned for another attempt.
    """
    if dry_run:
        return plan

    repo = cfg.repo_slug
    for v in plan.orphaned_worktrees:
        gitops.remove_worktree(v.entry.path, cwd=repo_dir, runner=runner)
        plan.removed_worktrees.append(v.entry.path)
        if v.entry.branch:
            bproc = gitops.delete_remote_branch(v.entry.branch, cwd=repo_dir, runner=runner)
            if bproc.ok:
                plan.deleted_branches.append(v.entry.branch)
    # Sweep any leftover worktree administrative metadata (e.g. a worktree
    # directory removed by hand outside of `git worktree remove`).
    gitops.prune_worktrees(cwd=repo_dir, runner=runner)

    for v in plan.stranded_claims:
        issue = v.issue
        prior = issue.attempts()
        nxt = prior + 1
        remove_label = [f"attempts:{prior}"] if prior else None
        if nxt >= cfg.max_ticket_attempts:
            github.edit_labels(repo, issue.number, add=["blocked"], remove=remove_label, runner=runner)
            github.unassign(repo, issue.number, plan.loop_login, runner=runner)
            plan.blocked_tickets.append(issue.number)
        else:
            github.edit_labels(
                repo, issue.number, add=[f"attempts:{nxt}"], remove=remove_label, runner=runner
            )
            github.unassign(repo, issue.number, plan.loop_login, runner=runner)
            plan.returned_tickets.append(issue.number)
    return plan
