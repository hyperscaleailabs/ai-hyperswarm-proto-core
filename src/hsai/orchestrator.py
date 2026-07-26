"""The hsai autonomous loop.

One iteration = sync main -> CI check -> choose work (heal / implement /
self-improve) -> ensure a ticket exists -> run a model -> re-check CI ->
record a lesson -> open a linked PR -> merge on green.

Decision logic (:func:`decide_path`), PR-body assembly (:func:`build_pr_body`),
and the off-spec guard (:func:`assess_spec_alignment`) are pure and
unit-tested; the orchestration around them performs the real side effects
through the wrapper modules.
"""
from __future__ import annotations

import re
import threading
import time
from collections.abc import Sequence
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


_EXPECTED_PATH_RE = re.compile(r"`([\w./-]+\.[A-Za-z0-9]+)`")
_TOKEN_RE = re.compile(r"[a-z0-9]+")
# Words too generic (English stopwords, or terms that appear in nearly every
# ticket/path in this repo) to count as evidence of relevance either way.
_SPEC_STOPWORDS = frozenset({
    "the", "a", "an", "and", "or", "to", "of", "in", "for", "on", "with",
    "is", "are", "be", "will", "that", "this", "these", "those", "as", "at",
    "by", "from", "into", "it", "its", "you", "your", "we", "our", "their",
    "not", "but", "if", "so", "then", "end", "one", "via",
    "chore", "feat", "fix", "docs", "ci", "loop", "hsai", "src", "py",
    "tests", "test", "implement", "ticket", "goal", "task", "change",
    "addresses",
})


def _spec_tokens(text: str) -> set[str]:
    return {
        t for t in _TOKEN_RE.findall(text.lower())
        if len(t) > 2 and t not in _SPEC_STOPWORDS
    }


def assess_spec_alignment(
    ticket_title: str, ticket_body: str, changed_paths: Sequence[str],
) -> tuple[bool, str]:
    """Lightweight off-spec guard: does the diff plausibly address the ticket?

    PR #9's worker ignored ticket #4 (reference-set miner) and added a mypy
    step to CI instead - local CI was green, but the change had nothing to do
    with the ticket. This heuristic checks for two signals, cheapest first:

    1. If the ticket body names specific paths (backticked, e.g. `` `src/x.py` ``),
       the diff must touch at least one of them.
    2. Otherwise, the diff's changed paths must share at least one non-generic
       keyword with the ticket title/body (e.g. ``miner`` in both the ticket
       title and a new ``reference_miner.py``).

    This is a heuristic, not a model call, so it is deliberately non-blocking:
    a false positive must never strand a real change. Callers should record
    the result (aligned or not, with the reason) in the lesson/PR rather than
    fail the iteration on it.
    """
    if not changed_paths:
        return False, "agent left no file changes"

    expected = _EXPECTED_PATH_RE.findall(ticket_body)
    if expected:
        if any(p in changed_paths for p in expected):
            return True, f"touches ticket-named path(s): {', '.join(expected)}"
        return False, f"ticket names {', '.join(expected)} but diff touches none of them"

    keywords = _spec_tokens(f"{ticket_title} {ticket_body}")
    if not keywords:
        return True, "ticket has no distinctive keywords to check against"

    path_text = " ".join(changed_paths).replace("/", " ").replace("_", " ").replace(".", " ")
    overlap = keywords & _spec_tokens(path_text)
    if overlap:
        return True, f"keyword overlap with changed paths: {', '.join(sorted(overlap))}"
    return False, "no keyword overlap between ticket text and changed paths"


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
    off_spec: bool = False
    notes: list[str] = field(default_factory=list)

    def describe(self) -> str:
        parts = [
            f"kind={self.kind}",
            f"ticket={self.ticket}",
            f"pr={self.pr}",
            f"model={self.model}",
            f"ci:{self.ci_before}->{self.ci_after}",
            f"off_spec={self.off_spec}",
            f"remote={self.remote or '-'}",
            f"merged={self.merged}",
            f"recovered={self.recovered}",
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
    claimed_issue: github.Issue | None = None

    if dry_run:
        kind = decide_path(ci_before.ok, has_tickets=False)
        if kind == IMPROVE:
            ticket_title, ticket_body = _improvement_idea(cfg)
        elif kind == HEAL:
            ticket_title, ticket_body = "ci: main is red - auto-heal", "dry-run"
    else:
        with _SERIAL:
            # Consider only UNASSIGNED, non-blocked tickets; claiming = assigning.
            open_unassigned = [
                i for i in github.list_open_issues(repo, runner=runner)
                if not i.assignees and not i.is_blocked
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
                claimed_issue = top
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
    reverted_workflows: list[str] = []
    spec_ok, spec_reason = True, "dry-run: not checked"
    if not dry_run:
        prompt = _task_prompt(kind, cfg, ticket_title, ticket_body)
        ares = ai.run_agent(
            prompt, choice, cfg, cwd=wt, runner=ai_runner, timeout=cfg.agent_timeout
        )
        agent_ok, agent_err = ares.ok, ares.error

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

        # Off-spec guard (#13): does the remaining diff plausibly address the
        # ticket, or did the worker drift (as PR #9 did on ticket #4)? A
        # heuristic, so it only flags - it never blocks the PR on its own.
        spec_ok, spec_reason = assess_spec_alignment(
            ticket_title, ticket_body, gitops.changed_paths(cwd=wt, runner=runner),
        )
        result.off_spec = not spec_ok
        result.notes.append(
            f"spec-alignment: {'ok' if spec_ok else 'FLAGGED'} ({spec_reason})"
        )

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
            + (
                f"\n\nReverted off-spec workflow edits: {reverted_workflows}."
                if reverted_workflows else ""
            )
            + (f"\n\nAgent error:\n```\n{agent_err[:800]}\n```" if agent_err else "")
        ),
        lesson=(
            ("Change merged cleanly under a green build." if outcome == "pass"
             else "Change did not reach green; auto-merge will hold until CI passes. "
             "Investigate the failure captured above before the next attempt.")
            + (
                f"\n\nOff-spec guard FLAGGED this change: {spec_reason}. Green CI does "
                "not guarantee the diff addressed the ticket (see PR #9 / ticket #4) - "
                "a human should confirm this PR before trusting the merge."
                if not dry_run and not spec_ok else ""
            )
        ),
        iteration=iteration,
        ticket=ticket_num,
        model=choice.model,
        references=references,
        spec_alignment=(
            "not checked (dry-run)" if dry_run
            else f"{'ok' if spec_ok else 'FLAGGED'}: {spec_reason}"
        ),
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
    """A PR did not go green: close it, and either return the ticket to the
    backlog for another attempt or mark it ``blocked`` for a human."""
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
            )
        )
    return results
