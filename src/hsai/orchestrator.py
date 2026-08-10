"""The hsai autonomous loop.

One iteration = sync main -> CI check -> choose work (heal / implement /
self-improve) -> ensure a ticket exists -> run a model -> re-check CI ->
record a lesson -> open a linked PR -> merge on green.

Decision logic (:func:`decide_path`) and PR-body assembly
(:func:`build_pr_body`) are pure and unit-tested; the orchestration around them
performs the real side effects through the wrapper modules.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from uuid import uuid4

from . import ai, ci, failures, github, gitops, ledger, repro, trajectory
from .config import CoreConfig
from .knowledge import KnowledgeBase, Lesson
from .models import ModelChoice, Task, select
from .proc import Runner, run
from .tickets import NEEDS_REFINEMENT, issue_well_formed

HEAL = "heal"
IMPLEMENT = "implement"
IMPROVE = "improve"

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
) -> str:
    """Assemble a PR body that satisfies the traceability invariants.

    Raises if there is no ticket - every PR MUST be linked to one.
    """
    if not ticket:
        raise ValueError("Every PR must be linked to a ticket (traceability invariant).")
    refs = ", ".join(f"`{r}`" for r in references) or "_(none)_"
    artifacts = _phase_artifacts(kind) if kind else ""
    artifacts_section = f"\n## Phase artifacts\n{artifacts}\n" if artifacts else ""
    # What the run cost and how it ended, visible on the PR itself - the full
    # record stays in the local (gitignored) trajectory store.
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
    lesson_path: str = ""
    failure_class: str = ""
    retry_action: str = ""
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
        ]
        if self.failure_class:
            parts.append(f"failure={self.failure_class}")
        return "iteration(" + ", ".join(parts) + ")"


PREVIOUS_ATTEMPT_HEADER = "## Previous attempt failed"


def _remediation_section(previous: trajectory.Trajectory | None) -> str:
    """The bounded 'Previous attempt failed' block prepended on a retry.

    Reflection before acting again (ChatDev): a second attempt that is handed
    the first one's failure class, red CI step and last steps is materially
    better informed than one that simply re-reads the same ticket.
    """
    if previous is None:
        return ""
    return (
        f"\n\n{PREVIOUS_ATTEMPT_HEADER} - read this before you start, and do not "
        "repeat it. This is evidence from the previous worker on this exact "
        f"ticket, not part of the ticket itself.\n{previous.remediation()}\n"
    )


def _task_prompt(
    kind: str,
    cfg: CoreConfig,
    ticket_title: str,
    ticket_body: str,
    previous: trajectory.Trajectory | None = None,
) -> str:
    goals = "; ".join(f"{g.get('id')}:{g.get('title')}" for g in cfg.goals)
    common = (
        "You are a worker in the hsai autonomous loop for ai-hyperswarm-proto-core. "
        "Work only inside this worktree. Make the smallest correct change, keep the "
        "code style consistent, and ensure `ruff check .` and `pytest` both pass. "
        f"Project goals: {goals}."
    )
    tail = _remediation_section(previous)
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
    phase_seconds: dict[str, float] = {}
    ci_started = time.time()
    ci_before = (
        ci.run_local(cwd=wt, runner=runner)
        if not dry_run
        else ci.CIResult(ok=True, steps={}, log="dry-run")
    )
    phase_seconds["ci_before"] = round(time.time() - ci_started, 3)

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

    # 4. model selection (recorded for audit); a soft budget breach biases it
    # one tier cheaper, and so does a `tier:demote` label left by a previous
    # attempt whose failure class the retry policy answered with demote_tier.
    ticket_labels = tuple(claimed_issue.labels) if claimed_issue else ()
    task = Task(kind=kind, title=ticket_title, body=ticket_body, labels=ticket_labels)
    choice = select(
        task, cfg, demote=demote_tier or failures.DEMOTE_LABEL in ticket_labels
    )

    # The previous failed attempt on this ticket, if any: it feeds both the
    # worker prompt (reflection before acting) and the escalated timeout.
    previous = trajectory.last_failure_for_ticket(repo_dir, ticket_num) if not dry_run else None
    agent_timeout = cfg.agent_timeout
    if failures.ESCALATE_LABEL in ticket_labels and agent_timeout:
        agent_timeout = float(agent_timeout) * 2
        result_note = f"escalated agent timeout to {agent_timeout:.0f}s after a prior timeout"
    else:
        result_note = ""

    result = IterationResult(
        kind=kind, ticket=ticket_num, model=choice.model, ci_before=ci_before.ok
    )
    if result_note:
        result.notes.append(result_note)
    if previous is not None:
        result.notes.append(
            f"retry: informed by trajectory {previous.identifier} "
            f"({previous.failure_class or 'unclassified'})"
        )

    # Quota ledger: every iteration that runs a model appends one cost record.
    # Written to the repo root (not the ephemeral worktree) so the block-level
    # aggregate and budget gate can read across iterations; the governance PR
    # later commits it so the economics stay auditable.
    attempts = (claimed_issue.attempts() if claimed_issue else 0) + 1
    block = iteration // 100
    tokens: tuple[int, int] | None = None
    traj: trajectory.Trajectory | None = None
    guards: dict[str, str] = {}

    def _record_cost(outcome: str) -> None:
        # Every terminal path passes through here, so it is also where the
        # trajectory learns how its run ended and why.
        if traj is not None:
            traj.outcome = outcome
            traj.guards = dict(guards)
            traj.failure_class = result.failure_class
            traj.retry_action = result.retry_action
            traj.remote_ci = result.remote
            traj.phase_seconds = {
                **phase_seconds,
                "total": round(max(0.0, time.time() - started), 3),
            }
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
                failure_class=result.failure_class,
            ),
        )

    def _fail(remote_signal: str, *, pr_num: int = 0, failure_class: str = "", **signals) -> None:
        """Classify this failure (unless already classified), apply the policy.

        ``failure_class`` is passed pre-computed on the remote-CI path, where the
        class has to be known early enough to be written into the lesson.
        """
        result.failure_class = failure_class or failures.classify(failed=True, **signals)
        action = _recover_failed(
            cfg, repo, pr_num, kind=kind, ticket_num=ticket_num,
            claimed_issue=claimed_issue, login=login, remote=remote_signal,
            runner=runner, failure_class=result.failure_class,
        )
        result.retry_action = action.name
        result.recovered = True
        result.notes.append(
            f"failure={result.failure_class} -> retry policy `{action.name}`"
            + ("" if action.consumes_attempt else " (no attempt consumed)")
        )

    # 5. run the agent (subscription-only)
    agent_ok = True
    agent_err = ""
    reverted_workflows: list[str] = []
    repro_result: repro.ReproResult | None = None
    prompt = _task_prompt(kind, cfg, ticket_title, ticket_body, previous=previous)
    if dry_run:
        # A rehearsal still leaves exactly one trajectory - with an empty step
        # stream and exit_status="dry-run", so it is never mistaken for a real
        # run - which is what makes "one record per iteration" a true invariant
        # rather than one that holds only on the live path.
        traj = trajectory.record(
            repo_dir,
            iteration=iteration, ticket=ticket_num, kind=kind,
            tier=choice.tier, model=choice.model, prompt=prompt, result=None,
            block=block, branch=branch, strategy=choice.strategy, outcome="dry-run",
        )
        result.notes.append(f"trajectory={traj.identifier}")
    else:
        agent_started = time.time()
        ares = ai.run_agent(
            prompt, choice, cfg, cwd=wt, runner=ai_runner, timeout=agent_timeout
        )
        phase_seconds["agent"] = round(time.time() - agent_started, 3)
        # Persist the trajectory FIRST: a guard below can return early, and the
        # run that gets aborted is exactly the one worth being able to replay.
        # It lands in the repo root (not the ephemeral worktree) and stays
        # local - only a redacted tail is quoted in the committed lesson.
        traj = trajectory.record(
            repo_dir,
            iteration=iteration, ticket=ticket_num, kind=kind,
            tier=choice.tier, model=choice.model, prompt=prompt, result=ares,
            block=block, branch=branch, strategy=choice.strategy,
            duration_seconds=phase_seconds["agent"],
        )
        traj.local_ci["before"] = dict(ci_before.steps)
        result.notes.append(f"trajectory={traj.identifier}")
        agent_ok, agent_err = ares.ok, ares.error
        # Fed from the parsed envelope, not re-parsed from stdout: this is the
        # path that finally populates the ledger's token columns.
        tokens = ledger.parse_tokens(ares.payload)
        if agent_err:
            agent_err = _format_error_with_context(agent_err, kind, ticket_num)

        # Guard: a task must not change the CI checks, or local and remote CI
        # would diverge (as happened once when a worker added mypy). Revert any
        # workflow edits before they are committed, then stop: tampering with
        # the gate is structural, so the retry policy blocks the ticket
        # immediately rather than letting a second identical attempt burn the
        # remaining retry.
        traj.changed_paths = list(gitops.changed_paths(cwd=wt, runner=runner))
        reverted_workflows = [
            p for p in traj.changed_paths if p.startswith(".github/workflows/")
        ]
        guards["workflow_revert"] = (
            f"tampered: {reverted_workflows}" if reverted_workflows else "clean"
        )
        if reverted_workflows:
            gitops.restore_pathspec(".github/workflows", cwd=wt, runner=runner)
            result.notes.append(f"reverted workflow edits: {reverted_workflows}")
            _fail(
                "WORKFLOW_TAMPER",
                agent_ok=agent_ok, agent_error=agent_err,
                workflow_paths=reverted_workflows,
            )
            _record_cost("workflow_tamper")
            gitops.remove_worktree(wt, cwd=repo_dir, runner=runner)
            return result

        # Completeness guard: a code ticket (feat/skill/refactor/fix) cannot be
        # satisfied by a knowledge-only diff. PR #17 once "closed" a feature
        # ticket by committing nothing but its own lesson file - never again.
        if _requires_code(ticket_title):
            code_files = [p for p in traj.changed_paths if not p.startswith("knowledge/")]
            guards["completeness"] = "ok" if code_files else "knowledge-only diff"
            if not code_files:
                result.notes.append("completeness guard: knowledge-only diff on a code ticket")
                _fail(
                    "INCOMPLETE",
                    agent_ok=agent_ok, agent_error=agent_err, guard="incomplete",
                )
                _record_cost("incomplete")
                gitops.remove_worktree(wt, cwd=repo_dir, runner=runner)
                return result

        # Reproduce-before-fix guard: heal/bugfix tickets must add or modify a
        # test that FAILS on the pre-fix (parent) tree and PASSES on the fix
        # branch, proving the bug was real (llama_index's fix-stream
        # discipline). Docs/chore tickets are exempt.
        if repro.requires_repro_guard(kind, ticket_title):
            base_ref = gitops.merge_base(
                "HEAD", f"origin/{cfg.default_branch}", cwd=wt, runner=runner,
            ) or f"origin/{cfg.default_branch}"
            test_files = repro.changed_test_files(
                gitops.changed_paths(cwd=wt, runner=runner)
            )
            repro_result = repro.check_repro(
                repo_root=repo_dir, wt=wt, base_ref=base_ref,
                test_files=test_files, worktrees_dir=cfg.worktrees_dir, runner=runner,
            )
            guards["repro"] = repro_result.reason
            result.notes.append(f"repro guard: {repro_result.reason}")
            if not repro_result.ok:
                _fail(
                    "NO_REPRO",
                    agent_ok=agent_ok, agent_error=agent_err, guard="no_repro",
                )
                _record_cost("no_repro")
                gitops.remove_worktree(wt, cwd=repo_dir, runner=runner)
                return result

    # 6. re-check CI
    ci_started = time.time()
    ci_after = ci.run_local(cwd=wt, runner=runner) if not dry_run else ci_before
    phase_seconds["ci_after"] = round(time.time() - ci_started, 3)
    result.ci_after = ci_after.ok

    # 7. lesson (ALWAYS, pass or fail)
    outcome = "pass" if (agent_ok and ci_after.ok) else "fail"
    if traj is not None:
        # The digest quoted below should state what is known now; `_record_cost`
        # refines it to the merge outcome once that is settled.
        traj.outcome = outcome
        traj.local_ci["after"] = dict(ci_after.steps)
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
            # Only a digest line plus a redacted tail of the trajectory is
            # committed; the full record stays in the (gitignored) local store,
            # replayable with `hsai traj <iteration>`.
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
    gitops.push_branch(branch, cwd=wt, runner=runner)

    pr_body = build_pr_body(
        ticket=ticket_num or 0,
        choice=choice,
        lesson_note=lesson.note_name(),
        lesson_summary=lesson.lesson,
        ci_summary=ci_after.summary(),
        kind=kind,
        references=references,
        trajectory_digest=traj.digest() if traj else "",
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
    remote_started = time.time()
    remote = ci.wait_remote(
        pr_num, repo,
        timeout=cfg.ci_remote_timeout, interval=cfg.ci_poll_interval, runner=runner,
    )
    phase_seconds["remote_ci"] = round(time.time() - remote_started, 3)
    result.remote = remote
    result.notes.append(f"remote CI={remote}")

    # Classify BEFORE the lesson is rewritten: the named cause is the most
    # useful thing the knowledge base can carry about a failed iteration, and
    # this is the last write of the lesson before the PR is judged.
    remote_failure = (
        ""
        if remote == ci.SUCCESS
        else failures.classify(
            failed=True, agent_ok=agent_ok, agent_error=agent_err,
            ci_steps=ci_after.steps, remote=remote,
            merge_conflict=failures.has_merge_conflict(ci_after.log),
        )
    )

    # Record the true remote outcome in the lesson itself, then push that
    # update so it lands in the knowledge base once the PR merges.
    lesson.remote_ci = remote
    if remote_failure:
        lesson.lesson = (
            f"Classified `{remote_failure}` (see the Failure taxonomy table in this "
            f"block's whitepaper). {lesson.lesson}"
        )
    kb.write_lesson(lesson)
    gitops.commit_all(
        f"docs: record remote CI outcome ({remote}) in lesson\n\nRefs #{ticket_num}",
        cwd=wt, runner=runner,
    )
    gitops.push_branch(branch, cwd=wt, runner=runner)

    # The merge gate is unchanged and unconditional: SUCCESS is the ONLY path to
    # a merge. The failure class steers what happens to the *ticket* afterwards,
    # never whether a red PR may land.
    if remote == ci.SUCCESS:
        github.merge_pr(repo, pr_num, auto=True, runner=runner)
        result.merged = True
    else:
        result.merged = False
        _fail(remote, pr_num=pr_num, failure_class=remote_failure)
        result.notes.append("recovered: closed PR, returned ticket to backlog")

    _record_cost("merged" if result.merged else "recovered")

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
) -> failures.RetryAction:
    """A PR did not go green (or never got one): close it and apply the policy.

    ``retry_policy`` in ``core.yaml`` decides what happens next, keyed by
    ``failure_class``. The ticket is always labelled ``failure:<class>`` and
    always left unassigned, so the next worker (or the architect) can see the
    cause; what varies is whether the failure consumes one of the ticket's
    ``execution.max_ticket_attempts`` retries and what the retry is told.

    Returns the action applied so the caller can record it on the trajectory.
    """
    action = failures.action_for(failure_class, cfg.retry_policy)
    if pr_num:
        github.close_pr(
            repo, pr_num,
            comment=(
                f"Remote CI concluded {remote}; classified `{failure_class or 'unknown'}`, "
                f"closing per retry policy (`{action.name}`)."
            ),
            delete_branch=True, runner=runner,
        )
    if not ticket_num:
        return action

    # Determine how many attempts this ticket has already had.
    issue = claimed_issue or github.get_issue(repo, ticket_num, runner=runner)
    prior = issue.attempts() if issue else 0
    nxt = prior + 1

    add = [failures.failure_label(failure_class or failures.UNKNOWN), *action.labels]
    # Stale routing labels from an earlier attempt must not leak into this one:
    # a timeout followed by a lint slip should escalate nothing.
    remove = [
        lbl for lbl in (failures.ESCALATE_LABEL, failures.DEMOTE_LABEL)
        if issue and lbl in issue.labels and lbl not in add
    ]
    if issue:
        remove += [
            lbl for lbl in issue.labels
            if lbl.startswith(failures.FAILURE_LABEL_PREFIX) and lbl not in add
        ]

    if action.blocks:
        # Structural failure: block now, WITHOUT consuming a retry, so the
        # ticket reaches the architect with its cause and its attempts intact.
        add.insert(0, "blocked")
    else:
        add.insert(0, "blocked" if nxt >= cfg.max_ticket_attempts else f"attempts:{nxt}")
        if prior:
            remove.append(f"attempts:{prior}")
    github.edit_labels(repo, ticket_num, add=add, remove=remove or None, runner=runner)
    # Always unassigned: blocked tickets are skipped by future workers, and a
    # retryable one must be claimable again.
    github.unassign(repo, ticket_num, login, runner=runner)
    return action


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
