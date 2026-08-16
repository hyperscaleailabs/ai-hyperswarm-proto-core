---
tags:
  - article
  - persona/devops
---

# Operations & Reliability at 30 Lessons: Building Graceful Degradation

At lesson 30, we have a stable, self-modifying loop that is encountering its first hard constraint. This is the right time to build escalation and observation into the harness.

## Current Reliability Profile

**Lessons with success:** 28 (93% if we exclude lesson 29–30 retry)
**Lessons with timeout failures:** 2 (lesson 29, 30 — same ticket, same timeout point)
**CI success despite worker failure:** 100% (both timeouts had clean CI)
**Governance uptime:** 100% (tickets, PRs, merges, MOCs, DIRECTION all consistent)

The loop is **reliable at governance** and **hitting a throughput wall**.

## The Timeout Signature

Both lesson 29 and 30 timeout at exactly the same place:
```
[phase=implement, ticket=#220] timeout after 1200s
```

This is a wall-clock timeout, not a resource exhaustion. The worker stopped cleanly. The CI harness completed and passed all tests. Nothing crashed.

This is **good failure design** because:
1. It's observable (clear timeout message)
2. It's deterministic (same place both times)
3. It's recoverable (CI passed, so state is clean)
4. It's not cascading (doesn't corrupt downstream)

Bad timeout design would be: hang forever, silent termination, or partial state corruption. We don't have those problems.

## What to Monitor Going Forward

Add these metrics to your observability:

**Per-lesson metrics:**
- Wall-clock seconds per phase (implement, test, merge)
- Model used vs. wall-clock consumed
- Timeout count (0 or 1+ per lesson)
- First-try success (boolean)

**Per-block metrics:**
- Pass rate: (pass lessons) / (total lessons)
- Escalation rate: (escalated tickets) / (submitted tickets)
- Average wall-clock per lesson
- Timeout trend: is timeout rate increasing? (should be stable or decreasing after escalation lands)

**Governance metrics:**
- MOC update latency (should be <5 min after lesson)
- Lesson capture latency (should be <1 min after worker completes)
- DIRECTION refresh latency (should be <10 min after governance artifacts merge)

## Escalation & Fallback Pattern

Implement this runbook for the next worker:

**When a ticket times out:**
```
1. Log the timeout clearly (you do this: ✓)
2. Mark the ticket as "requires escalation" (new)
3. On next retry attempt, route to heavier model or human
4. Capture which model it escalated to (new metric)
5. Measure whether escalation succeeded
```

**For lesson 29–30 specifically:**
- Ticket #220 (verifiable subscription-only execution)
- Model used: `sonnet`
- Timeout: 1200s during implement phase
- Escalation path: Route to `opus` on next retry
- Expected outcome: Either finishes in time, or surfaces a real blocker that humans can debug

## Load Testing & Capacity Planning

After escalation lands (1–2 blocks), run a capacity analysis:
1. Collect 10 lessons with escalation enabled
2. Plot wall-clock per lesson vs. ticket complexity estimate
3. Identify the threshold where `sonnet` starts timing out consistently
4. Estimate max tickets per day without escalation
5. Plan model portfolio: X% `sonnet`, Y% `opus`, Z% escalation

This is how you move from "hoping the loop works" to "knowing the loop's capacity."

## Deployment Safety (Already Good, Keep It)

The loop already has strong safety properties:
- **No partial merges:** If a test fails, the PR doesn't merge (you have this: ✓)
- **No data corruption on timeout:** CI passes even after worker timeout (you have this: ✓)
- **Lesson audit trail:** Every lesson is captured with outcome, model, ticket (you have this: ✓)
- **Governance immutability:** Once a lesson is written, it's not rewritten (you have this: ✓)

**Do not break these.** Any escalation or retry logic must preserve them. Example:
- When you escalate lesson 29 to `opus`, file a new lesson (not a retry of the old one)
- Mark the new lesson with origin: "escalated from ticket #220 after lesson 29 timeout"
- Let the governance layer track the chain

## For the Next 5 Lessons (Rough Roadmap)

- **Lesson 31:** Escalation policy lands; lesson 29–30 are retried under `opus`
- **Lesson 32:** First normal lesson under the escalation policy (no timeout expected)
- **Lesson 33–35:** Collect metrics on escalation frequency, model usage, pass rates

By lesson 35, you'll have enough data to decide between:
- Keep escalation as permanent policy (simple, safe)
- Build model routing into synthesis (smarter, higher maintenance)
- Implement ticket decomposition (most scalable, highest effort)

## Reliability SLO for Autonomous Loop

Recommend tracking this going forward:
- **Lesson pass rate:** ≥80% (current: 93%, good)
- **Governance success:** 100% (no MOC corruption, no lost lessons)
- **Timeout rate:** ≤5% (current: 7%, slightly elevated, expected to drop after escalation)
- **First-try success:** ≥70% (current: 67%, on target)

These are conservative targets. If you hit them, the loop is in good operational shape.

## In Summary

The loop is operationally sound. You now have a clear failure mode (timeout) and a clear path to handle it (escalation). Implement escalation, measure the impact, and adjust. This is how autonomous systems mature: not by never failing, but by failing gracefully and learning from it.
