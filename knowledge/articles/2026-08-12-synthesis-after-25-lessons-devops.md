---
tags:
  - article
  - persona/devops
---

# Twenty-Five Iterations Without a Pager Alert: And Why That Streak Ends

> For: DevOps level - CI/CD, automation mechanics, operational lessons
> From: [[2026-08-12-synthesis-after-25-lessons]]

## The Good News: Operational Stability Through 22

From lesson 1 through 22, the loop ran without a single incident that required manual intervention. No hung blocks, no dangling PRs, no quota runaway. Durable journals recovered cleanly from interrupts, trajectory telemetry showed clean cost curves per iteration, and CI gates held. If that was the end of the story, we'd be shipping this.

## The Reality: Three Failures That Shouldn't Have Been Silent

Lesson 23–25 didn't produce pager alerts because the failures were **inside** the loop, not **outside** it. The system didn't crash or exceed quota; it cleanly executed a task and decided not to produce output (agent ok=False). That's actually worse from an ops perspective—it means the monitoring is blind to a failure class.

### What Went Wrong Operationally

**Lesson 23 (timeout)**:
- Worker process consumed 1200+ seconds of wall-clock time
- Loop detected hard timeout and halted
- No alert fired because timeout is a known failure mode (quota protection)
- But the timeout itself is a signal: context was larger than expected

**Lesson 24–25 (silent halt)**:
- Worker exited cleanly with ok=False but no error log
- CI and remote checks passed (the problem wasn't repo state)
- Loop recorded "agent ok=False" in the lesson file
- But there's no metric, alert, or dashboard that surfaces "ok=False rate exceeded threshold"

### The Operational Blind Spot

Currently, the loop has observability for:
- ✅ CI status (green/red)
- ✅ Quota consumption (tokens per block)
- ✅ Wall-clock times (block duration)
- ✅ Lesson outcomes (pass/fail)
- ❌ Agent ok/False rate
- ❌ Timeout frequency
- ❌ Synthesis validation failures (caught implicitly, not measured)

When lesson 23 timed out, we didn't know it was a scaling problem until we read the lesson file. A single histogram of "worker runtime per iteration" would have surfaced the trend.

## Quota Gating Still Holds

The good news: neither failure triggered quota exhaustion or hard halts. Blocks 41349, 41351, 41353 all spent quota appropriately and stayed within budget. The trajectory ledger shows clean per-PR token costs. Quota-per-block ceiling is doing its job—it's not the constraint we hit.

## Monitoring Gaps and Fixes

**Immediate (low effort)**:
- Add a metric for "agent ok=False rate per block"
- Alert if timeout count > 0 in a block
- Dashboard for synthesis validation pass/fail rate (inferred from "blocked by timeout" patterns)

**Medium term (medium effort)**:
- Add pre-flight validation for synthesis output (check that generated tickets parse against repo state before workers see them)
- Streaming histogram of worker runtime so we catch scaling issues early (before timeout)

**Long term (high effort)**:
- Implement top-K lesson retrieval (reduces per-worker context growth at the source)
- Add a circuit breaker: if ok=False rate > 20% in a block, escalate to human review instead of auto-merge

## Operational Readiness Assessment

| aspect | status | note |
| --- | --- | --- |
| CI gate | ✅ solid | remote CI is truth, no false negatives |
| Quota protection | ✅ solid | no runaway spend, predictable per-block cost |
| Recovery from crashes | ✅ solid | durable journals, can resume without replay |
| Timeout detection | ⚠️ working | detects, but no trend/alert |
| Silent failure detection | ❌ missing | ok=False goes unmonitored |
| Synthesis validation | ❌ missing | malformed tickets aren't caught early |

## Recommendation

The loop is operationally stable for its current scale (5 PRs/block, 25 lessons). Before scaling to 10+ PRs/block or 50+ lessons:

1. **Add ok=False rate alerting** (1–2 hours)
2. **Implement synthesis pre-flight validation** (4–6 hours)
3. **Add streaming worker-runtime histogram** (2–3 hours)

After that, you can safely run the loop in an always-on mode with human review only for exceptions (ok=False > threshold, or anomalous cost).

Current state: supervised execution only. Recommended state for 50+ lessons: automated with human spot-check (daily digest of high-ok=False blocks).
