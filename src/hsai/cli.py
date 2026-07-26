"""`hsai` command-line entry point.

Commands:
  hsai loop [--iterations N] [--max-parallel M] [--dry-run]   run the auto loop
  hsai run-once [--dry-run]                                    a single iteration
  hsai status                                                  config + backlog snapshot
  hsai reindex                                                 rebuild knowledge MOCs
  hsai doctor                                                  verify environment + invariants
"""
from __future__ import annotations

import argparse
import sys

from . import __version__, ai
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
    cfg = _load(args)
    kb = KnowledgeBase.from_config(cfg, ".")
    written = kb.reindex_mocs()
    for p in written:
        print(f"reindexed {p}")
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
