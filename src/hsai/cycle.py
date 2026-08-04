"""One half-day governance block.

    synthesize (if backlog is thin) -> N sequential implementations ->
    block whitepaper -> persona articles -> DIRECTION.md refresh ->
    review issue -> governance PR

Sequential on purpose: cycles run on the architect's machine, and five workers
at once degrade it. Parallelism remains available via `hsai loop` for
supervised bursts.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from . import github, gitops, ledger, trajectory
from .ai import run_agent
from .config import CoreConfig
from .governance import BlockReport, open_review_issue, write_direction
from .knowledge import KnowledgeBase
from .models import ModelChoice
from .orchestrator import run_once
from .proc import Runner, run
from .synthesis import synthesize
from .tickets import issue_well_formed


@dataclass
class CycleResult:
    report: BlockReport
    review_issue: int = 0
    governance_pr: int = 0


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


def run_cycle(
    cfg: CoreConfig,
    *,
    repo_dir: str,
    cycle_index: int | None = None,
    runner: Runner = run,
    ai_runner: Runner = run,
) -> CycleResult:
    repo_root = Path(repo_dir).resolve()
    idx = cycle_index if cycle_index is not None else int(time.time()) // 43200  # half-days
    report = BlockReport(cycle_index=idx)

    # 1. Synthesize substantial tickets when the well-formed backlog is thin.
    low_water = int(cfg.cycle.get("backlog_low_watermark", 4))
    if _well_formed_backlog(cfg, runner=runner) < low_water:
        sres = synthesize(
            cfg, cycle_index=idx, repo_dir=repo_root, runner=runner, ai_runner=ai_runner
        )
        report.synthesized = sres.filed
        if not sres.ok:
            report.notes.append(f"synthesis produced no tickets: {sres.error}")

    # 2. Sequential implementation block, under the quota budget gate. Before
    # starting each iteration we grade the block's spend so far: a soft breach
    # biases the next selection toward cheaper tiers; a hard breach stops
    # starting NEW work (any already-running PR keeps merging - the gate never
    # aborts in-flight work and never bypasses the green-merge gate).
    ledger_file = ledger.ledger_path(cfg, repo_root)
    block = int(cfg.cycle.get("block_size", 5))
    for i in range(block):
        spent = ledger.aggregate_block(ledger.read_records(ledger_file), idx)
        decision = ledger.evaluate_budget(spent, cfg.budget)
        if decision.halt:
            report.notes.append(
                f"budget: hard breach after {spent.summary()} - halting new work "
                f"for the block ({decision.reason})"
            )
            break
        if decision.demote:
            report.notes.append(
                f"budget: soft breach ({decision.reason}) - biasing selection cheaper"
            )
        res = run_once(
            cfg, repo_dir=str(repo_root), runner=runner, ai_runner=ai_runner,
            iteration=idx * 100 + i + 1, demote_tier=decision.demote,
        )
        report.iterations.append(res.describe())
        if res.merged and res.pr:
            report.merged_prs.append(res.pr)
        elif res.recovered and res.pr:
            report.recovered_prs.append(res.pr)

    # Fold the block's ledger records into the summary the review brief surfaces.
    report.cost = ledger.aggregate_block(ledger.read_records(ledger_file), idx)

    # Trajectories are local forensics, not repo content: keep the recent blocks
    # replayable and drop the rest so the store stays bounded.
    dropped = trajectory.prune(repo_root, cfg.trajectory_retention_blocks)
    if dropped:
        report.notes.append(f"pruned trajectories for block(s) {dropped}")

    # 3. Sync main so knowledge produced by merged PRs is present locally.
    gitops.sync_main(cfg.default_branch, cwd=str(repo_root), runner=runner)
    runner(["git", "merge", "--ff-only", f"origin/{cfg.default_branch}"], cwd=str(repo_root))

    # 4. Block whitepaper + persona articles + MOCs + DIRECTION refresh.
    kb = KnowledgeBase.from_config(cfg, repo_root)
    if cfg.cycle.get("whitepaper_per_block", True) and kb.lesson_notes():
        paper = kb.synthesize_whitepaper(n=block)
        kb.write_whitepaper(paper)
        report.whitepaper = paper.note_name()
        report.articles = _persona_articles(
            cfg, kb, report.whitepaper, repo_root=repo_root, ai_runner=ai_runner
        )
    # Re-derive the adopted-practice registry from the block's lessons before
    # the MOCs are rebuilt, so the coverage table and the index agree.
    kb.write_practices()
    kb.reindex_mocs()
    write_direction(cfg, repo_root=repo_root, runner=runner)

    # 5. Governance PR: ship the block artifacts through the same gate as code.
    pr = _governance_pr(cfg, report, repo_root=repo_root, runner=runner)

    # 6. Review issue - the architect's entrance for this block.
    review = open_review_issue(cfg, report, runner=runner)
    return CycleResult(report=report, review_issue=review, governance_pr=pr)


def _governance_pr(
    cfg: CoreConfig, report: BlockReport, *, repo_root: Path, runner: Runner
) -> int:
    """Commit block artifacts (whitepaper, articles, MOCs, DIRECTION) via PR."""
    if not gitops.has_changes(cwd=str(repo_root), runner=runner):
        return 0
    ticket = github.create_issue(
        cfg.repo_slug,
        f"chore: governance artifacts for block {report.cycle_index}",
        "Whitepaper, persona articles, MOC reindex, and DIRECTION refresh for "
        f"block {report.cycle_index}. Filed automatically by `hsai cycle`.",
        ["hsai", "priority:P2"],
        runner=runner,
    )
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
