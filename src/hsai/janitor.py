"""Loop janitor: reclaim stranded tickets, orphaned worktrees, and dead branches.

A killed iteration (machine sleep, launchd kill, crash, hard budget halt) leaves
permanent debris: the ticket stays assigned - and because workers only ever
consider UNASSIGNED tickets (see :mod:`hsai.orchestrator`), it becomes invisible
to the backlog forever; the worktree stays on disk and in ``git worktree
list``; the iteration branch stays on the remote. Nothing reaps any of it.

Classification is pure and unit-tested; the impure ``scan_*``/``reap`` layer is
a thin wrapper that gathers evidence (git/gh) and hands it to the pure
functions, exactly like :mod:`hsai.postmortem`'s ``classify`` / Pareto split.

- :func:`classify_worktree` - one worktree registered under
  ``execution.worktrees_dir`` is **active** (git-locked, or its branch has an
  open PR), **orphaned** (no open PR AND no commits ahead of
  ``origin/<default_branch>`` - safe to remove), or **ambiguous** (a detached
  HEAD, an undetermined ahead-count, or unpushed commits with no PR) - and
  ambiguous worktrees are NEVER touched, mirroring the ``blocked``-ticket
  policy of leaving what cannot be safely decided for a human.
- :func:`classify_claim` - one assigned, open ticket's claim is **fresh**
  (within its TTL, or an open PR already references it), **stranded** (past
  its TTL, unreferenced, and assigned SOLELY to this loop's own login - never
  a human's), or **human** (any assignee other than the loop's own login,
  which is never touched, full stop).
- :func:`reap` executes only what :func:`classify_worktree`/:func:`classify_claim`
  decided is safe: removes orphaned worktrees (:func:`hsai.gitops.remove_worktree`
  + a metadata prune), deletes their now-dead remote branch, and returns
  stranded claims to the backlog with an incremented ``attempts:N`` label - or
  labels them ``blocked`` once ``max_ticket_attempts`` is reached, so the
  existing retry policy still governs (see
  :func:`hsai.orchestrator._recover_failed`, which this mirrors).

Synthesis: FoundationAgents/MetaGPT's ``stale.yaml`` (time-based reclamation of
abandoned work items), run-llama/llama_index's ``stale_bot.yml`` +
``close_new_integration_prs.yml`` (automated, policy-driven backlog hygiene
with explicit exemptions), and crewAIInc/crewAI's ``stale.yml`` plus the
principle that one crashed run must not poison subsequent ones. hsai's loop
additionally owns filesystem worktrees and remote branches, so this fuses the
upstream bot pattern into one conservative reaper spanning tickets, worktrees,
and branches, gated by the repo's existing attempts/blocked policy.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import github, gitops
from .config import CoreConfig
from .proc import Runner, run

# --- worktree classification --------------------------------------------------

ACTIVE = "active"
ORPHANED = "orphaned"
AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class WorktreeEntry:
    """One entry from ``git worktree list --porcelain``."""

    path: str
    branch: str = ""  # "" for a detached worktree
    locked: bool = False
    prunable: bool = False


@dataclass(frozen=True)
class WorktreeClassification:
    entry: WorktreeEntry
    status: str  # ACTIVE | ORPHANED | AMBIGUOUS
    reason: str


def parse_worktree_list(text: str) -> list[WorktreeEntry]:
    """Parse ``git worktree list --porcelain`` output (blank-line-separated blocks)."""
    entries: list[WorktreeEntry] = []
    for block in text.strip("\n").split("\n\n"):
        block = block.strip()
        if not block:
            continue
        path, branch, locked, prunable = "", "", False, False
        for line in block.splitlines():
            if line.startswith("worktree "):
                path = line[len("worktree "):].strip()
            elif line.startswith("branch "):
                branch = line[len("branch "):].strip().removeprefix("refs/heads/")
            elif line.startswith("locked"):
                locked = True
            elif line.startswith("prunable"):
                prunable = True
        if path:
            entries.append(WorktreeEntry(path=path, branch=branch, locked=locked, prunable=prunable))
    return entries


def classify_worktree(
    entry: WorktreeEntry,
    *,
    open_pr_branches: frozenset[str],
    ahead_of_main: int | None,
) -> WorktreeClassification:
    """Pure decision for one worktree already known to be under ``worktrees_dir``.

    Order matters: a lock or an open PR wins over everything else (active), a
    detached/undetermined worktree can never be proven safe (ambiguous), and
    only "no PR AND definitely zero commits ahead" is orphaned.
    """
    if entry.locked:
        return WorktreeClassification(entry, ACTIVE, "worktree is git-locked")
    if entry.branch and entry.branch in open_pr_branches:
        return WorktreeClassification(entry, ACTIVE, "branch has an open PR")
    if not entry.branch:
        return WorktreeClassification(
            entry, AMBIGUOUS, "detached HEAD - cannot verify safety without a branch"
        )
    if ahead_of_main is None:
        return WorktreeClassification(
            entry, AMBIGUOUS, "could not determine commits ahead of origin/main"
        )
    if ahead_of_main > 0:
        return WorktreeClassification(
            entry, AMBIGUOUS,
            f"{ahead_of_main} commit(s) ahead of origin/main but no open PR "
            "- may be unpushed work",
        )
    return WorktreeClassification(
        entry, ORPHANED, "no open PR and no commits ahead of origin/main"
    )


def scan_worktrees(
    cfg: CoreConfig, *, repo_root: str, runner: Runner = run
) -> list[WorktreeClassification]:
    """Classify every worktree registered under ``cfg.worktrees_dir``.

    The main checkout - and any worktree a human made by hand outside
    ``worktrees_dir`` - is never even considered, let alone touched.
    """
    root = Path(repo_root).resolve()
    managed_root = (root / cfg.worktrees_dir).resolve()
    entries = parse_worktree_list(
        gitops.list_worktrees_porcelain(cwd=str(root), runner=runner)
    )
    open_pr_branches = frozenset(
        pr.head_ref for pr in github.list_open_prs(cfg.repo_slug, runner=runner)
    )
    default_ref = f"origin/{cfg.default_branch}"

    results: list[WorktreeClassification] = []
    for entry in entries:
        try:
            in_scope = Path(entry.path).resolve().is_relative_to(managed_root)
        except (OSError, ValueError):
            in_scope = False
        if not in_scope:
            continue
        ahead = (
            gitops.commits_ahead(entry.branch, default_ref, cwd=str(root), runner=runner)
            if entry.branch
            else None
        )
        results.append(
            classify_worktree(entry, open_pr_branches=open_pr_branches, ahead_of_main=ahead)
        )
    return results


# --- claim classification -----------------------------------------------------

FRESH = "fresh"
STRANDED = "stranded"
HUMAN = "human"

DEFAULT_SAFETY_MULTIPLIER = 3.0


@dataclass(frozen=True)
class ClaimClassification:
    issue: github.Issue
    status: str  # FRESH | STRANDED | HUMAN
    reason: str


def derive_ttl_seconds(cfg: CoreConfig) -> float:
    """A claim's TTL: ``(agent_timeout + ci_remote_timeout) * safety_multiplier``.

    Deliberately derived from the SAME two ceilings a healthy iteration is
    already bounded by (the agent run itself, then the remote-CI poll before
    merge/recover - see :func:`hsai.orchestrator.run_once`), so it tracks
    those config values instead of drifting out of sync with them. The
    multiplier is slack for everything a timeout doesn't cover (git/gh
    round-trips, review-gate time, GitHub API latency).
    """
    agent_timeout = float(cfg.agent_timeout) if cfg.agent_timeout else 0.0
    multiplier = float(cfg.janitor.get("safety_multiplier", DEFAULT_SAFETY_MULTIPLIER))
    return (agent_timeout + float(cfg.ci_remote_timeout)) * multiplier


def _parse_timestamp(value: str) -> float | None:
    """Best-effort epoch-seconds from a `gh`-reported ISO-8601 timestamp."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def classify_claim(
    issue: github.Issue,
    *,
    now: float,
    ttl_seconds: float,
    referenced: frozenset[int],
    login: str,
) -> ClaimClassification | None:
    """Pure decision for one ticket's claim.

    Returns ``None`` for an unassigned ticket - there is no claim to classify.
    A ticket touched by ANY login other than ``login`` is always ``human``,
    even if it also carries the loop's own assignment; that assignee-union
    check is what makes "never unassign a human" an invariant rather than a
    best effort.
    """
    if not issue.assignees:
        return None
    others = [a for a in issue.assignees if a != login]
    if others:
        return ClaimClassification(
            issue, HUMAN,
            f"assigned to a login other than the loop's own: {', '.join(others)}",
        )
    if issue.number in referenced:
        return ClaimClassification(issue, FRESH, "an open PR already references this ticket")
    updated_ts = _parse_timestamp(issue.updated_at)
    if updated_ts is None:
        # Nothing to reason about - never guess a claim is stale.
        return ClaimClassification(issue, FRESH, "claim age unknown (no updatedAt reported)")
    age = now - updated_ts
    if age < ttl_seconds:
        return ClaimClassification(
            issue, FRESH, f"claimed {age:.0f}s ago, within the {ttl_seconds:.0f}s TTL"
        )
    return ClaimClassification(
        issue, STRANDED,
        f"claimed {age:.0f}s ago, past the {ttl_seconds:.0f}s TTL, no open PR references it",
    )


def scan_claims(
    cfg: CoreConfig,
    *,
    login: str,
    now: float,
    ttl_seconds: float | None = None,
    runner: Runner = run,
) -> list[ClaimClassification]:
    """Classify every assigned, open ticket's claim."""
    ttl = derive_ttl_seconds(cfg) if ttl_seconds is None else ttl_seconds
    repo = cfg.repo_slug
    issues = github.list_open_issues(repo, runner=runner)
    referenced = frozenset(github.referenced_tickets(github.list_open_prs(repo, runner=runner)))
    out: list[ClaimClassification] = []
    for issue in issues:
        c = classify_claim(issue, now=now, ttl_seconds=ttl, referenced=referenced, login=login)
        if c is not None:
            out.append(c)
    return out


# --- the reclaim plan -----------------------------------------------------------

@dataclass(frozen=True)
class ReclaimPlan:
    """Every classified worktree and claim from one scan - nothing executed yet."""

    worktrees: list[WorktreeClassification] = field(default_factory=list)
    claims: list[ClaimClassification] = field(default_factory=list)

    @property
    def active_worktrees(self) -> list[WorktreeClassification]:
        return [w for w in self.worktrees if w.status == ACTIVE]

    @property
    def orphaned_worktrees(self) -> list[WorktreeClassification]:
        return [w for w in self.worktrees if w.status == ORPHANED]

    @property
    def ambiguous_worktrees(self) -> list[WorktreeClassification]:
        return [w for w in self.worktrees if w.status == AMBIGUOUS]

    @property
    def fresh_claims(self) -> list[ClaimClassification]:
        return [c for c in self.claims if c.status == FRESH]

    @property
    def stranded_claims(self) -> list[ClaimClassification]:
        return [c for c in self.claims if c.status == STRANDED]

    @property
    def human_claims(self) -> list[ClaimClassification]:
        return [c for c in self.claims if c.status == HUMAN]

    @property
    def actionable(self) -> bool:
        """Something was decided safely enough to act on."""
        return bool(self.orphaned_worktrees or self.stranded_claims)

    @property
    def blocked(self) -> bool:
        """Debris exists but NOTHING could be safely decided this run."""
        return bool(self.ambiguous_worktrees) and not self.actionable


def render_plan(plan: ReclaimPlan) -> str:
    """Human-readable reclaim plan for ``hsai janitor`` (dry-run or not)."""
    lines = [
        "hsai janitor - reclaim plan",
        "",
        f"worktrees: {len(plan.orphaned_worktrees)} orphaned, "
        f"{len(plan.active_worktrees)} active, {len(plan.ambiguous_worktrees)} ambiguous",
    ]
    for w in plan.orphaned_worktrees:
        lines.append(f"  [reap] {w.entry.path} (branch={w.entry.branch or '-'}) - {w.reason}")
    for w in plan.ambiguous_worktrees:
        lines.append(f"  [skip: needs a human] {w.entry.path} - {w.reason}")
    lines += [
        "",
        f"claims: {len(plan.stranded_claims)} stranded, {len(plan.fresh_claims)} fresh, "
        f"{len(plan.human_claims)} human-assigned",
    ]
    for c in plan.stranded_claims:
        lines.append(f"  [reap] #{c.issue.number} {c.issue.title} - {c.reason}")
    for c in plan.human_claims:
        lines.append(f"  [skip: human-assigned] #{c.issue.number} {c.issue.title} - {c.reason}")
    if not plan.actionable:
        lines += ["", "nothing safely reclaimable this run."]
    return "\n".join(lines)


# --- execution: only what the plan decided is safe -----------------------------

@dataclass
class ReclaimReport:
    """What :func:`reap` actually did (or, in a dry run, would have done)."""

    worktrees_removed: list[str] = field(default_factory=list)
    branches_deleted: list[str] = field(default_factory=list)
    tickets_reopened: list[int] = field(default_factory=list)  # attempts incremented
    tickets_blocked: list[int] = field(default_factory=list)   # attempts exhausted

    @property
    def is_empty(self) -> bool:
        return not (
            self.worktrees_removed or self.branches_deleted
            or self.tickets_reopened or self.tickets_blocked
        )


def _reap_claim(
    cfg: CoreConfig, repo: str, issue: github.Issue, *, login: str, runner: Runner
) -> str:
    """Mirrors :func:`hsai.orchestrator._recover_failed`'s attempts/blocked policy."""
    prior = issue.attempts()
    nxt = prior + 1
    remove = [f"attempts:{prior}"] if prior else None
    if nxt >= cfg.max_ticket_attempts:
        github.edit_labels(repo, issue.number, add=["blocked"], remove=remove, runner=runner)
        github.unassign(repo, issue.number, login, runner=runner)
        return "blocked"
    github.edit_labels(repo, issue.number, add=[f"attempts:{nxt}"], remove=remove, runner=runner)
    github.unassign(repo, issue.number, login, runner=runner)
    return "reopened"


def reap(
    cfg: CoreConfig, plan: ReclaimPlan, *, repo_root: str, login: str, runner: Runner = run
) -> ReclaimReport:
    """Execute exactly the plan's actionable items.

    Active, ambiguous, fresh, and human entries are never touched - this is
    the only function in the module allowed to issue a destructive command,
    and it only ever issues one for something already classified ORPHANED or
    STRANDED.
    """
    report = ReclaimReport()
    repo = cfg.repo_slug

    for w in plan.orphaned_worktrees:
        gitops.remove_worktree(w.entry.path, cwd=repo_root, runner=runner)
        report.worktrees_removed.append(w.entry.path)
        if w.entry.branch:
            gitops.delete_remote_branch(w.entry.branch, cwd=repo_root, runner=runner)
            report.branches_deleted.append(w.entry.branch)
    if plan.orphaned_worktrees:
        # Sweeps any administrative entry whose directory was already gone
        # (e.g. `rm -rf`'d by hand) and that `remove_worktree` above therefore
        # could not clean up itself.
        gitops.prune_worktrees(cwd=repo_root, runner=runner)

    for c in plan.stranded_claims:
        outcome = _reap_claim(cfg, repo, c.issue, login=login, runner=runner)
        (report.tickets_blocked if outcome == "blocked" else report.tickets_reopened).append(
            c.issue.number
        )

    return report


def render_reclaimed(report: ReclaimReport) -> str:
    """The 'Reclaimed' section of the block review brief (see hsai.governance)."""
    if report.is_empty:
        return "_none this block_"
    lines: list[str] = []
    if report.worktrees_removed:
        lines.append("**Worktrees removed:**")
        lines += [f"- `{p}`" for p in report.worktrees_removed]
    if report.branches_deleted:
        lines.append("**Branches deleted:**")
        lines += [f"- `{b}`" for b in report.branches_deleted]
    if report.tickets_reopened:
        lines.append("**Tickets returned to the backlog (stranded claim):**")
        lines += [f"- #{n}" for n in report.tickets_reopened]
    if report.tickets_blocked:
        lines.append("**Tickets marked blocked (attempts exhausted):**")
        lines += [f"- #{n}" for n in report.tickets_blocked]
    return "\n".join(lines)


# --- top-level orchestration ----------------------------------------------------

@dataclass
class JanitorRun:
    plan: ReclaimPlan
    report: ReclaimReport
    dry_run: bool


def run_janitor(
    cfg: CoreConfig,
    *,
    repo_root: str = ".",
    dry_run: bool = False,
    ttl_seconds: float | None = None,
    now: float | None = None,
    runner: Runner = run,
) -> JanitorRun:
    """Scan (always) then, unless ``dry_run``, reap exactly the plan's actions."""
    login = github.current_login(runner=runner)
    now_ts = time.time() if now is None else now
    plan = ReclaimPlan(
        worktrees=scan_worktrees(cfg, repo_root=repo_root, runner=runner),
        claims=scan_claims(cfg, login=login, now=now_ts, ttl_seconds=ttl_seconds, runner=runner),
    )
    report = (
        ReclaimReport()
        if dry_run
        else reap(cfg, plan, repo_root=repo_root, login=login, runner=runner)
    )
    return JanitorRun(plan=plan, report=report, dry_run=dry_run)


def exit_code(plan: ReclaimPlan) -> int:
    """0 unless the scan found debris that nothing could be safely decided about."""
    return 1 if plan.blocked else 0


# --- `hsai doctor` health signal -------------------------------------------------

@dataclass(frozen=True)
class JanitorHealth:
    orphaned_worktrees: int
    ambiguous_worktrees: int
    stranded_claims: int

    def summary(self) -> str:
        return (
            f"orphaned_worktrees={self.orphaned_worktrees} "
            f"ambiguous_worktrees={self.ambiguous_worktrees} "
            f"stranded_claims={self.stranded_claims}"
        )


def health_counts(
    cfg: CoreConfig, *, repo_root: str = ".", runner: Runner = run
) -> JanitorHealth:
    """Read-only counts for ``hsai doctor`` - never reaps anything."""
    login = github.current_login(runner=runner)
    worktrees = scan_worktrees(cfg, repo_root=repo_root, runner=runner)
    claims = scan_claims(cfg, login=login, now=time.time(), runner=runner)
    return JanitorHealth(
        orphaned_worktrees=sum(1 for w in worktrees if w.status == ORPHANED),
        ambiguous_worktrees=sum(1 for w in worktrees if w.status == AMBIGUOUS),
        stranded_claims=sum(1 for c in claims if c.status == STRANDED),
    )
