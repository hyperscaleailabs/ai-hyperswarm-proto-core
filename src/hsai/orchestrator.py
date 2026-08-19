"""The hsai autonomous loop.

One iteration = sync main -> CI check -> choose work (heal / implement /
self-improve) -> ensure a ticket exists -> run a model -> re-check CI ->
independent review by a different model -> record a lesson -> open a linked PR
-> merge on green.

Decision logic (:func:`decide_path`) and PR-body assembly
(:func:`build_pr_body`) are pure and unit-tested; the orchestration around them
performs the real side effects through the wrapper modules.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from uuid import uuid4

from . import ai, ci, github, gitops, ledger, postmortem, recall, repro, review, trajectory
from .config import CoreConfig
from .knowledge import KnowledgeBase, Lesson
from .models import ModelChoice, Task, select
from .proc import Runner, run
from .tickets import NEEDS_REFINEMENT, issue_well_formed

HEAL = "heal"
IMPLEMENT = "implement"
IMPROVE = "improve"

# Sentinel block index for iterations run outside a governed `hsai cycle`
# (ad-hoc `hsai loop`/`hsai run-once`). Real cycle indices are always >= 0
# (`resolve_cycle_index` derives them from wall-clock epoch // 43200, or an
# explicit `--index`), so a negative sentinel can never collide with one -
# unlike the old `iteration // 100` default, which put every such run in
# block 0 and polluted cycle 0's ledger aggregate with unrelated iterations.
AD_HOC_BLOCK = -1

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
    review_verdict: str = "",
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
    # record stays in the local (gitignored) trajectory store.
    traj_section = (
        f"\n## Trajectory\n{trajectory_digest}\n" if trajectory_digest else ""
    )
    # Who checked the work, not just who wrote it: always rendered, so a PR that
    # skipped the gate says so out loud instead of staying silent about it.
    verdict = review_verdict or "_(no independent review recorded)_"
    return f"""Closes #{ticket}

## Model used
- **model**: `{choice.model}` (tier: `{choice.tier}`)
- **selection**: {choice.rationale} [strategy: `{choice.strategy}`]{artifacts_section}{traj_section}

## CI
{ci_summary}

## Independent review
{verdict}

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
    review: str = ""  # approve | blocked | skipped (independent review gate)
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
            f"review={self.review or '-'}",
            f"remote={self.remote or '-'}",
            f"merged={self.merged}",
            f"recovered={self.recovered}",
        ]
        return "iteration(" + ", ".join(parts) + ")"


def _task_prompt(
    kind: str,
    cfg: CoreConfig,
    ticket_title: str,
    ticket_body: str,
    lessons: str = "",
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
    recalled = f"\n\n{lessons}" if lessons else ""
    if kind == HEAL:
        return (
            f"{common}\nThe build is RED. Diagnose and fix it so CI is green. "
            f"Ticket: {ticket_title}\n{ticket_body}{recalled}"
        )
    if kind == IMPLEMENT:
        return (
            f"{common}\nImplement this ticket END TO END. Satisfy EVERY checkbox in "
            "its Acceptance criteria and execute its Verification plan, adding tests "
            "as evidence. A knowledge-only or docs-only diff on a feat/skill/refactor "
            "ticket is an automatic failure - real code must change.\n"
            f"Ticket: {ticket_title}\n{ticket_body}{recalled}"
        )
    return (
        f"{common}\nImplement this self-improvement, learning from the reference set "
        f"pinned in .ai-swarm/core.yaml.\nTicket: {ticket_title}\n{ticket_body}{recalled}"
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
    block: int | None = None,
    demote_tier: bool = False,
) -> IterationResult:
    """Execute a single iteration of the loop.

    ``demote_tier`` is set by the caller's budget gate on a soft breach: it
    biases model selection one tier cheaper so a block that is burning quota
    keeps progressing instead of halting. Regardless of it, every iteration that
    runs a model appends a cost record to the quota ledger.

    ``block`` identifies which governance block's aggregate this iteration's
    ledger record belongs to. ``hsai cycle`` always passes its own index
    explicitly, and ``hsai loop``/``hsai run-once`` (via :func:`run_loop`)
    always pass :data:`AD_HOC_BLOCK`, since their iteration numbers restart
    from 1 and would otherwise land in block 0 - indistinguishable from (and
    polluting) a real cycle 0. When omitted entirely, this falls back to the
    historical ``iteration // 100`` derivation, so a bare, direct call (as
    every pre-existing test makes) keeps its exact prior behavior.
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
    ci_before = (
        ci.run_local(cwd=wt, runner=runner)
        if not dry_run
        else ci.CIResult(ok=True, steps={}, log="dry-run")
    )

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
    # one tier cheaper.
    task = Task(kind=kind, title=ticket_title, body=ticket_body, labels=(
        tuple(claimed_issue.labels) if claimed_issue else ()
    ))
    choice = select(task, cfg, demote=demote_tier)

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

    # Quota ledger: every iteration that runs a model appends one cost record.
    # Written to the repo root (not the ephemeral worktree) so the block-level
    # aggregate and budget gate can read across iterations; the governance PR
    # later commits it so the economics stay auditable.
    attempts = (claimed_issue.attempts() if claimed_issue else 0) + 1
    block = iteration // 100 if block is None else block
    tokens: tuple[int, int] | None = None
    traj: trajectory.Trajectory | None = None

    def _record_cost(outcome: str, *, failure_class: str = "", failure_detail: str = "") -> None:
        # Every terminal path passes through here, so it is also where the
        # trajectory learns how its run ended.
        if traj is not None:
            traj.outcome = outcome
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
                failure_class=failure_class,
                failure_detail=failure_detail[:200],
                recalled_count=len(recalled.notes),
            ),
        )

    # 5. run the agent (subscription-only)
    agent_ok = True
    agent_err = ""
    reverted_workflows: list[str] = []
    repro_result: repro.ReproResult | None = None
    if not dry_run:
        prompt = _task_prompt(kind, cfg, ticket_title, ticket_body, recalled.section)
        agent_started = time.time()
        ares = ai.run_agent(
            prompt, choice, cfg, cwd=wt, runner=ai_runner, timeout=cfg.agent_timeout
        )
        # Persist the trajectory FIRST: a guard below can return early, and the
        # run that gets aborted is exactly the one worth being able to replay.
        # It lands in the repo root (not the ephemeral worktree) and stays
        # local - only a redacted tail is quoted in the committed lesson.
        traj = trajectory.record(
            repo_dir,
            iteration=iteration, ticket=ticket_num, kind=kind,
            tier=choice.tier, model=choice.model, prompt=prompt, result=ares,
            block=block, duration_seconds=time.time() - agent_started,
        )
        result.notes.append(f"trajectory={traj.identifier}")
        agent_ok, agent_err = ares.ok, ares.error
        # Fed from the parsed envelope, not re-parsed from stdout: this is the
        # path that finally populates the ledger's token columns.
        tokens = ledger.parse_tokens(ares.payload)
        if agent_err:
            agent_err = _format_error_with_context(agent_err, kind, ticket_num)

        # Guard: a task must not change the CI checks, or local and remote CI
        # would diverge (as happened once when a worker added mypy). Revert any
        # workflow edits before they are committed and note it in the lesson.
        reverted_workflows = [
            p for p in gitops.changed_paths(cwd=wt, runner=runner)
            if p.startswith(".github/workflows/")
        ]
        if reverted_workflows:
            gitops.restore_pathspec(".github/workflows", cwd=wt, runner=runner)
            result.notes.append(f"reverted workflow edits: {reverted_workflows}")

        # Completeness guard: a code ticket (feat/skill/refactor/fix) cannot be
        # satisfied by a knowledge-only diff. PR #17 once "closed" a feature
        # ticket by committing nothing but its own lesson file - never again.
        if _requires_code(ticket_title):
            touched = gitops.changed_paths(cwd=wt, runner=runner)
            code_files = [p for p in touched if not p.startswith("knowledge/")]
            if not code_files:
                result.notes.append("completeness guard: knowledge-only diff on a code ticket")
                _recover_failed(
                    cfg, repo, 0, kind=kind, ticket_num=ticket_num,
                    claimed_issue=claimed_issue, login=login,
                    remote="INCOMPLETE", runner=runner,
                )
                result.recovered = True
                _record_cost(
                    "incomplete",
                    failure_class=postmortem.INCOMPLETE_DIFF,
                    failure_detail="knowledge-only diff on a code ticket",
                )
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
            result.notes.append(f"repro guard: {repro_result.reason}")
            if not repro_result.ok:
                _recover_failed(
                    cfg, repo, 0, kind=kind, ticket_num=ticket_num,
                    claimed_issue=claimed_issue, login=login,
                    remote="NO_REPRO", runner=runner,
                )
                result.recovered = True
                _record_cost(
                    "no_repro",
                    failure_class=postmortem.NO_REPRO,
                    failure_detail=repro_result.reason,
                )
                gitops.remove_worktree(wt, cwd=repo_dir, runner=runner)
                return result

    # 6. re-check CI
    ci_after = ci.run_local(cwd=wt, runner=runner) if not dry_run else ci_before
    result.ci_after = ci_after.ok

    # 6b. Independent review gate: a SECOND opinion, from a different tier than
    # the author, on whether the diff actually satisfies the ticket. Every other
    # guard so far only checks the change's shape. The agent's work is committed
    # first because the reviewer reads `git diff <merge-base>...HEAD`; nothing is
    # pushed until the verdict is in, so a blocking verdict never opens a PR.
    verdict = review.skip_review("dry-run: nothing was committed to review")
    if not dry_run:
        commit_msg = f"{kind}: {ticket_title}\n\nRefs #{ticket_num}\nModel: {choice.model}"
        gitops.commit_all(commit_msg, cwd=wt, runner=runner)
        if not ci_after.ok:
            # A red branch is already headed for _recover_failed via remote CI;
            # spending review quota on it would buy nothing.
            verdict = review.skip_review("local CI is red; the CI gate decides this one")
        else:
            base_ref = gitops.merge_base(
                "HEAD", f"origin/{cfg.default_branch}", cwd=wt, runner=runner,
            ) or f"origin/{cfg.default_branch}"
            verdict = review.review_change(
                cfg,
                repo_root=repo_dir, wt=wt, base_ref=base_ref,
                ticket_title=ticket_title, ticket_body=ticket_body,
                author=choice, iteration=iteration, block=block,
                ticket=ticket_num, attempts=attempts,
                runner=runner, ai_runner=ai_runner,
            )
    result.review = verdict.status
    result.notes.append(f"independent review: {verdict.summary()}")

    # Evidence for the failure taxonomy (see hsai.postmortem): everything known
    # about this iteration so far. `remote_ci` is filled in once the PR's real
    # checks conclude (step 12), so callers before that pass it separately.
    def _failure_evidence(remote_ci: str = "") -> postmortem.FailureEvidence:
        return postmortem.FailureEvidence(
            agent_ok=agent_ok, agent_error=agent_err, ci_steps=ci_after.steps,
            review_approved=None if verdict.skipped else verdict.approve,
            remote_ci=remote_ci,
        )

    # 7. lesson (ALWAYS, pass or fail)
    outcome = "pass" if (agent_ok and ci_after.ok) else "fail"
    lesson_failure_class = postmortem.classify(_failure_evidence()) if outcome == "fail" else ""
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
        recalled=recalled.note_names,
        review_verdict=verdict.render(),
        execution_trace=traj.execution_trace() if traj else "",
        failure_class=lesson_failure_class,
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
        if outcome == "fail":
            fclass, detail = postmortem.classify_with_detail(_failure_evidence())
            _record_cost(outcome, failure_class=fclass, failure_detail=detail)
        else:
            _record_cost(outcome)
        return result

    # 7b. A blocking verdict stops here: nothing is pushed, no PR is opened, and
    # the ticket goes back through the SAME retry policy a red PR uses - one
    # attempt spent, ``blocked`` at max_ticket_attempts. No new stall state.
    if not verdict.approve:
        _recover_failed(
            cfg, repo, 0, kind=kind, ticket_num=ticket_num,
            claimed_issue=claimed_issue, login=login,
            remote="REVIEW_BLOCKED", runner=runner,
        )
        result.recovered = True
        result.notes.append("recovered: independent review blocked the change")
        # The lesson was written into a worktree that is about to be discarded
        # unpushed; the durable record of this run is its ledger + trajectory.
        result.lesson_path = ""
        _record_cost(
            "review_blocked",
            failure_class=postmortem.AGENT_ERROR,
            failure_detail="; ".join(verdict.blocking) or "independent review blocked the change",
        )
        gitops.remove_worktree(wt, cwd=repo_dir, runner=runner)
        return result

    # 8-11. commit the lesson, push, PR (linked + model + lesson), merge on green
    gitops.commit_all(
        f"docs: record lesson for {kind}\n\nRefs #{ticket_num}", cwd=wt, runner=runner
    )
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
        recalled=recalled.note_names,
        review_verdict=verdict.render(),
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

    # Record the true remote outcome in the lesson itself, then push that
    # update so it lands in the knowledge base once the PR merges.
    lesson.remote_ci = remote
    kb.write_lesson(lesson)
    gitops.commit_all(
        f"docs: record remote CI outcome ({remote}) in lesson\n\nRefs #{ticket_num}",
        cwd=wt, runner=runner,
    )
    gitops.push_branch(branch, cwd=wt, runner=runner)

    if remote == ci.SUCCESS:
        github.merge_pr(repo, pr_num, auto=True, runner=runner)
        result.merged = True
    else:
        result.merged = False
        _recover_failed(
            cfg, repo, pr_num, kind=kind, ticket_num=ticket_num,
            claimed_issue=claimed_issue, login=login, remote=remote, runner=runner,
        )
        result.recovered = True
        result.notes.append("recovered: closed PR, returned ticket to backlog")

    if result.merged:
        _record_cost("merged")
    else:
        fclass, detail = postmortem.classify_with_detail(_failure_evidence(remote))
        _record_cost("recovered", failure_class=fclass, failure_detail=detail)

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
) -> None:
    """A PR did not go green (or never got one): close it, and either return the
    ticket to the backlog for another attempt or mark it ``blocked``."""
    if pr_num:
        github.close_pr(
            repo, pr_num,
            comment=f"Remote CI concluded {remote}; closing per retry policy.",
            delete_branch=True, runner=runner,
        )
    if not ticket_num:
        return

    # Determine how many attempts this ticket has already had.
    issue = claimed_issue or github.get_issue(repo, ticket_num, runner=runner)
    prior = issue.attempts() if issue else 0
    nxt = prior + 1

    if nxt >= cfg.max_ticket_attempts:
        github.edit_labels(
            repo, ticket_num, add=["blocked"], remove=[f"attempts:{prior}"] if prior else None,
            runner=runner,
        )
        # Leave it unassigned but blocked so no worker retries it.
        github.unassign(repo, ticket_num, login, runner=runner)
    else:
        github.edit_labels(
            repo, ticket_num,
            add=[f"attempts:{nxt}"], remove=[f"attempts:{prior}"] if prior else None,
            runner=runner,
        )
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
                block=AD_HOC_BLOCK,
            )
        )
    return results
