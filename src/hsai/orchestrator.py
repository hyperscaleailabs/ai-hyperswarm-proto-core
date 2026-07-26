"""The hsai autonomous loop.

One iteration = sync main -> CI check -> choose work (heal / implement /
self-improve) -> ensure a ticket exists -> run a model -> re-check CI ->
push -> poll remote CI (gh checks) as the pre-merge gate -> record a lesson ->
open a linked PR -> merge only if the remote gate passed.

Decision logic (:func:`decide_path`) and PR-body assembly
(:func:`build_pr_body`) are pure and unit-tested; the orchestration around them
performs the real side effects through the wrapper modules.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from uuid import uuid4

from . import ai, ci, github, gitops
from .config import CoreConfig
from .knowledge import KnowledgeBase, Lesson
from .models import ModelChoice, Task, select
from .proc import Runner, run

HEAL = "heal"
IMPLEMENT = "implement"
IMPROVE = "improve"

# Serializes the short git-metadata prologue and the ticket claim so parallel
# workers (threads) never race on git's index lock or grab the same ticket. The
# slow work (agent run, CI, push, PR, merge) happens outside this lock.
_SERIAL = threading.Lock()


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
    references: tuple[str, ...] = (),
    remote_ci: str = "",
) -> str:
    """Assemble a PR body that satisfies the traceability invariants.

    Raises if there is no ticket - every PR MUST be linked to one.
    """
    if not ticket:
        raise ValueError("Every PR must be linked to a ticket (traceability invariant).")
    refs = ", ".join(f"`{r}`" for r in references) or "_(none)_"
    remote_line = f"\nremote CI (gh checks): `{remote_ci}`" if remote_ci else ""
    return f"""Closes #{ticket}

## Model used
- **model**: `{choice.model}` (tier: `{choice.tier}`)
- **selection**: {choice.rationale} [strategy: `{choice.strategy}`]

## CI
{ci_summary}{remote_line}

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
    remote_ci: str = ""
    remote_ci_ok: bool | None = None
    merged: bool = False
    lesson_path: str = ""
    notes: list[str] = field(default_factory=list)

    def describe(self) -> str:
        parts = [
            f"kind={self.kind}",
            f"ticket={self.ticket}",
            f"pr={self.pr}",
            f"model={self.model}",
            f"ci:{self.ci_before}->{self.ci_after}",
            f"remote_ci={self.remote_ci or 'n/a'}",
            f"merged={self.merged}",
        ]
        return "iteration(" + ", ".join(parts) + ")"


def _task_prompt(kind: str, cfg: CoreConfig, ticket_title: str, ticket_body: str) -> str:
    goals = "; ".join(f"{g.get('id')}:{g.get('title')}" for g in cfg.goals)
    common = (
        "You are a worker in the hsai autonomous loop for ai-hyperswarm-proto-core. "
        "Work only inside this worktree. Make the smallest correct change, keep the "
        "code style consistent, and ensure `ruff check .` and `pytest` both pass. "
        f"Project goals: {goals}."
    )
    if kind == HEAL:
        return (
            f"{common}\nThe build is RED. Diagnose and fix it so CI is green. "
            f"Ticket: {ticket_title}\n{ticket_body}"
        )
    if kind == IMPLEMENT:
        return f"{common}\nImplement this ticket end to end.\nTicket: {ticket_title}\n{ticket_body}"
    return (
        f"{common}\nImplement this self-improvement, learning from the reference set "
        f"pinned in .ai-swarm/core.yaml.\nTicket: {ticket_title}\n{ticket_body}"
    )


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
) -> IterationResult:
    """Execute a single iteration of the loop."""
    repo = cfg.repo_slug
    login = github.current_login(runner=runner) if not dry_run else "hsai-bot"

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

    if dry_run:
        kind = decide_path(ci_before.ok, has_tickets=False)
        if kind == IMPROVE:
            ticket_title, ticket_body = _improvement_idea(cfg)
        elif kind == HEAL:
            ticket_title, ticket_body = "ci: main is red - auto-heal", "dry-run"
    else:
        with _SERIAL:
            # Only consider UNASSIGNED tickets; claiming = assigning to us.
            open_unassigned = [
                i for i in github.list_open_issues(repo, runner=runner) if not i.assignees
            ]
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
                github.assign(repo, top.number, login, runner=runner)
            else:  # IMPROVE - file a ticket FIRST so the PR has one
                ticket_title, ticket_body = _improvement_idea(cfg)
                ticket_num = github.create_issue(
                    repo, ticket_title, ticket_body, ["priority:P3", "self-improve", "hsai"],
                    assignee=login, runner=runner,
                )

    # 4. model selection (recorded for audit)
    task = Task(kind=kind, title=ticket_title, body=ticket_body)
    choice = select(task, cfg)

    result = IterationResult(
        kind=kind, ticket=ticket_num, model=choice.model, ci_before=ci_before.ok
    )

    # 5. run the agent (subscription-only)
    agent_ok = True
    agent_err = ""
    if not dry_run:
        prompt = _task_prompt(kind, cfg, ticket_title, ticket_body)
        ares = ai.run_agent(
            prompt, choice, cfg, cwd=wt, runner=ai_runner, timeout=cfg.agent_timeout
        )
        agent_ok, agent_err = ares.ok, ares.error

    # 6. re-check CI
    ci_after = ci.run_local(cwd=wt, runner=runner) if not dry_run else ci_before
    result.ci_after = ci_after.ok

    # 7. commit + push the agent's change so GitHub has a commit to check.
    # Remote CI cannot be observed before the branch exists on origin, so this
    # push happens before we poll for the remote gate.
    if not dry_run:
        commit_msg = f"{kind}: {ticket_title}\n\nRefs #{ticket_num}\nModel: {choice.model}"
        gitops.commit_all(commit_msg, cwd=wt, runner=runner)
        gitops.push_branch(branch, cwd=wt, runner=runner)

    # 8. poll remote CI (gh checks) - the ground truth GitHub recorded for this
    # branch, used as an explicit pre-merge gate rather than trusting the local
    # run or GitHub's own auto-merge wait silently.
    if dry_run:
        remote_ci = ""
        remote_ci_ok = True
    else:
        remote_ci = ci.poll_remote_status(repo, branch, runner=runner)
        remote_ci_ok = ci.remote_ok(remote_ci)
    result.remote_ci = remote_ci
    result.remote_ci_ok = remote_ci_ok

    # 9. lesson (ALWAYS, pass or fail) - records the true remote CI outcome
    outcome = "pass" if (agent_ok and ci_after.ok and remote_ci_ok) else "fail"
    kb = KnowledgeBase.from_config(cfg, wt)
    references = tuple(r.repo for r in cfg.reference_top10[:3])
    lesson = Lesson(
        title=f"{kind}: {ticket_title}"[:120],
        outcome=outcome,
        kind=kind,
        context=f"Iteration {iteration}. Ticket #{ticket_num}. CI before: {ci_before.summary()}.",
        what_happened=(
            f"Model `{choice.model}` ({choice.tier}) ran the task. "
            f"Agent ok={agent_ok}. CI after: {ci_after.summary()}. "
            f"Remote CI (gh checks): `{remote_ci or 'n/a'}` "
            f"(gate {'passed' if remote_ci_ok else 'failed'})."
            + (f"\n\nAgent error:\n```\n{agent_err[:800]}\n```" if agent_err else "")
        ),
        lesson=(
            "Change merged cleanly under a green local and remote build."
            if outcome == "pass"
            else "Change did not reach green; merge was withheld until CI passes. "
            "Investigate the failure captured above before the next attempt."
        ),
        iteration=iteration,
        ticket=ticket_num,
        model=choice.model,
        remote_ci=remote_ci,
        references=references,
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
        return result

    # 10. commit + push the lesson note, now that the remote outcome is known
    gitops.commit_all(f"docs: record lesson for {kind} #{ticket_num}", cwd=wt, runner=runner)
    gitops.push_branch(branch, cwd=wt, runner=runner)

    # 11. open the linked PR (ticket + model + lesson), merge only on a full
    # green outcome - agent, local CI, and the remote CI gate all passed
    pr_body = build_pr_body(
        ticket=ticket_num or 0,
        choice=choice,
        lesson_note=lesson.note_name(),
        lesson_summary=lesson.lesson,
        ci_summary=ci_after.summary(),
        references=references,
        remote_ci=remote_ci,
    )
    pr_num = github.create_pr(
        repo, branch, f"{kind}: {ticket_title}"[:120], pr_body,
        base=cfg.default_branch, runner=runner,
    )
    result.pr = pr_num

    if outcome == "pass":
        merge = github.merge_pr(repo, pr_num, auto=True, runner=runner)
        result.merged = merge.ok
        result.notes.append(f"merge queued ok={merge.ok}")
    elif not remote_ci_ok:
        result.notes.append(f"merge withheld: remote CI gate failed ({remote_ci or 'n/a'})")
    else:
        result.notes.append("merge withheld: agent or local CI did not reach green")

    # 12. cleanup worktree (branch lives on until merged/deleted by gh)
    gitops.remove_worktree(wt, cwd=repo_dir, runner=runner)
    return result


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
