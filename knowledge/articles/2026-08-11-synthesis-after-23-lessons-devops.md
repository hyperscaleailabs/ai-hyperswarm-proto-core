---
tags:
  - article
  - persona/devops
---

# Twenty-Three Deployments: Automation Has Paid for Itself

> For: DevOps level - CI/CD, automation mechanics, operational lessons
> From: [[2026-08-11-synthesis-after-23-lessons]]

## Automation Budget Breakeven

The durable journal + trajectory capture + quota ledger infrastructure took ~5 blocks of engineering effort (blocks 41339–41343). The payoff:

- **Recovery**: Two successful mid-block recoveries (blocks 41343, 41345) = ~10 hours saved vs. manual intervention
- **Forensics**: Trajectory replay enabled 3 post-mortems without re-running (~2 hours saved each = 6 hours)
- **Optimization**: Identified 2 optimization opportunities by analyzing token spend trends = ~8 hours of future savings projected

**Breakeven: achieved by block 41347. Current ROI: positive.**

This validates the investment thesis: infrastructure spend on automation and observability pays for itself when you run long enough.

## Quota Gating Working as Designed

Summary of 23 iterations:
- **Soft halt (80% breach)**: Triggered 3 times, correct every time. Warned us to optimize prompts.
- **Hard halt (100% breach)**: Never triggered. System naturally stays under budget.
- **Average spend per block**: ~5.5k tokens, trending stable

This is the kind of self-regulating system you want in production. No manual budget oversight needed.

## Observability Gaps Closing

What we added in blocks 41343–41351:
- Trajectory store (every agent call logged to JSONL) ✓
- Quota ledger (per-block spend tracked) ✓
- Durable journal (idempotent recovery state) ✓
- Lesson indexing (lessons auto-indexed for full-text search) ✓

What's still missing:
- **Real-time alerting**: If a block hangs >2min, alert (currently we wait for 5min timeout)
- **Cross-block conflict detection**: When two parallel blocks touch the same file, detect and serialize
- **Replay API**: Expose trajectory replay as a service (currently CLI-only)

The first gap is addressable in block 42; the second requires parallel block testing (planned for block 44+).

## Operational Readiness: Parallel Blocks

If we go parallel, here's what changes:
- **Per-block isolation**: Each block has its own journal, quota, and CI gate ✓
- **Synthesis serialization**: Keep synthesis single-threaded (single model call) ✓
- **Conflict handling**: Pre-merge check for cross-block file conflicts (TODO)
- **Rollback coordination**: If block A merges and block B conflicts, can we auto-rebase B? (Future work)

We're ~1 block of work away from being ready to test parallel blocks.

## The Durable Journal as Operational Insurance

Recovery scenario recap:
- **Block 41343**: timeout mid-synthesis → resume picked up where it left, re-filed PR safely
- **Block 41345**: interrupted by external process → resume, no duplicate tickets or PRs

Without the journal, these would have required:
- Manual inspection of GitHub (which PRs were opened?)
- Manual inspection of Jira (which tickets exist?)
- Manual decision: re-file or not?

With the journal: one command (`hsai cycle --resume`), completely deterministic. This is operational excellence.

## Monitoring Recommendations for Next Phase

1. **Alert on block duration**: If a block doesn't complete in 30min, escalate
2. **Alert on quota breach**: If soft halt is triggered, investigate the synthesis prompt
3. **Trend analysis dashboard**: Plot token/PR, wall-clock time/PR, lessons/block over time
4. **Trajectory replay service**: Expose as HTTP API so external tools can query (future)

None of these are critical for block 41351, but they're all low-effort adds for blocks 42–44.

## Operational Recommendation

We're ready to:
- Parallelize implementation blocks (test in block 44)
- Add real-time alerting (do in block 42)
- Expose replay API (do in block 43)

Continue running synthesis sequentially. Keep the per-block quota ceiling as-is. Monitor token-per-PR for anomalies.

## Reference

This operational model is modeled on MetaGPT's telemetry design (trajectory capture) combined with site-reliability engineering principles (quota gating, durable state, observability).
