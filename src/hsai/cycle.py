"""One half-day governance block.

    synthesize (if backlog is thin) -> N sequential implementations ->
    block whitepaper -> persona articles -> DIRECTION.md refresh ->
    review issue -> governance PR

Sequential on purpose: cycles run on the architect's machine, and five workers
at once degrade it. Parallelism remains available via `hsai loop` for
supervised bursts.

Every side-effecting step goes through :func:`hsai.journal.once`, so a block
that dies mid-flight can be resumed (`hsai cycle --resume`) without re-filing a
ticket, re-spending quota on a whitepaper, or re-opening a review issue. See
:mod:`hsai.journal` for the durability contract.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from . import github, gitops, journal, ledger, trajectory
from .ai import run_agent
from .config import CoreConfig
from .governance import BlockReport, open_review_issue, write_direction
from .knowledge import KnowledgeBase
from .ledger import BudgetDecision
from .models import ModelChoice
from .orchestrator import IterationResult, run_once
from .proc import Runner, run
from .synthesis import synthesize
from .tickets import issue_well_formed


@dataclass
class CycleResult:
    report: BlockReport
    review_issue: int = 0
    governance_pr: int = 0
    journal_path: str = ""
    resumed: bool = False


def _well_formed_backlog(cfg: CoreConfig, *, runner: Runner) -> int:
    issues = [
        i for i in github.list_open_issues(cfg.repo_slug, runner=runner)
        if not i.assignees and not i.is_blocked and issue_well_formed(i).ok
        and cfg.governance.get("review_label", "review") not in i.labels
    ]
    return len(issues)


def _persona_articles(
    cfg: CoreConfig,
    kb: KnowledgeBase,
    whitepaper_note: str,
    *,
    repo_root: Path,
    ai_runner: Runner,
) -> list[str]:
    """Generate one targeted article per persona from the block whitepaper."""
    if not whitepaper_note:
        return []
    paper_path = kb.whitepapers_dir / f"{whitepaper_note}.md"
    if not paper_path.exists():
        return []
    paper = paper_path.read_text()
    articles_dir = repo_root / "knowledge" / "articles"
    articles_dir.mkdir(parents=True, exist_ok=True)
    tier = "standard" if "standard" in cfg.tiers else cfg.default_tier
    choice = ModelChoice(
        tier=tier, model=cfg.tiers[tier].model,
        rationale="persona article generation", strategy="cycle-v1",
    )
    written: list[str] = []
    for persona in cfg.personas:
        pid = persona.get("id", "reader")
        audience = persona.get("audience", "")
        prompt = (
            f"Rewrite the following engineering whitepaper as a short article "
            f"(<= 500 words) for this audience: {audience}. Keep it concrete and "
            f"honest - include what failed, not just wins. Output ONLY the "
            f"article markdown, starting with a # title.\n\n{paper[:6000]}"
        )
        ares = run_agent(prompt, choice, cfg, runner=ai_runner, timeout=600)
        if ares.ok and ares.output.strip():
            out = articles_dir / f"{whitepaper_note}-{pid}.md"
            out.write_text(
                f"---\ntags:\n  - article\n  - persona/{pid}\n---\n\n"
                + ares.output.strip() + "\n"
            )
            written.append(str(out.relative_to(repo_root)))
    return written


def resolve_cycle_index(
    repo_root: Path, cycle_index: int | None, *, resume: bool, dry_run: bool = False
) -> int:
    """Pick the block to run: explicit index, else the resume target, else now.

    ``--resume`` with no index means "the most recent block whose journal was
    never closed". With no such journal there is nothing to resume, so we derive
    a fresh index and the run behaves exactly like an ordinary one.
    """
    if cycle_index is not None:
        return cycle_index
    if resume:
        found = journal.latest_resumable(repo_root, dry_run=dry_run)
        if found is not None:
            return found
    return int(time.time()) // 43200  # half-days


def _iteration_payload(res: IterationResult) -> dict:
    """The journaled shape of one implementation - enough to rebuild the brief."""
    return {
        "describe": res.describe(),
        "pr": res.pr,
        "merged": bool(res.merged),
        "recovered": bool(res.recovered),
    }


def _synthesis_step(
    cfg: CoreConfig, *, idx: int, runner: Runner, ai_runner: Runner, dry_run: bool
) -> dict:
    """Synthesize tickets when the well-formed backlog is thin (journaled once)."""
    low_water = int(cfg.cycle.get("backlog_low_watermark", 4))
    if dry_run or _well_formed_backlog(cfg, runner=runner) >= low_water:
        return {"ran": False, "filed": [], "error": "", "refused": []}
    sres = synthesize(cfg, cycle_index=idx, runner=runner, ai_runner=ai_runner)
    return {
        "ran": True, "filed": list(sres.filed), "error": sres.error,
        "refused": [r.payload() for r in sres.refused],
    }


def _grade_budget(ledger_file: Path, idx: int, budget: dict) -> dict:
    """Journaled form of the pre-iteration budget verdict.

    Recording the verdict (not just its inputs) is what makes a resume faithful:
    the ledger already holds every completed iteration's cost, so re-grading
    from it on a replay would see the *whole* block's spend before iteration 0
    and halt immediately.
    """
    spent = ledger.aggregate_block(ledger.read_records(ledger_file), idx)
    decision = ledger.evaluate_budget(spent, budget)
    return {"status": decision.status, "reason": decision.reason, "spent": spent.summary()}


def _implementation_block(
    cfg: CoreConfig,
    report: BlockReport,
    jr: journal.Journal | None,
    *,
    idx: int,
    repo_root: Path,
    ledger_file: Path,
    runner: Runner,
    ai_runner: Runner,
    dry_run: bool,
) -> None:
    """Sequential implementations under the quota budget gate.

    Before starting each iteration we grade the block's spend so far: a soft
    breach biases the next selection toward cheaper tiers; a hard breach stops
    starting NEW work (any already-running PR keeps merging - the gate never
    aborts in-flight work and never bypasses the green-merge gate). The hard
    breach is journaled as a terminal record, so resuming the block replays the
    halt instead of restarting work under a breached ceiling.
    """
    block = int(cfg.cycle.get("block_size", 5))
    for i in range(block):
        graded = journal.once(
            jr, "budget", str(i),
            lambda: _grade_budget(ledger_file, idx, cfg.budget),
        )
        decision = BudgetDecision(graded["status"], graded["reason"])
        if decision.halt:
            halted = journal.once(
                jr, "budget_halt", "block",
                lambda graded=graded, i=i: {**graded, "iteration": i},
                status=journal.HALTED,
            )
            report.notes.append(
                f"budget: hard breach after {halted['spent']} - halting new work "
                f"for the block ({halted['reason']})"
            )
            break
        if decision.demote:
            report.notes.append(
                f"budget: soft breach ({decision.reason}) - biasing selection cheaper"
            )
        done = journal.once(
            jr, "iteration", str(i),
            lambda i=i, decision=decision: _iteration_payload(run_once(
                cfg, repo_dir=str(repo_root), runner=runner, ai_runner=ai_runner,
                iteration=idx * 100 + i + 1, demote_tier=decision.demote,
                dry_run=dry_run,
            )),
        )
        report.iterations.append(done["describe"])
        if done["merged"] and done["pr"]:
            report.merged_prs.append(done["pr"])
        elif done["recovered"] and done["pr"]:
            report.recovered_prs.append(done["pr"])


def run_cycle(
    cfg: CoreConfig,
    *,
    repo_dir: str,
    cycle_index: int | None = None,
    resume: bool = False,
    dry_run: bool = False,
    runner: Runner = run,
    ai_runner: Runner = run,
) -> CycleResult:
    """Run (or resume) one governance block.

    ``dry_run`` skips everything that writes to GitHub or spends agent quota -
    synthesis, persona articles, the governance PR and the review issue - and
    threads down into each iteration. It journals into its own file so a
    rehearsal can neither satisfy nor poison a later live run of the block.
    """
    repo_root = Path(repo_dir).resolve()
    idx = resolve_cycle_index(repo_root, cycle_index, resume=resume, dry_run=dry_run)
    jr = journal.open_journal(repo_root, idx, dry_run=dry_run)
    report = BlockReport(cycle_index=idx)

    # 1. Synthesize substantial tickets when the well-formed backlog is thin.
    synth = journal.once(
        jr, "synthesis", "block",
        lambda: _synthesis_step(
            cfg, idx=idx, runner=runner, ai_runner=ai_runner, dry_run=dry_run
        ),
    )
    report.synthesized = list(synth["filed"])
    if synth["ran"] and not report.synthesized:
        report.notes.append(f"synthesis produced no tickets: {synth['error']}")
    # Older journals (pre-refusal-reasons) carry `rejected_titles` instead; a
    # resume of one of those degrades to titles without reasons, never a crash.
    refused = synth.get("refused") or [
        {"title": t, "reason": "duplicate of prior work"}
        for t in synth.get("rejected_titles", [])
    ]
    report.refused = [f"{r['title']} - {r['reason']}" for r in refused]
    if refused:
        report.notes.append(
            f"synthesis: {len(refused)} idea(s) refused by the dedupe gate - "
            f"filed {len(report.synthesized)} survivor(s), no back-fill"
        )

    # 2. Sequential implementation block, under the quota budget gate.
    ledger_file = ledger.ledger_path(cfg, repo_root)
    _implementation_block(
        cfg, report, jr, idx=idx, repo_root=repo_root, ledger_file=ledger_file,
        runner=runner, ai_runner=ai_runner, dry_run=dry_run,
    )

    # Fold the block's ledger records into the summary the review brief surfaces.
    # Derived from the durable ledger, so it needs no journal record of its own.
    report.cost = ledger.aggregate_block(ledger.read_records(ledger_file), idx)

    # Trajectories are local forensics, not repo content: keep the recent blocks
    # replayable and drop the rest so the store stays bounded.
    dropped = journal.once(
        jr, "prune", "block",
        lambda: {"dropped": trajectory.prune(repo_root, cfg.trajectory_retention_blocks)},
    )["dropped"]
    if dropped:
        report.notes.append(f"pruned trajectories for block(s) {dropped}")

    # 3. Sync main so knowledge produced by merged PRs is present locally.
    journal.once(
        jr, "sync", "block",
        lambda: {"branch": _sync_main(cfg, repo_root=repo_root, runner=runner)},
    )

    # 4. Block whitepaper + persona articles + MOCs + DIRECTION refresh.
    kb = KnowledgeBase.from_config(cfg, repo_root)
    report.whitepaper = journal.once(
        jr, "whitepaper", "block", lambda: _whitepaper_step(cfg, kb),
    )["note"]
    report.articles = journal.once(
        jr, "articles", "block",
        lambda: {"paths": [] if dry_run else _persona_articles(
            cfg, kb, report.whitepaper, repo_root=repo_root, ai_runner=ai_runner
        )},
    )["paths"]
    journal.once(
        jr, "direction", "block",
        lambda: _direction_step(cfg, kb, repo_root=repo_root, runner=runner),
    )

    # A resumed block says so in the brief, in one line, before it is rendered.
    if jr.replayed:
        report.notes.append(jr.summary())

    # 5. Governance PR: ship the block artifacts through the same gate as code.
    pr = journal.once(
        jr, "governance_pr", "block",
        lambda: {"number": _governance_pr(
            cfg, report, repo_root=repo_root, runner=runner, jr=jr, dry_run=dry_run
        )},
    )["number"]

    # 6. Review issue - the architect's entrance for this block.
    review = journal.once(
        jr, "review_issue", "block",
        lambda: {"number": 0 if dry_run else open_review_issue(cfg, report, runner=runner)},
    )["number"]

    # Close the journal so `--resume` never picks this block up again.
    journal.once(
        jr, "block", "complete",
        lambda: {"review_issue": review, "governance_pr": pr},
        status=journal.COMPLETE,
    )
    return CycleResult(
        report=report, review_issue=review, governance_pr=pr,
        journal_path=str(jr.path), resumed=jr.resumed,
    )


def _sync_main(cfg: CoreConfig, *, repo_root: Path, runner: Runner) -> str:
    gitops.sync_main(cfg.default_branch, cwd=str(repo_root), runner=runner)
    runner(["git", "merge", "--ff-only", f"origin/{cfg.default_branch}"], cwd=str(repo_root))
    return cfg.default_branch


def _whitepaper_step(cfg: CoreConfig, kb: KnowledgeBase) -> dict:
    """Write the block whitepaper, or record that this block has none.

    The "is there anything to write about" test lives inside the step so a
    resumed run reconstructs the answer from the journal rather than re-deriving
    it from a knowledge base that has moved on.
    """
    if not (cfg.cycle.get("whitepaper_per_block", True) and kb.lesson_notes()):
        return {"note": ""}
    paper = kb.synthesize_whitepaper(n=int(cfg.cycle.get("block_size", 5)))
    kb.write_whitepaper(paper)
    return {"note": paper.note_name()}


def _direction_step(
    cfg: CoreConfig, kb: KnowledgeBase, *, repo_root: Path, runner: Runner
) -> dict:
    """Rebuild the derived indexes: knowledge MOCs, then the steering doc."""
    mocs = [str(p) for p in kb.reindex_mocs()]
    path = write_direction(cfg, repo_root=repo_root, runner=runner)
    return {"path": str(path), "mocs": mocs}


def _governance_pr(
    cfg: CoreConfig,
    report: BlockReport,
    *,
    repo_root: Path,
    runner: Runner,
    jr: journal.Journal | None = None,
    dry_run: bool = False,
) -> int:
    """Commit block artifacts (whitepaper, articles, MOCs, DIRECTION) via PR.

    The ticket is journaled separately from the PR: a crash between filing the
    ticket and opening the PR must not file a second ticket on resume, and the
    ticket-per-PR invariant means the PR body has to reference the same number.
    """
    if dry_run or not gitops.has_changes(cwd=str(repo_root), runner=runner):
        return 0
    ticket = journal.once(
        jr, "governance_ticket", "block",
        lambda: {"number": github.create_issue(
            cfg.repo_slug,
            f"chore: governance artifacts for block {report.cycle_index}",
            "Whitepaper, persona articles, MOC reindex, and DIRECTION refresh for "
            f"block {report.cycle_index}. Filed automatically by `hsai cycle`.",
            ["hsai", "priority:P2"],
            runner=runner,
        )},
    )["number"]
    branch = f"hsai/cycle-{report.cycle_index}-{int(time.time())}"
    runner(["git", "checkout", "-b", branch], cwd=str(repo_root))
    gitops.commit_all(
        f"chore: governance artifacts for block {report.cycle_index}\n\nRefs #{ticket}",
        cwd=str(repo_root), runner=runner,
    )
    gitops.push_branch(branch, cwd=str(repo_root), runner=runner)
    pr = github.create_pr(
        cfg.repo_slug, branch,
        f"chore: governance artifacts for block {report.cycle_index}",
        f"Closes #{ticket}\n\n## Model used\n- `n/a` - deterministic block artifacts"
        f" (whitepaper/articles generated within the block)\n\n## CI\npre-flight local"
        f"\n\n## Lesson learned\nBlock artifacts ship through the same gate as code.\n",
        base=cfg.default_branch, runner=runner,
    )
    if pr:
        github.merge_pr(cfg.repo_slug, pr, auto=True, runner=runner)
    runner(["git", "checkout", cfg.default_branch], cwd=str(repo_root))
    return pr
