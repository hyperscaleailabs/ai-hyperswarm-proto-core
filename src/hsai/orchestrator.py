"""The hsai autonomous loop.

One iteration = sync main -> CI check -> choose work (heal / implement /
self-improve) -> ensure a ticket exists -> run a model -> re-check CI ->
record a lesson -> open a linked PR -> merge on green.

Decision logic (:func:`decide_path`) and PR-body assembly
(:func:`build_pr_body`) are pure and unit-tested; the orchestration around them
performs the real side effects through the wrapper modules.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from . import ai, ci, github, gitops, knowledge
from .config import CoreConfig
from .knowledge import KnowledgeBase, Lesson
from .models import ModelChoice, Task, select
from .proc import Runner, run

HEAL = "heal"
IMPLEMENT = "implement"
IMPROVE = "improve"


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
) -> str:
    """Assemble a PR body that satisfies the traceability invariants.

    Raises if there is no ticket - every PR MUST be linked to one.
    """
    if not ticket:
        raise ValueError("Every PR must be linked to a ticket (traceability invariant).")
    refs = ", ".join(f"`{r}`" for r in references) or "_(none)_"
    return f"""Closes #{ticket}

## Model used
- **model**: `{choice.model}` (tier: `{choice.tier}`)
- **selection**: {choice.rationale} [strategy: `{choice.strategy}`]

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
    lesson_path: str = ""
    notes: list[str] = field(default_factory=list)

    def describe(self) -> str:
        parts = [
            f"kind={self.kind}",
            f"ticket={self.ticket}",
            f"pr={self.pr}",
            f"model={self.model}",
            f"ci:{self.ci_before}->{self.ci_after}",
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

    # 1. sync main + fresh worktree
    branch = f"hsai/iter-{int(time.time())}"
    if not dry_run:
        gitops.sync_main(cfg.default_branch, cwd=repo_dir, runner=runner)
        _, wt = gitops.create_worktree(
            cfg.worktrees_dir, branch, base=cfg.default_branch, cwd=repo_dir, runner=runner
        )
    else:
        wt = repo_dir

    # 2. CI check (skipped in dry-run so we never recurse into a real build)
    ci_before = (
        ci.run_local(cwd=wt, runner=runner)
        if not dry_run
        else ci.CIResult(ok=True, steps={}, log="dry-run")
    )

    # 3. choose work
    tickets = github.list_open_issues(repo, runner=runner) if not dry_run else []
    kind = decide_path(ci_before.ok, bool(tickets))

    ticket_num: int | None = None
    ticket_title = ""
    ticket_body = ""
    labels_priority = "priority:P2"

    if kind == HEAL:
        ticket_title = "ci: main is red - auto-heal"
        ticket_body = f"CI failing on {cfg.default_branch}.\n\n```\n{ci_before.summary()}\n```"
        labels_priority = "priority:P0"
        if not dry_run:
            ticket_num = github.create_issue(
                repo, ticket_title, ticket_body, [labels_priority, "ci", "hsai"],
                assignee=login, runner=runner,
            )
    elif kind == IMPLEMENT:
        top = tickets[0]
        ticket_num, ticket_title, ticket_body = top.number, top.title, top.body
        if not dry_run:
            github.assign(repo, top.number, login, runner=runner)
    else:  # IMPROVE - file a ticket FIRST so the PR has one
        ticket_title, ticket_body = _improvement_idea(cfg)
        if not dry_run:
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

    # 7. lesson (ALWAYS, pass or fail)
    outcome = "pass" if (agent_ok and ci_after.ok) else "fail"
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
            + (f"\n\nAgent error:\n```\n{agent_err[:800]}\n```" if agent_err else "")
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
    )
    lesson_path = kb.write_lesson(lesson)
    if kb.should_write_whitepaper():
        kb.write_whitepaper(
            knowledge.Whitepaper(
                title=f"Synthesis after {len(kb.lesson_notes())} lessons",
                summary="Periodic synthesis of accumulated lessons.",
                body="_Auto-scaffolded; a future iteration should deepen this analysis._",
                covers_lessons=tuple(kb.lesson_notes()),
            )
        )
    kb.reindex_mocs()
    result.lesson_path = str(lesson_path)
    result.notes.append(f"lesson outcome={outcome}")

    if dry_run:
        result.notes.append("dry-run: skipped commit/push/PR/merge")
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
        references=references,
    )
    pr_num = github.create_pr(
        repo, branch, f"{kind}: {ticket_title}"[:120], pr_body,
        base=cfg.default_branch, runner=runner,
    )
    result.pr = pr_num

    merge = github.merge_pr(repo, pr_num, auto=True, runner=runner)
    result.merged = merge.ok
    result.notes.append(f"merge queued ok={merge.ok}")

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
