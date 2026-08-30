"""`hsai` command-line entry point.

Commands:
  hsai loop [--iterations N] [--max-parallel M] [--dry-run]   run the auto loop
  hsai run-once [--dry-run]                                    a single iteration
  hsai status                                                  config + backlog snapshot
  hsai cycle [--cycle-index N] [--resume] [--dry-run]          one governance block
  hsai reindex [--root DIR]                                    rebuild knowledge MOCs + notes.json
  hsai observe [--refresh]                                     refresh reference digests + dossiers
  hsai recall "<query>" [--k N] [--kind K]                     rank prior lessons/ADRs
  hsai practices list                                          show the adopted-practice registry
  hsai practices add --title T --source-project P ...          record a new adopted practice
  hsai postmortem [--block N]                                  print the failure-class Pareto for a block
  hsai doctor                                                  verify environment + invariants
  hsai traj <iteration> [--json]                               print a stored agent run
  hsai replay <iteration> [--json]                              alias of `hsai traj`
"""
from __future__ import annotations

import argparse
import os
import sys
import time

from . import (
    __version__,
    ai,
    ledger,
    observatory,
    postmortem,
    practices,
    recall,
    repro,
    retrieval,
    trajectory,
)
from .config import CoreConfig, load_config, validate
from .knowledge import KnowledgeBase
from .orchestrator import run_loop
from .proc import Runner, run
from .swarm import run_parallel


def _load(args: argparse.Namespace) -> CoreConfig:
    return load_config(getattr(args, "config", None))


def cmd_status(args: argparse.Namespace) -> int:
    cfg = _load(args)
    v = validate(cfg)
    print(f"repo:        {cfg.repo_slug}  ({cfg.license}, {cfg.raw.get('identity', {}).get('visibility', '?')})")
    print(f"mission:     {cfg.mission[:80]}...")
    print(f"max_parallel:{cfg.max_parallel}  (proven_at={cfg.proven_at}, target={cfg.ramp_target})")
    print(f"tiers:       {', '.join(f'{k}->{t.model}' for k, t in cfg.tiers.items())}")
    print(f"top10:       {len(cfg.reference_top10)} reference repos pinned")
    print(f"config valid:{v.ok}")
    for w in v.warnings:
        print(f"  war: {w}")
    for e in v.errors:
        print(f"  ERR: {e}")
    return 0 if v.ok else 1


def cmd_doctor(args: argparse.Namespace) -> int:
    cfg = _load(args)
    ok = True
    print(f"hsai {__version__}")
    v = validate(cfg)
    for e in v.errors:
        print(f"  config ERR: {e}")
        ok = False
    # Subscription-only guard (config inspection: would preflight() raise?)
    try:
        ai.preflight(cfg)
        print("  subscription guard: OK (no metered API path)")
    except ai.SubscriptionGuardError as exc:
        print(f"  subscription guard: FAIL - {exc}")
        ok = False
    # Live counterpart: actually spawn a child with the sanitized env and read
    # back what it saw, instead of only trusting our own in-memory dict.
    live_ok, live_msg = ai.check_child_env(cfg)
    print(f"  child-environment guard: {'PASS' if live_ok else 'FAIL'} - {live_msg}")
    if not live_ok:
        ok = False
    print(f"  constraints: subscription_only={cfg.subscription_only}, "
          f"require_ticket_per_pr={cfg.constraints.get('require_ticket_per_pr')}")
    return 0 if ok else 1


def cmd_reindex(args: argparse.Namespace) -> int:
    """Serialized knowledge maintenance: whitepaper cadence, MOC rebuild, and
    the retrieval index the planner reads (`knowledge/index/notes.json`).

    Kept out of the parallel workers so PRs never collide on derived index files.
    """
    cfg = _load(args)
    root = getattr(args, "root", ".")
    kb = KnowledgeBase.from_config(cfg, root)
    if kb.should_write_whitepaper():
        p = kb.write_whitepaper(kb.synthesize_whitepaper())
        print(f"wrote whitepaper {p}")
    written = kb.reindex_mocs()
    for p in written:
        print(f"reindexed {p}")
    index = retrieval.write_index(root, cfg)
    print(f"reindexed {index} ({len(retrieval.note_paths(root, cfg))} note(s))")
    return 0


def cmd_observe(args: argparse.Namespace, *, runner: Runner = run) -> int:
    """Refresh the reference-set digests and dossiers without running a cycle.

    Touches only the observatory's own artifacts: the digest cache, the
    per-project dossiers, and the Reference Set MOC. No ticket, no PR, no model
    call - the point is to make a stale reference set cheap to fix.
    """
    cfg = _load(args)
    root = args.root
    ocfg = observatory.ObservatoryConfig.from_core(cfg)
    directory = observatory.reference_dir(root, cfg)
    repos = [r.repo for r in cfg.reference_top10]

    digests = observatory.observe_all(
        directory, repos, runner=runner, refresh=args.refresh,
        stale_after_days=ocfg.stale_after_days,
        commits=ocfg.commits, readme_bytes=ocfg.readme_bytes,
    )
    for digest in digests:
        delta = digest.delta
        print(f"{digest.repo}: {delta.summary() if delta else 'cached (not re-observed)'}")

    written = KnowledgeBase.from_config(cfg, root).write_reference_dossiers()
    print(f"wrote {len(written)} reference note(s) under {directory}")
    print(
        observatory.stale_report(
            directory, repos, stale_after_days=ocfg.stale_after_days
        ).line()
    )
    return 0


def cmd_recall(args: argparse.Namespace) -> int:
    """Spot-check what a worker would be shown for a given task.

    Pure reading: builds the BM25 index over knowledge/ + docs/adr and prints
    the ranking. No model call, no network, no quota.
    """
    cfg = _load(args)
    corpus = recall.Corpus.load(args.root, cfg)
    if not len(corpus):
        print(f"recall: no indexable notes under {args.root}", file=sys.stderr)
        return 1
    notes = corpus.search(args.query, args.k, kind=args.kind or "")
    if not notes:
        print(f"recall: no match for {args.query!r} in {len(corpus)} note(s)", file=sys.stderr)
        return 1
    for note in notes:
        print(f"{note.score:8.3f}  {note.note_name}  ({note.label()})")
    return 0


def cmd_practices_list(args: argparse.Namespace) -> int:
    """Print the adopted-practice registry (pure reading, no quota spent)."""
    cfg = _load(args)
    records = practices.load(args.root, cfg)
    if not records:
        print("practices: registry is empty")
        return 0
    for p in records:
        pr = f"#{p.adopted_pr}" if p.adopted_pr else "-"
        print(f"{p.id}  [{p.status}]  {p.title}  <- {p.source_project} ({p.source_artifact})  PR {pr}")
    return 0


def cmd_practices_add(args: argparse.Namespace) -> int:
    """Record a new adopted practice - refuses a (source_project, title) duplicate."""
    cfg = _load(args)
    practice = practices.build_practice(
        title=args.title,
        source_project=args.source_project,
        source_artifact=args.source_artifact,
        evidence=args.evidence,
        status=args.status,
        adopted_pr=args.adopted_pr,
        adopted_date=args.adopted_date or "",
        notes=args.notes or "",
    )
    try:
        path = practices.append(args.root, practice, cfg=cfg)
    except practices.DuplicatePracticeError as exc:
        print(f"practices add: refused - {exc}", file=sys.stderr)
        return 1
    print(f"wrote {path}")
    return 0


def cmd_postmortem(args: argparse.Namespace) -> int:
    """Print the failure-class Pareto for one block (spends no quota, no writes)."""
    cfg = _load(args)
    records = ledger.read_records(ledger.ledger_path(cfg, args.root))
    block = args.block
    if block is None:
        seen = {r.block for r in records}
        block = max(seen) if seen else int(time.time()) // 43200
    rows = postmortem.pareto_table(records, block)
    total = sum(r.count for r in rows)
    print(f"postmortem: block {block} - {total} failure(s) across {len(rows)} class(es)")
    print(postmortem.render_pareto_table(rows))
    ratio = float(cfg.postmortem.get("ratio_threshold", postmortem.DEFAULT_RATIO_THRESHOLD))
    min_count = int(cfg.postmortem.get("min_count", postmortem.DEFAULT_MIN_COUNT))
    dominant = postmortem.dominant_failure(rows, ratio_threshold=ratio, min_count=min_count)
    if dominant:
        print(
            f"dominant: `{dominant.failure_class}` ({dominant.count}/{total}, "
            f"{dominant.share:.0%}) clears the trigger (ratio>={ratio:g}, count>={min_count}) "
            "- `hsai cycle` will file (or has filed) a P1 ticket for it"
        )
    else:
        print(f"no class clears the postmortem trigger (ratio>={ratio:g}, count>={min_count})")
    return 0


def cmd_cycle(args: argparse.Namespace) -> int:
    from .cycle import run_cycle

    cfg = _load(args)
    res = run_cycle(
        cfg, repo_dir=".", cycle_index=args.index,
        resume=args.resume, dry_run=args.dry_run,
    )
    print(f"cycle: block {res.report.cycle_index}"
          f"{' (resumed)' if res.resumed else ''}  journal: {res.journal_path}")
    print(f"cycle: synthesized={res.report.synthesized} merged={res.report.merged_prs} "
          f"recovered={res.report.recovered_prs}")
    print(f"whitepaper={res.report.whitepaper or '-'} articles={len(res.report.articles)}")
    print(f"review issue: #{res.review_issue}  governance PR: #{res.governance_pr}")
    for line in res.report.iterations:
        print(f"  {line}")
    return 0


def cmd_synthesize(args: argparse.Namespace) -> int:
    from .synthesis import synthesize

    cfg = _load(args)
    res = synthesize(cfg, cycle_index=args.index)
    print(f"studied: {', '.join(res.studied)}")
    print(f"filed tickets: {res.filed or 'none'}")
    if res.rejected:
        print(f"duplicates rejected: {res.rejected} (matched: {', '.join(res.rejected_titles)})")
    for flag in res.risk_flags:
        print(f"duplicate-risk: {flag}")
    if res.error:
        print(f"error: {res.error}")
    return 0 if res.ok else 1


def cmd_repro_check(args: argparse.Namespace) -> int:
    """Remote-CI counterpart of the orchestrator's reproduce-before-fix guard.

    Runs as a pre-merge gate on GitHub: for heal/bugfix PRs, proves the
    added/modified test fails on ``--base-ref`` (pre-fix) and passes on the
    checked-out PR branch. Exempt tickets (docs/chore, or non heal/bugfix)
    pass immediately.
    """
    cfg = _load(args)
    pr_title = args.pr_title or os.environ.get("PR_TITLE", "")
    result = repro.evaluate_pr(
        pr_title=pr_title, repo_dir=".", base_ref=args.base_ref,
        worktrees_dir=cfg.worktrees_dir,
    )
    print(f"repro-check: {'PASS' if result.ok else 'BLOCKED'} - {result.reason}")
    if result.log:
        print(result.log)
    return 0 if result.ok else 1


def _print_trajectory(args: argparse.Namespace, label: str) -> int:
    """Reconstruct a stored agent run from the local trajectory store.

    Pure reading: no ``claude`` subprocess, no network, no quota spent.
    """
    try:
        traj = trajectory.load(args.root, args.trajectory_id)
    except (FileNotFoundError, ValueError) as exc:
        print(f"{label}: {exc}", file=sys.stderr)
        return 1
    print(traj.to_json() if args.json else traj.render())
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    """Reconstruct a stored agent run (alias of ``hsai traj``)."""
    return _print_trajectory(args, "replay")


def cmd_traj(args: argparse.Namespace) -> int:
    """Print one iteration's stored trajectory for a post-mortem."""
    return _print_trajectory(args, "traj")


def cmd_brief(args: argparse.Namespace) -> int:
    from .governance import write_direction

    cfg = _load(args)
    path = write_direction(cfg, repo_root=".")
    print(f"refreshed {path}")
    return 0


def cmd_run_once(args: argparse.Namespace) -> int:
    cfg = _load(args)
    results = run_loop(cfg, repo_dir=".", max_iterations=1, dry_run=args.dry_run)
    for r in results:
        print(r.describe())
    return 0


def cmd_loop(args: argparse.Namespace) -> int:
    cfg = _load(args)
    workers = args.max_parallel if args.max_parallel is not None else 1
    if workers > 1:
        results = run_parallel(
            cfg, repo_dir=".", workers=workers, rounds=args.iterations, dry_run=args.dry_run
        )
    else:
        results = run_loop(
            cfg, repo_dir=".", max_iterations=args.iterations, dry_run=args.dry_run
        )
    for r in results:
        print(r.describe())
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hsai", description="AI hyperswarm self-improvement loop")
    p.add_argument("--version", action="version", version=f"hsai {__version__}")
    p.add_argument("--config", help="path to core.yaml (default: search upward)")
    sub = p.add_subparsers(dest="command", required=True)

    lp = sub.add_parser("loop", help="run the autonomous loop")
    lp.add_argument("-n", "--iterations", type=int, default=1, help="iterations (or rounds)")
    lp.add_argument("--max-parallel", type=int, default=None, help="concurrent workers")
    lp.add_argument("--dry-run", action="store_true", help="no side effects (no PR/merge/agent)")
    lp.set_defaults(func=cmd_loop)

    ro = sub.add_parser("run-once", help="run a single iteration")
    ro.add_argument("--dry-run", action="store_true")
    ro.set_defaults(func=cmd_run_once)

    st = sub.add_parser("status", help="show config + invariant status")
    st.set_defaults(func=cmd_status)

    dr = sub.add_parser("doctor", help="verify environment and safety invariants")
    dr.set_defaults(func=cmd_doctor)

    ri = sub.add_parser("reindex", help="rebuild knowledge-base MOCs + the retrieval index")
    ri.add_argument("--root", default=".", help="repo root holding knowledge/ and docs/adr")
    ri.set_defaults(func=cmd_reindex)

    ob = sub.add_parser(
        "observe", help="refresh the reference-set digests + dossiers (no cycle, no quota)"
    )
    ob.add_argument("--root", default=".", help="repo root holding knowledge/reference")
    ob.add_argument(
        "--refresh", action="store_true",
        help="re-observe every project, not only the ones that have gone stale",
    )
    ob.set_defaults(func=cmd_observe)

    rl = sub.add_parser("recall", help="rank prior lessons/whitepapers/ADRs for a query")
    rl.add_argument("query", help="what the task is about, in plain words")
    rl.add_argument("--k", type=int, default=5, help="how many notes to print")
    rl.add_argument("--kind", default="", help="bias toward this task kind (heal/implement/improve)")
    rl.add_argument("--root", default=".", help="repo root holding knowledge/ and docs/adr")
    rl.set_defaults(func=cmd_recall)

    pr = sub.add_parser("practices", help="the adopted-practice registry (see hsai.practices)")
    pr_sub = pr.add_subparsers(dest="practices_command", required=True)

    pr_list = pr_sub.add_parser("list", help="print every registered practice")
    pr_list.add_argument("--root", default=".", help="repo root holding knowledge/practices")
    pr_list.set_defaults(func=cmd_practices_list)

    pr_add = pr_sub.add_parser("add", help="record a new adopted practice")
    pr_add.add_argument("--root", default=".", help="repo root holding knowledge/practices")
    pr_add.add_argument("--title", required=True)
    pr_add.add_argument("--source-project", required=True, help="e.g. langchain-ai/langchain")
    pr_add.add_argument(
        "--source-artifact", required=True,
        help="one of core.yaml reference_set.learn_from "
        "(source_code, commit_history, ci_cd, issue_history, harness_design, readme)",
    )
    pr_add.add_argument("--evidence", required=True, help="URL or commit/PR reference")
    pr_add.add_argument("--status", default="adopted", choices=list(practices.STATUSES))
    pr_add.add_argument("--adopted-pr", type=int, default=None)
    pr_add.add_argument("--adopted-date", default="", help="YYYY-MM-DD (default: today)")
    pr_add.add_argument("--notes", default="")
    pr_add.set_defaults(func=cmd_practices_add)

    pm = sub.add_parser(
        "postmortem", help="print the failure-class Pareto for a block (spends no quota)"
    )
    pm.add_argument(
        "--block", type=int, default=None, help="block index (default: most recent in the ledger)"
    )
    pm.add_argument("--root", default=".", help="repo root holding knowledge/ledger")
    pm.set_defaults(func=cmd_postmortem)

    cy = sub.add_parser("cycle", help="run one half-day governance block")
    cy.add_argument("--index", "--cycle-index", dest="index", type=int, default=None,
                    help="cycle index (default: derived, or the resume target)")
    cy.add_argument("--resume", action="store_true",
                    help="replay an interrupted block's journal instead of re-running its "
                         "completed steps (no index: the most recent unfinished block)")
    cy.add_argument("--dry-run", action="store_true",
                    help="no GitHub writes and no agent quota spend; journals separately")
    cy.set_defaults(func=cmd_cycle)

    sy = sub.add_parser("synthesize", help="heavy-model synthesis: file substantial tickets")
    sy.add_argument("--index", type=int, default=0, help="rotation index for reference subset")
    sy.set_defaults(func=cmd_synthesize)

    br = sub.add_parser("brief", help="refresh governance/DIRECTION.md")
    br.set_defaults(func=cmd_brief)

    for name, help_text, func in (
        ("traj", "print a stored trajectory for a post-mortem", cmd_traj),
        ("replay", "reconstruct a stored agent run (alias of `traj`)", cmd_replay),
    ):
        tp = sub.add_parser(name, help=f"{help_text} (spends no quota)")
        tp.add_argument("trajectory_id", metavar="iteration",
                        help="iteration number, or a path to a trajectory file")
        tp.add_argument("--root", default=".", help="repo root holding .hsai/traj")
        tp.add_argument("--json", action="store_true", help="print the raw trajectory JSON")
        tp.set_defaults(func=func)

    rc = sub.add_parser(
        "repro-check", help="reproduce-before-fix guard for heal/bugfix PRs (CI gate)"
    )
    rc.add_argument("--pr-title", default=None, help="PR title (default: $PR_TITLE)")
    rc.add_argument(
        "--base-ref", default="origin/main", help="pre-fix ref to diff/checkout against"
    )
    rc.set_defaults(func=cmd_repro_check)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    # Convenience: allow `hsai --loop` as an alias for `hsai loop`.
    raw = list(sys.argv[1:] if argv is None else argv)
    raw = ["loop" if a == "--loop" else a for a in raw]
    args = parser.parse_args(raw)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
