"""`hsai` command-line entry point.

Commands:
  hsai loop [--iterations N] [--max-parallel M] [--dry-run]   run the auto loop
  hsai run-once [--dry-run]                                    a single iteration
  hsai status                                                  config + backlog snapshot
  hsai reindex                                                 rebuild knowledge MOCs
  hsai doctor                                                  verify environment + invariants
  hsai evidence-check                                          PR-body SDLC evidence gate (CI)
"""
from __future__ import annotations

import argparse
import os
import re
import sys

from . import __version__, ai, github, repro, review
from .config import CoreConfig, load_config, validate
from .knowledge import KnowledgeBase
from .orchestrator import run_loop
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
    # Subscription-only guard
    try:
        ai.preflight(cfg)
        print("  subscription guard: OK (no metered API path)")
    except ai.SubscriptionGuardError as exc:
        print(f"  subscription guard: FAIL - {exc}")
        ok = False
    print(f"  constraints: subscription_only={cfg.subscription_only}, "
          f"require_ticket_per_pr={cfg.constraints.get('require_ticket_per_pr')}")
    return 0 if ok else 1


def cmd_reindex(args: argparse.Namespace) -> int:
    """Serialized knowledge maintenance: whitepaper cadence + MOC rebuild.

    Kept out of the parallel workers so PRs never collide on derived index files.
    """
    cfg = _load(args)
    kb = KnowledgeBase.from_config(cfg, ".")
    if kb.should_write_whitepaper():
        p = kb.write_whitepaper(kb.synthesize_whitepaper())
        print(f"wrote whitepaper {p}")
    written = kb.reindex_mocs()
    for p in written:
        print(f"reindexed {p}")
    return 0


def cmd_cycle(args: argparse.Namespace) -> int:
    from .cycle import run_cycle

    cfg = _load(args)
    res = run_cycle(cfg, repo_dir=".", cycle_index=args.index)
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


def cmd_evidence_check(args: argparse.Namespace) -> int:
    """The SDLC evidence gate, callable from CI.

    Checks the long-standing PR-body invariants (ticket link, model, lesson)
    and, when the linked ticket carries ``## Acceptance criteria``, that the PR
    shows an ``## Acceptance review`` section with a row per criterion. The
    ticket body is fetched from the ``Closes #N`` link unless supplied.
    """
    cfg = _load(args)
    pr_body = args.pr_body if args.pr_body is not None else os.environ.get("PR_BODY", "")
    ticket_body = args.ticket_body
    if ticket_body is None:
        ticket_body = os.environ.get("TICKET_BODY", "")
    if not ticket_body:
        m = re.search(r"closes\s+#(\d+)", pr_body, re.IGNORECASE)
        if m:
            issue = github.get_issue(cfg.repo_slug, int(m.group(1)))
            ticket_body = issue.body if issue else ""
    result = review.check_pr_evidence(pr_body, ticket_body=ticket_body)
    if result.ok:
        print("evidence-check: PASS")
        return 0
    for reason in result.reasons:
        print(f"::error::{reason}")
    print("evidence-check: BLOCKED")
    return 1


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

    ri = sub.add_parser("reindex", help="rebuild knowledge-base MOCs")
    ri.set_defaults(func=cmd_reindex)

    cy = sub.add_parser("cycle", help="run one half-day governance block")
    cy.add_argument("--index", type=int, default=None, help="cycle index (default: derived)")
    cy.set_defaults(func=cmd_cycle)

    sy = sub.add_parser("synthesize", help="heavy-model synthesis: file substantial tickets")
    sy.add_argument("--index", type=int, default=0, help="rotation index for reference subset")
    sy.set_defaults(func=cmd_synthesize)

    ev = sub.add_parser(
        "evidence-check", help="SDLC PR-body evidence gate incl. acceptance review (CI gate)"
    )
    ev.add_argument("--pr-body", default=None, help="PR body (default: $PR_BODY)")
    ev.add_argument(
        "--ticket-body", default=None,
        help="linked ticket body (default: $TICKET_BODY, else fetched from 'Closes #N')",
    )
    ev.set_defaults(func=cmd_evidence_check)

    br = sub.add_parser("brief", help="refresh governance/DIRECTION.md")
    br.set_defaults(func=cmd_brief)

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
