"""The hsai autonomous loop.

One iteration = sync main -> CI check -> choose work (heal / implement /
self-improve) -> ensure a ticket exists -> run a model -> re-check CI ->
record a lesson -> open a linked PR -> merge on green.

Every terminal exit - merged, guard-aborted, or recovered - passes through the
same two places: one trajectory write (:mod:`hsai.trajectory`) and one ledger
record (:mod:`hsai.ledger`), both carrying the iteration's failure class from
:mod:`hsai.failures`. That class is what makes a retry different from the
attempt before it: ``execution.retry_policy`` maps it to an action (cheaper
tier, longer timeout, remediation excerpt in the prompt, or block on the spot),
and the ``failure:<class>`` label carries it to the next worker.

Decision logic (:func:`decide_path`) and PR-body assembly
(:func:`build_pr_body`) are pure and unit-tested; the orchestration around them
performs the real side effects through the wrapper modules.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from uuid import uuid4

from . import ai, ci, failures, github, gitops, ledger, recall, repro, trajectory
from .config import CoreConfig
from .knowledge import KnowledgeBase, Lesson
from .models import ModelChoice, Task, select
from .proc import Runner, run
from .tickets import NEEDS_REFINEMENT, issue_well_formed

HEAL = "heal"
IMPLEMENT = "implement"
IMPROVE = "improve"

# Outcomes that mean the iteration reached its gate cleanly; everything else is
# a failure and gets classified.
_CLEAN_OUTCOMES = frozenset({"merged", "pass"})

# How much longer a retry gets after a `timeout` failure (the `escalate_timeout`
# action). Bounded and multiplicative: one more shot at a slow ticket, not an
# open-ended one.
TIMEOUT_ESCALATION = 2.0

# The changed-path list on a trajectory is evidence, not an inventory: a
# thousand-file diff would say nothing the diffstat does not already say.
MAX_RECORDED_PATHS = 100

# Serializes the short git-metadata prologue and the ticket claim so parallel
# workers (threads) never race on git's index lock or grab the same ticket. The
# slow work (agent run, CI, push, PR, merge) happens outside this lock.
_SERIAL = threading.Lock()


def _format_error_with_context(
    error: str, phase: str, ticket: int | None
) -> str:
    """Format an error with execution context for better traceability.

    Adopted from openai/swarm: lightweight orchestration systems include phase
    context with errors so failures can be traced to which step failed and why.
    See: openai/swarm lightweight agent orchestration patterns.
    """
    context_parts = [f"phase={phase}"]
    if ticket:
        context_parts.append(f"ticket=#{ticket}")
    context = ", ".join(context_parts)
    return f"[{context}] {error}"


def _phase_artifacts(kind: str) -> str:
    """Document explicit outputs for each phase.

    Adopted from FoundationAgents/MetaGPT: multi-agent systems benefit from
    explicit role-based artifact definitions. Each phase has clear deliverables
    that can be audited and verified. This supports G2 (auditability) by making
    the work products of each phase visible in the PR.
    """
    if kind == HEAL:
        return (
            "- Root cause of build failure identified\n"
            "- Regression test added or modified to reproduce bug\n"
            "- Minimal fix applied\n"
            "- CI returned to green"
        )
    elif kind == IMPLEMENT:
        return (
            "- Feature/fix implemented end-to-end\n"
            "- Tests added covering acceptance criteria\n"
            "- Code change scoped to ticket description\n"
            "- Linting and tests passing"
        )
    else:  # IMPROVE
        return (
            "- One practice extracted from reference-set project\n"
            "- Small, focused implementation of the practice\n"
            "- Lesson recorded with source citation\n"
            "- Tests passing, auditable change"
        )


def decide_path(ci_green: bool, has_tickets: bool) -> str:
    """Map current state to the branch of the loop to execute."""
    if not ci_green:
        return HEAL
    if has_tickets:
        return IMPLEMENT
    return IMPROVE


def build_pr_body(
    *,
    ticket: int,
    choice: ModelChoice,
    lesson_note: str,
    lesson_summary: str,
    ci_summary: str,
    kind: str = "",
    references: tuple[str, ...] = (),
    trajectory_digest: str = "",
    recalled: tuple[str, ...] = (),
) -> str:
    """Assemble a PR body that satisfies the traceability invariants.

    Raises if there is no ticket - every PR MUST be linked to one.
    """
    if not ticket:
        raise ValueError("Every PR must be linked to a ticket (traceability invariant).")
    refs = ", ".join(f"`{r}`" for r in references) or "_(none)_"
    artifacts = _phase_artifacts(kind) if kind else ""
    artifacts_section = f"\n## Phase artifacts\n{artifacts}\n" if artifacts else ""
    # Which prior notes the worker was shown - retrieval is only trustworthy if
    # it is auditable after the fact.
    recalled_links = "\n".join(f"- [[{n}]]" for n in recalled)
    recalled_section = (
        f"\n## Prior lessons consulted\n{recalled_links}\n" if recalled else ""
    )
    # What the run cost and how it ended, visible on the PR itself - the full
    # record ships beside it under knowledge/trajectories/.
    traj_section = (
        f"\n## Trajectory\n{trajectory_digest}\n" if trajectory_digest else ""
    )
    return f"""Closes #{ticket}

## Model used
- **model**: `{choice.model}` (tier: `{choice.tier}`)
- **selection**: {choice.rationale} [strategy: `{choice.strategy}`]{artifacts_section}{traj_section}

## CI
{ci_summary}

## Lesson learned
{lesson_summary}

See [[{lesson_note}]] in the knowledge base.
{recalled_section}
## Reference-set evidence
{refs}

---
_Filed automatically by the `hsai` loop. Model usage is on the Claude subscription (no metered API)._
"""


@dataclass
class IterationResult:
    kind: str
    ticket: int | None = None
    pr: int | None = None
    model: str = ""
    ci_before: bool = False
    ci_after: bool = False
    merged: bool = False
    remote: str = ""
    recovered: bool = False
    #: The :mod:`hsai.failures` class this iteration ended in ("" when clean).
    failure_class: str = ""
    lesson_path: str = ""
    recalled: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def describe(self) -> str:
        parts = [
            f"kind={self.kind}",
            f"ticket={self.ticket}",
            f"pr={self.pr}",
            f"model={self.model}",
            f"ci:{self.ci_before}->{self.ci_after}",
            f"remote={self.remote or '-'}",
            f"merged={self.merged}",
            f"recovered={self.recovered}",
            f"failure={self.failure_class or '-'}",
        ]
        return "iteration(" + ", ".join(parts) + ")"


PREVIOUS_ATTEMPT_HEADING = "Previous attempt failed"


def _previous_attempt_section(excerpt: str) -> str:
    """Render the bounded 'Previous attempt failed' block for a retry prompt.

    A retry used to be byte-identical to the attempt that just failed, so the
    second worker rediscovered the failure at full price and often reproduced
    it. The excerpt comes from the previous attempt's trajectory record, is
    already redacted and clipped by
    :meth:`hsai.trajectory.Trajectory.failure_excerpt`, and is framed as
    evidence rather than instruction so it cannot displace the ticket.
    """
    if not excerpt:
        return ""
    return (
        f"\n\n## {PREVIOUS_ATTEMPT_HEADING}\n"
        "This ticket has been attempted before and the attempt did not merge. "
        "Evidence from that run:\n\n"
        f"{excerpt}\n\n"
        "Diagnose why that happened before you start, and do not repeat it. "
        "The ticket above is still the specification."
    )


def _task_prompt(
    kind: str,
    cfg: CoreConfig,
    ticket_title: str,
    ticket_body: str,
    lessons: str = "",
    previous_failure: str = "",
) -> str:
    goals = "; ".join(f"{g.get('id')}:{g.get('title')}" for g in cfg.goals)
    common = (
        "You are a worker in the hsai autonomous loop for ai-hyperswarm-proto-core. "
        "Work only inside this worktree. Make the smallest correct change, keep the "
        "code style consistent, and ensure `ruff check .` and `pytest` both pass. "
        f"Project goals: {goals}."
    )
    # Retrieved prior notes, if any: appended last so the ticket stays the
    # instruction and the lessons stay context (empty string => nothing renders).
    # The previous-attempt evidence goes after them, closest to the work.
    recalled = f"\n\n{lessons}" if lessons else ""
    tail = recalled + _previous_attempt_section(previous_failure)
    if kind == HEAL:
        return (
            f"{common}\nThe build is RED. Diagnose and fix it so CI is green. "
            f"Ticket: {ticket_title}\n{ticket_body}{tail}"
        )
    if kind == IMPLEMENT:
        return (
            f"{common}\nImplement this ticket END TO END. Satisfy EVERY checkbox in "
            "its Acceptance criteria and execute its Verification plan, adding tests "
            "as evidence. A knowledge-only or docs-only diff on a feat/skill/refactor "
            "ticket is an automatic failure - real code must change.\n"
            f"Ticket: {ticket_title}\n{ticket_body}{tail}"
        )
    return (
        f"{common}\nImplement this self-improvement, learning from the reference set "
        f"pinned in .ai-swarm/core.yaml.\nTicket: {ticket_title}\n{ticket_body}{tail}"
    )


def _requires_code(ticket_title: str) -> bool:
    """Tickets whose titles promise code must produce code."""
    lowered = ticket_title.strip().lower()
    return lowered.startswith(("feat:", "fix:", "skill:", "refactor:", "perf:", "test:"))


def _improvement_idea(cfg: CoreConfig) -> tuple[str, str]:
    """Pick one improvement toward the goals when the backlog is empty.

    v0: deterministic, evidence-anchored suggestion. Improving this selection is
    itself a tracked skill (see seeded backlog).
    """
    title = "chore: refresh reference-set snapshot and extract one practice"
    body = (
        "Backlog is empty. Toward goal G1, revisit the pinned reference set in "
        ".ai-swarm/core.yaml, pick ONE concrete practice observed in those "
        "projects (code, CI, or issue-handling), and adopt a small version of it "
        "here. Cite the source project in the lesson.\n\n"
        "Reference set: "
        + ", ".join(r.repo for r in cfg.reference_top10)
    )
    return title, body


def run_once(
    cfg: CoreConfig,
    *,
    repo_dir: str,
    dry_run: bool = False,
    runner: Runner = run,
    ai_runner: Runner = run,
    iteration: int = 0,
    demote_tier: bool = False,
) -> IterationResult:
    """Execute a single iteration of the loop.

    ``demote_tier`` is set by the caller's budget gate on a soft breach: it
    biases model selection one tier cheaper so a block that is burning quota
    keeps progressing instead of halting. Regardless of it, every iteration that
    runs a model appends a cost record to the quota ledger.
    """
    repo = cfg.repo_slug
    login = github.current_login(runner=runner) if not dry_run else "hsai-bot"
    started = time.time()
    # Wall-clock per phase; folded into the trajectory at every terminal exit.
    phases: dict[str, float] = {}
    # Everything this iteration observes that could explain a failure. Filled
    # in as we go, reduced to exactly one class by `failures.classify`.
    signals = failures.Signals()

    # 1. sync main + fresh worktree (serialized: touches shared .git)
    # Branch name is unique per worker even within the same second.
    branch = f"hsai/iter-{int(time.time())}-{iteration}-{uuid4().hex[:6]}"
    if not dry_run:
        with _SERIAL:
            gitops.sync_main(cfg.default_branch, cwd=repo_dir, runner=runner)
            _, wt = gitops.create_worktree(
                cfg.worktrees_dir, branch,
                base=f"origin/{cfg.default_branch}", cwd=repo_dir, runner=runner,
            )
    else:
        wt = repo_dir

    # 2. CI check (skipped in dry-run so we never recurse into a real build)
    ci_started = time.time()
    ci_before = (
        ci.run_local(cwd=wt, runner=runner)
        if not dry_run
        else ci.CIResult(ok=True, steps={}, log="dry-run")
    )
    phases["ci_before"] = round(max(0.0, time.time() - ci_started), 3)

    # 3. choose work + claim a ticket (serialized so workers never collide)
    ticket_num: int | None = None
    ticket_title = ""
    ticket_body = ""
    claimed_issue: github.Issue | None = None

    if dry_run:
        kind = decide_path(ci_before.ok, has_tickets=False)
        if kind == IMPROVE:
            ticket_title, ticket_body = _improvement_idea(cfg)
        elif kind == HEAL:
            ticket_title, ticket_body = "ci: main is red - auto-heal", "dry-run"
    idle_reason = ""
    if not dry_run:
        with _SERIAL:
            all_open = github.list_open_issues(repo, runner=runner)
            # Consider only UNASSIGNED, non-blocked, non-review tickets.
            candidates = [
                i for i in all_open
                if not i.assignees and not i.is_blocked
                and "review" not in i.labels and NEEDS_REFINEMENT not in i.labels
            ]
            # Quality gate: vague tickets are refused, not implemented badly.
            open_unassigned = []
            for i in candidates:
                wf = issue_well_formed(i)
                if wf.ok:
                    open_unassigned.append(i)
                else:
                    github.edit_labels(repo, i.number, add=[NEEDS_REFINEMENT], runner=runner)
            kind = decide_path(ci_before.ok, has_tickets=bool(open_unassigned))
            if kind == HEAL:
                ticket_title = "ci: main is red - auto-heal"
                ticket_body = (
                    f"CI failing on {cfg.default_branch}.\n\n```\n{ci_before.summary()}\n```"
                )
                ticket_num = github.create_issue(
                    repo, ticket_title, ticket_body, ["priority:P0", "ci", "hsai"],
                    assignee=login, runner=runner,
                )
            elif kind == IMPLEMENT:
                top = open_unassigned[0]
                ticket_num, ticket_title, ticket_body = top.number, top.title, top.body
                claimed_issue = top
                github.assign(repo, top.number, login, runner=runner)
            else:  # IMPROVE - file a ticket FIRST so the PR has one
                ticket_title, ticket_body = _improvement_idea(cfg)
                # Dedupe: never spam the backlog with copies of the same idea
                # (a stranded run once filed nine identical chore tickets).
                existing = next((i for i in all_open if i.title == ticket_title), None)
                if existing is not None:
                    if existing.assignees or existing.is_blocked:
                        idle_reason = (
                            f"idle: improvement #{existing.number} already in flight/blocked"
                        )
                    else:
                        ticket_num = existing.number
                        ticket_body = existing.body
                        claimed_issue = existing
                        github.assign(repo, existing.number, login, runner=runner)
                else:
                    ticket_num = github.create_issue(
                        repo, ticket_title, ticket_body,
                        ["priority:P3", "self-improve", "hsai"],
                        assignee=login, runner=runner,
                    )

    if idle_reason:
        res = IterationResult(kind=kind, ci_before=ci_before.ok)
        res.notes.append(idle_reason)
        gitops.remove_worktree(wt, cwd=repo_dir, runner=runner)
        return res

    # 4a. Retry policy. A ticket that failed before carries its diagnosis as a
    # `failure:<class>` label; `execution.retry_policy` turns that class into
    # the action this attempt should take (cheaper tier, longer timeout, and/or
    # the previous attempt's evidence in the prompt). The stale label is
    # cleared on claim so this attempt's verdict is unambiguous.
    prior_failure = (
        failures.class_from_labels(claimed_issue.labels) if claimed_issue else ""
    )
    action = failures.action_for(prior_failure, cfg.retry_policy)
    if claimed_issue and not dry_run:
        stale = failures.failure_labels(claimed_issue.labels)
        if stale:
            github.edit_labels(repo, claimed_issue.number, remove=stale, runner=runner)

    # 4b. model selection (recorded for audit); a soft budget breach - or a
    # `demote_tier` retry action - biases it one tier cheaper.
    task = Task(kind=kind, title=ticket_title, body=ticket_body, labels=(
        tuple(claimed_issue.labels) if claimed_issue else ()
    ))
    choice = select(task, cfg, demote=demote_tier or action.demote)

    # Read side of the knowledge base: pull the most relevant prior notes for
    # this ticket out of the vault in the worktree. Computed before the agent
    # runs (and regardless of dry-run) so the lesson can record what was shown.
    recalled = recall.for_task(
        wt, cfg, title=ticket_title, body=ticket_body, kind=kind
    )

    result = IterationResult(
        kind=kind, ticket=ticket_num, model=choice.model, ci_before=ci_before.ok,
        recalled=list(recalled.note_names),
    )
    if recalled.notes:
        result.notes.append(f"recalled {len(recalled.notes)} prior note(s)")

    # The previous attempt's record, when the policy says this class deserves
    # remediation. Reading it costs one file read and no quota.
    previous_failure = ""
    if prior_failure:
        result.notes.append(f"retry policy: {prior_failure} -> {action.name}")
        if action.remediate:
            prev = trajectory.latest_for_ticket(repo_dir, ticket_num)
            if prev is not None:
                previous_failure = prev.failure_excerpt()
                result.notes.append(
                    f"remediating from trajectory {prev.identifier} ({prior_failure})"
                )
    prompt = _task_prompt(
        kind, cfg, ticket_title, ticket_body, recalled.section, previous_failure
    )
    # `escalate_timeout`: one more shot at a ticket that ran out of clock.
    agent_timeout = cfg.agent_timeout
    if action.escalate and agent_timeout:
        agent_timeout = float(agent_timeout) * TIMEOUT_ESCALATION
        result.notes.append(f"escalated agent timeout to {agent_timeout:.0f}s")

    # Quota ledger: every iteration that runs a model appends one cost record.
    # Written to the repo root (not the ephemeral worktree) so the block-level
    # aggregate and budget gate can read across iterations; the governance PR
    # later commits it so the economics stay auditable.
    attempts = (claimed_issue.attempts() if claimed_issue else 0) + 1
    block = iteration // 100
    tokens: tuple[int, int] | None = None
    traj: trajectory.Trajectory | None = None

    def _classify(outcome: str) -> failures.FailureClass:
        """Reduce everything observed so far to one class, for ``outcome``."""
        signals.failed = outcome not in _CLEAN_OUTCOMES
        return failures.classify(signals)

    def _record_cost(outcome: str) -> None:
        # Every terminal path passes through here, so it is also where the
        # trajectory learns how its run ended and what it is classified as.
        fclass = _classify(outcome)
        result.failure_class = fclass.name
        if traj is not None:
            traj.outcome = outcome
            traj.failure_class = fclass.name
            traj.failure_reason = fclass.reason
            phases["total"] = round(max(0.0, time.time() - started), 3)
            traj.phases = dict(phases)
            trajectory.write(traj, repo_dir)
        ledger.append_record(
            ledger.ledger_path(cfg, repo_dir),
            ledger.LedgerRecord(
                iteration=iteration,
                block=block,
                ticket=ticket_num,
                kind=kind,
                tier=choice.tier,
                model=choice.model,
                wall_clock_seconds=round(max(0.0, time.time() - started), 3),
                attempts=attempts,
                outcome=outcome,
                input_tokens=tokens[0] if tokens else None,
                output_tokens=tokens[1] if tokens else None,
                failure_class=fclass.name,
            ),
        )

    def _abort(outcome: str, *, remote: str, pr: int = 0) -> None:
        """Close out a failed iteration: classify it, then apply the policy."""
        fclass = _classify(outcome)
        _recover_failed(
            cfg, repo, pr, kind=kind, ticket_num=ticket_num,
            claimed_issue=claimed_issue, login=login, remote=remote,
            runner=runner, failure_class=fclass.name,
        )
        result.recovered = True
        result.notes.append(f"failure class: {fclass.name} - {fclass.reason}")
        _record_cost(outcome)

    # 5. run the agent (subscription-only)
    agent_ok = True
    agent_err = ""
    reverted_workflows: list[str] = []
    repro_result: repro.ReproResult | None = None
    if dry_run:
        # A dry run makes no model call - and that is itself a fact worth
        # recording, so the block's trajectory set has no holes in it. The
        # record is schema-complete; its agent fields simply say "not run".
        traj = trajectory.record(
            repo_dir,
            iteration=iteration, ticket=ticket_num, kind=kind,
            tier=choice.tier, model=choice.model, prompt=prompt, result=None,
            block=block, branch=branch, strategy=choice.strategy, outcome="dry-run",
        )
        traj.guards = {g: "n/a (dry-run)" for g in ("workflow", "completeness", "repro")}
        traj.ci_before = dict(ci_before.steps)
        result.notes.append(f"trajectory={traj.identifier}")
    else:
        agent_started = time.time()
        ares = ai.run_agent(
            prompt, choice, cfg, cwd=wt, runner=ai_runner, timeout=agent_timeout
        )
        phases["agent"] = round(max(0.0, time.time() - agent_started), 3)
        # Persist the trajectory FIRST: a guard below can return early, and the
        # run that gets aborted is exactly the one worth being able to replay.
        # It lands in the repo root (not the ephemeral worktree) so the block's
        # records accumulate in one place.
        traj = trajectory.record(
            repo_dir,
            iteration=iteration, ticket=ticket_num, kind=kind,
            tier=choice.tier, model=choice.model, prompt=prompt, result=ares,
            block=block, branch=branch, strategy=choice.strategy,
            duration_seconds=phases["agent"],
        )
        traj.ci_before = dict(ci_before.steps)
        result.notes.append(f"trajectory={traj.identifier}")
        agent_ok, agent_err = ares.ok, ares.error
        # Fed from the parsed envelope, not re-parsed from stdout: this is the
        # path that finally populates the ledger's token columns.
        tokens = ledger.parse_tokens(ares.payload)
        signals.agent_ok = ares.ok
        signals.agent_error = ares.error
        # `proc.run` reports a killed child as "timeout after <n>s"; that is the
        # one signal that distinguishes a hang from an ordinary crash.
        signals.agent_timed_out = "timeout after" in (ares.error or "")
        if agent_err:
            agent_err = _format_error_with_context(agent_err, kind, ticket_num)

        # One snapshot of the worktree, shared by all three guards below (and
        # by the trajectory's diffstat) so they cannot disagree about the diff.
        touched = gitops.changed_paths(cwd=wt, runner=runner)
        traj.changed_paths = list(touched[:MAX_RECORDED_PATHS])
        traj.diffstat = trajectory.diffstat(touched)

        # Guard: a task must not change the CI checks, or local and remote CI
        # would diverge (as happened once when a worker added mypy). The edits
        # are reverted AND the iteration stops: a half-reverted diff whose
        # remaining half assumed the new workflow is not worth shipping, and
        # moving the goalposts is a safety event rather than a build error.
        reverted_workflows = [p for p in touched if p.startswith(".github/workflows/")]
        traj.guards["workflow"] = (
            f"tampered: {reverted_workflows}" if reverted_workflows else "clean"
        )
        if reverted_workflows:
            gitops.restore_pathspec(".github/workflows", cwd=wt, runner=runner)
            signals.workflow_paths = tuple(reverted_workflows)
            result.notes.append(f"reverted workflow edits: {reverted_workflows}")
            _abort("workflow_tamper", remote="WORKFLOW_TAMPER")
            gitops.remove_worktree(wt, cwd=repo_dir, runner=runner)
            return result

        # Completeness guard: a code ticket (feat/skill/refactor/fix) cannot be
        # satisfied by a knowledge-only diff. PR #17 once "closed" a feature
        # ticket by committing nothing but its own lesson file - never again.
        if _requires_code(ticket_title):
            code_files = [p for p in touched if not p.startswith("knowledge/")]
            signals.completeness_ok = bool(code_files)
            traj.guards["completeness"] = "ok" if code_files else "knowledge-only diff"
            if not code_files:
                result.notes.append("completeness guard: knowledge-only diff on a code ticket")
                _abort("incomplete", remote="INCOMPLETE")
                gitops.remove_worktree(wt, cwd=repo_dir, runner=runner)
                return result
        else:
            traj.guards["completeness"] = "n/a (not a code ticket)"

        # Reproduce-before-fix guard: heal/bugfix tickets must add or modify a
        # test that FAILS on the pre-fix (parent) tree and PASSES on the fix
        # branch, proving the bug was real (llama_index's fix-stream
        # discipline). Docs/chore tickets are exempt.
        if repro.requires_repro_guard(kind, ticket_title):
            base_ref = gitops.merge_base(
                "HEAD", f"origin/{cfg.default_branch}", cwd=wt, runner=runner,
            ) or f"origin/{cfg.default_branch}"
            repro_result = repro.check_repro(
                repo_root=repo_dir, wt=wt, base_ref=base_ref,
                test_files=repro.changed_test_files(touched),
                worktrees_dir=cfg.worktrees_dir, runner=runner,
            )
            signals.repro_ok = repro_result.ok
            traj.guards["repro"] = repro_result.reason
            result.notes.append(f"repro guard: {repro_result.reason}")
            if not repro_result.ok:
                _abort("no_repro", remote="NO_REPRO")
                gitops.remove_worktree(wt, cwd=repo_dir, runner=runner)
                return result
        else:
            traj.guards["repro"] = "n/a (not a heal/bugfix ticket)"

    # 6. re-check CI
    ci_started = time.time()
    ci_after = ci.run_local(cwd=wt, runner=runner) if not dry_run else ci_before
    phases["ci_after"] = round(max(0.0, time.time() - ci_started), 3)
    result.ci_after = ci_after.ok
    signals.ci_steps = dict(ci_after.steps)
    if traj is not None:
        traj.ci_after = dict(ci_after.steps)

    # 7. lesson (ALWAYS, pass or fail)
    outcome = "pass" if (agent_ok and ci_after.ok) else "fail"
    if traj is not None:
        # The digest quoted below should state what is known now; `_record_cost`
        # refines it to the merge outcome once that is settled.
        traj.outcome = outcome
    kb = KnowledgeBase.from_config(cfg, wt)
    references = tuple(r.repo for r in cfg.reference_top10[:3])
    lesson = Lesson(
        title=f"{kind}: {ticket_title}"[:120],
        outcome=outcome,
        kind=kind,
        context=f"Iteration {iteration}. Ticket #{ticket_num}. CI before: {ci_before.summary()}.",
        what_happened=(
            f"Model `{choice.model}` ({choice.tier}) ran the task. "
            f"Agent ok={agent_ok}. CI after: {ci_after.summary()}."
            + (
                f"\n\nReverted off-spec workflow edits: {reverted_workflows}."
                if reverted_workflows else ""
            )
            + (f"\n\nAgent error:\n```\n{agent_err[:800]}\n```" if agent_err else "")
            # The lesson is prose for a human, so it quotes only a digest line
            # and a redacted tail. The full structured record lives beside it
            # under knowledge/trajectories/, replayable with `hsai traj <id>`.
            + (
                f"\n\nTrajectory `{traj.identifier}` digest: {traj.digest()}"
                f"\n\nRedacted tail:\n```\n{traj.excerpt()}\n```"
                if traj else ""
            )
        ),
        lesson=(
            "Change merged cleanly under a green build."
            if outcome == "pass"
            else "Change did not reach green; auto-merge will hold until CI passes. "
            "Investigate the failure captured above before the next attempt."
        ),
        iteration=iteration,
        ticket=ticket_num,
        model=choice.model,
        references=references,
        repro_evidence=repro.render_evidence(repro_result) if repro_result else "",
        recalled=recalled.note_names,
        # Tagged on the note itself, so the block whitepaper can build its
        # failure-taxonomy table from the vault without re-reading the ledger.
        failure_class=_classify(outcome).name,
    )
    # Each PR commits ONLY its own uniquely-named lesson file. The MOC indexes
    # and whitepapers are regenerated by the serialized `hsai reindex`
    # maintenance step (see cli.cmd_reindex), so parallel PRs never collide on
    # shared, derived index files.
    lesson_path = kb.write_lesson(lesson)
    result.lesson_path = str(lesson_path)
    result.notes.append(f"lesson outcome={outcome}")

    if dry_run:
        result.notes.append("dry-run: skipped commit/push/PR/merge")
        _record_cost(outcome)
        return result

    # 8-11. commit, push, PR (linked + model + lesson), merge on green
    commit_msg = f"{kind}: {ticket_title}\n\nRefs #{ticket_num}\nModel: {choice.model}"
    gitops.commit_all(commit_msg, cwd=wt, runner=runner)
    push = gitops.push_branch(branch, cwd=wt, runner=runner)
    if not push.ok:
        # No branch on origin means no PR is possible; opening one anyway used
        # to yield PR #0 and a nonsense remote poll. A rejected push is almost
        # always a conflict, and a conflict is not fixable by re-running the
        # same prompt - so it blocks rather than burning a second attempt.
        detail = (push.stderr or push.stdout).strip()
        signals.merge_conflict = failures.looks_like_merge_conflict(detail)
        result.notes.append(f"push failed: {detail[:200]}")
        _abort("push_failed", remote="PUSH_FAILED")
        gitops.remove_worktree(wt, cwd=repo_dir, runner=runner)
        return result

    pr_body = build_pr_body(
        ticket=ticket_num or 0,
        choice=choice,
        lesson_note=lesson.note_name(),
        lesson_summary=lesson.lesson,
        ci_summary=ci_after.summary(),
        kind=kind,
        references=references,
        trajectory_digest=traj.digest() if traj else "",
        recalled=recalled.note_names,
    )
    pr_num = github.create_pr(
        repo, branch, f"{kind}: {ticket_title}"[:120], pr_body,
        base=cfg.default_branch, runner=runner,
    )
    result.pr = pr_num

    # 12. Poll the REAL (remote) CI to conclusion BEFORE relying on auto-merge -
    # it is the source of truth for whether the change may merge, and arming
    # auto-merge first (as opposed to gating on this poll) raced GitHub's own
    # merge against our recovery bookkeeping.
    remote = ci.wait_remote(
        pr_num, repo,
        timeout=cfg.ci_remote_timeout, interval=cfg.ci_poll_interval, runner=runner,
    )
    result.remote = remote
    result.notes.append(f"remote CI={remote}")
    signals.remote_ci = remote
    if traj is not None:
        traj.remote_ci = remote

    # Record the true remote outcome in the lesson itself, then push that
    # update so it lands in the knowledge base once the PR merges.
    lesson.remote_ci = remote
    lesson.failure_class = _classify(
        "merged" if remote == ci.SUCCESS else "recovered"
    ).name
    kb.write_lesson(lesson)
    gitops.commit_all(
        f"docs: record remote CI outcome ({remote}) in lesson\n\nRefs #{ticket_num}",
        cwd=wt, runner=runner,
    )
    gitops.push_branch(branch, cwd=wt, runner=runner)

    if remote == ci.SUCCESS:
        github.merge_pr(repo, pr_num, auto=True, runner=runner)
        result.merged = True
        _record_cost("merged")
    else:
        result.merged = False
        _abort("recovered", remote=remote, pr=pr_num)
        result.notes.append("recovered: closed PR, applied retry policy")

    # 13. cleanup worktree
    gitops.remove_worktree(wt, cwd=repo_dir, runner=runner)
    return result


def _recover_failed(
    cfg: CoreConfig,
    repo: str,
    pr_num: int,
    *,
    kind: str,
    ticket_num: int | None,
    claimed_issue: github.Issue | None,
    login: str,
    remote: str,
    runner: Runner,
    failure_class: str = "",
) -> None:
    """A PR did not go green (or never got one): close it, record *why*, and
    apply the action ``execution.retry_policy`` configures for that class.

    Every failed ticket leaves with a ``failure:<class>`` label. That label is
    the channel by which this attempt's diagnosis reaches the next one: the
    claiming worker reads it back, resolves the same action, and adjusts its
    tier, its timeout and its prompt accordingly.

    Two classes block on the spot rather than burning a retry -
    ``workflow_tamper`` (a worker moved the goalposts; that needs a human, not
    another roll of the dice) and ``merge_conflict`` (re-running the same
    prompt cannot resolve it). Blocking does *not* consume an attempt, so the
    ticket arrives at the architect with its retry budget intact.
    """
    if pr_num:
        github.close_pr(
            repo, pr_num,
            comment=(
                f"Remote CI concluded {remote}"
                + (f" (failure class: `{failure_class}`)" if failure_class else "")
                + "; closing per retry policy."
            ),
            delete_branch=True, runner=runner,
        )
    if not ticket_num:
        return

    action = failures.action_for(failure_class, cfg.retry_policy)
    class_label = [failures.label_for(failure_class)] if failure_class else []

    # Determine how many attempts this ticket has already had.
    issue = claimed_issue or github.get_issue(repo, ticket_num, runner=runner)
    prior = issue.attempts() if issue else 0
    nxt = prior + 1

    if action.blocks:
        # Straight to a human, with the retry counter untouched.
        github.edit_labels(
            repo, ticket_num, add=["blocked", *class_label], runner=runner,
        )
    elif nxt >= cfg.max_ticket_attempts:
        github.edit_labels(
            repo, ticket_num, add=["blocked", *class_label],
            remove=[f"attempts:{prior}"] if prior else None, runner=runner,
        )
    else:
        github.edit_labels(
            repo, ticket_num, add=[f"attempts:{nxt}", *class_label],
            remove=[f"attempts:{prior}"] if prior else None, runner=runner,
        )
    # Unassigned either way: blocked tickets are skipped by future workers, and
    # a retryable one has to be free for the next worker to claim.
    github.unassign(repo, ticket_num, login, runner=runner)


def run_loop(
    cfg: CoreConfig,
    *,
    repo_dir: str,
    max_iterations: int = 1,
    dry_run: bool = False,
    runner: Runner = run,
    ai_runner: Runner = run,
) -> list[IterationResult]:
    results: list[IterationResult] = []
    for i in range(max_iterations):
        results.append(
            run_once(
                cfg, repo_dir=repo_dir, dry_run=dry_run,
                runner=runner, ai_runner=ai_runner, iteration=i + 1,
            )
        )
    return results
