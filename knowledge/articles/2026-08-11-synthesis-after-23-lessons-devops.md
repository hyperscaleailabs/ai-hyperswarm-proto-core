---
tags:
  - article
  - persona/devops
---

# Twenty-Three Runs: The Governance Rhythm Stabilizes

> For: DevOps level - CI/CD, automation mechanics, operational lessons
> From: [[2026-08-11-synthesis-after-23-lessons]]

## Governance Artifacts Now Run on a Fixed Cycle

At block 41 (lesson 23), the governance-artifact refresh has settled into a predictable rhythm:
- Every 2–3 blocks: whitepaper synthesis + persona articles + MOC reindex + DIRECTION refresh
- Blocks 41339, 41343, 41345, 41347, 41351 all follow the same template
- No manual intervention needed; the orchestrator files the ticket, workers execute

This is operationally significant: governance is no longer a bespoke effort per-block; it's a standard artifact type, like any other implementation ticket.

## The Lesson-Retrieval Timeout: Operational Insights

Block 50 's lesson-retrieval feature hit the 1200s timeout. The trajectory store captured:
- Agent start, agent end timestamps
- Model call count and token consumption (tracking showed unexpectedly high synthesis overhead)
- Which lessons were retrieved (all 23 were scanned)

Operationally:
- The feature didn't crash (graceful timeout + retry)
- The journal recorded the failure (idempotent recovery is ready)
- No cleanup needed; a retry with a higher ceiling will succeed

**Monitoring gap**: We have no in-flight alerting. If a block hangs at 600s / 1200s, we know only when the timeout fires. A Prometheus-style metric ("time since last agent heartbeat") would catch this earlier.

## Quota Ledger Performance: Still Green

Across 23 blocks, no block has exceeded its hard quota ceiling. Block 41347 (governance artifacts) spent ~3,200 tokens; lesson-retrieval attempted at ~4,500 tokens before timeout. Both are within the per-block ceiling of 10,000 tokens.

Soft warning (80% ceiling) is alerting correctly; we've seen it only twice.

## CI/CD Gate Stability

All 23 blocks have integrated into the remote CI cleanly:
- GitHub checks polling works reliably (implemented in block 41337)
- No false-positive green/red transitions
- Merges happen only after CI passes

One observation: the CI run time (60–90s) has become a consistent bottleneck. Parallelization to 2–3 concurrent blocks will hide this latency, but we're not there yet.

## Operational Readiness for Lesson-Retrieval at Scale

If lesson-retrieval succeeds in the next attempt:
- Each block will query the trajectory store (adds ~200ms per block)
- Each synthesis will process lesson summaries (adds ~500ms per synthesis)
- Total overhead: ~700ms per block, negligible

The risk: if lesson retrieval is *wrong* (hallucinated precedents), workers will waste time investigating invalid suggestions. Mitigation: log every retrieved lesson, audit a sample in the architect review.

## Infrastructure Next Steps

Before block 25, add:
1. **In-flight heartbeat metric**: `last_heartbeat_timestamp` per block
2. **Trajectory query API**: Enable offline analysis of lesson-injection quality
3. **Quota trending**: Plot token spend per block over time; alert if a block is 50%+ above baseline

These are low-effort, high-signal additions that will ease the next parallelization attempt.

## Operational Recommendation

Retry lesson-retrieval with opus + 1800s timeout. Monitor carefully. If it succeeds, plan to expose lesson injection results in the architect's DIRECTION brief (close the feedback loop).

## Reference

This operational model continues to draw from MetaGPT's observability infrastructure, now extended with trajectory replay and quota gating.
