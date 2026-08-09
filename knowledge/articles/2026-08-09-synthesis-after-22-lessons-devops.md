---
tags:
  - article
  - persona/devops
---

# Twenty-Two Runs, Still Zero Manual Interventions: Operational Resilience Report

> For: DevOps level - CI/CD, automation mechanics, operational lessons
> From: [[2026-08-09-synthesis-after-22-lessons]]

## The Durable Journal Changed the Game

The addition of the durable cycle journal (block 41341) transformed how the loop handles failures. Before: a crash mid-block meant manual cleanup. After: a single command (`hsai cycle --resume`) picks up where it left off, skipping already-completed steps.

Operationally, this means:
- No pager alerts for hung blocks (they resume cleanly)
- No risk of duplicate PRs or duplicate issues
- A simple jsonl journal you can inspect and manually edit if needed

We've had two recovery events since implementing this (blocks 41343 and 41345), and both recovered cleanly without intervention.

## Trajectory Capture Unlocked Offline Analysis

The trajectory store (block 41343) records every model call, every agent prompt, and every token spent. This enables forensic analysis without re-running:
- Why did block X consume 8k tokens instead of 5k? Replay and see where the model spent time.
- Did changing the model prompt help? Replay old iterations with the new prompt offline.
- Is the loop getting faster or slower? Trend the wall-clock times across iterations.

This has already caught a subtle issue: a prompt that was unintentionally verbose in block 41345. We could have traced this only by replaying the trajectory.

## Quota Gating: Soft and Hard Limits Working as Designed

The system implements two quota thresholds per block:
1. **Soft breach (80%)**: Log a warning, continue
2. **Hard halt (100%)**: Stop accepting new work

In 22 iterations, no block has hit the hard halt. The closest was block 41345 at ~92% of budget. The soft warning gave us visibility to optimize the synthesis prompt slightly.

## Monitoring and Alerting Gaps

What we still need:
- **In-flight observability**: If a block hangs, we know it only when the per-iteration timeout fires (5 minutes). A streaming metric (Prometheus-style) would catch it sooner.
- **Anomaly detection**: When a block spends 50% more than usual, that's a signal to investigate. Currently, we review this manually after the fact.
- **Replay infrastructure**: The trajectory is stored but not yet queryable via API. Adding a replay API would unlock self-healing (e.g., "if a block fails, replay it with a heavier model").

## Operational Readiness for Parallelization

If we move to 2–3 parallel implementation blocks:
- Each block gets its own quota limit and journal → no contention
- Synthesis remains sequential (single-threaded model call) → predictable spend
- CI/CD gates work per-block → no cross-block conflicts

The risk: if two blocks both open PRs to the same file, GitHub will serialize the merges, but the loop sees them as independent. Add a pre-merge gate that checks for cross-block conflicts.

## Current Bottleneck: Everything Waits for Remote CI

The loop polls GitHub CI explicitly before merging. This is correct but slow: a 60-second CI run blocks the entire block for 60 seconds. Parallelizing to 3 blocks would parallelize CI waiting, so this becomes less of a bottleneck.

## Operational Recommendation

We're ready to go live with parallel blocks. Keep synthesis sequential. Monitor token-per-PR and wall-clock time per block. Set up a 5-minute alert if a block doesn't complete (currently there's a hard timeout but no alert).

## Reference

This operational model draws from MetaGPT's telemetry design and our own trajectory ledger, enabling forensic post-mortems without re-running.
