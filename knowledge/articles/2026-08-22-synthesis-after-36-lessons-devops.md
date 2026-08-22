---
tags:
  - article
  - persona/devops
---

# Timeouts as Infrastructure Data: Block 41371 Observability

Three consecutive lessons timed out at the 1200s boundary. This is not a transient error. It's infrastructure feedback about the loop's resource model.

## What the data tells us

| Lesson | Model | Task | Timeout | Phase |
| --- | --- | --- | --- | --- |
| 34 | sonnet | adopted-practice registry | 1200s | implement |
| 35 | sonnet | failure taxonomy | 1200s | implement |
| 36 | opus | retrieval-grounded synthesis | 1200s | implement |

Three tasks. Two models. One consistent outcome: 1200s, hard stop.

This is not a resource spike. It's a repeatable limit. The task reaches the boundary and stops, every time.

## The implication

Our timeout is a *scheduler* limit, not a *resource* limit. The task doesn't exceed available memory or CPU—it hits a predefined wall. If we want to move forward, we need to either:

1. **Extend the wall** (increase timeout)
2. **Build smaller tasks** (decompose to fit within the wall)
3. **Pre-filter** (don't start tasks we know will timeout)

Option 1 is expensive (wall-clock + quota). Option 3 requires prediction (model-selection heuristic, in the backlog). Option 2 is immediate and cheap.

## Logging and recovery

The loop's behavior under timeout is correct:
- Tasks fail cleanly (no partial merges, no orphaned PRs)
- Failures are logged with full context (model, phase, timing)
- Retries are gated (won't retry until a human or escalation policy decides)

What we're missing:
- **Root-cause capture**: Why did synthesis take 1200s? Where did the time go? (memory, CPU, I/O, model latency?)
- **Predictive gating**: Can we estimate task duration before starting?
- **Graceful degradation**: Can tasks return a 70% solution at 400s rather than timing out at 1200s?

## Recommendations

**Immediate**: Log synthesis duration and token usage for completed tasks (not just timeouts). Over a few blocks, we'll have data to predict which tasks will timeout.

**Next block**: Add a pre-flight check: if a task has similar characteristics to lessons 34–36, route it to decomposition or escalation before starting.

**Longer term**: Implement graceful synthesis (return best-effort output at T-30s before timeout, rather than all-or-nothing at T=1200s).

## Quota impact

Three timeouts consumed quota (models were running, even if they didn't finish cleanly). Before attempting lessons 34–36 again, confirm that decomposing them will not exceed the block budget. Retrying the same 1200s task three times is expensive; breaking it into three 400s tasks is much cheaper and more likely to succeed.
