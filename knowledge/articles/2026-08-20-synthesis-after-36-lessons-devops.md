---
tags:
  - article
  - persona/devops
---

# Lessons 34–36: Infrastructure Readiness for Task Escalation

From an infrastructure standpoint, lessons 34–36 are clean failures: CI passes, logs are clear, timeouts are recorded uniformly. This is good observability. But it also shows we're at the edge of what the current infrastructure can handle.

## What the Infrastructure Did Right

1. **Clean timeout recording** — All three lessons recorded the timeout with full context (phase=implement, wall-clock=1200s, model used, CI status). No crashes, no silent failures.

2. **CI remained green** — Even though the workers timed out, the remote CI passed. This means the code changes that *did* complete were valid. The timeout is a capacity issue, not a correctness issue.

3. **Graceful degradation** — The loop did not panic or retry blindly. It recorded the failure and moved on. The harness handled the timeout without cascading failures.

## The Bottleneck

The 1200s wall-clock limit is enforced at the harness level (likely in the worker or agent orchestrator). This is appropriate: you need a deadline. But we're now consistently hitting it on complex synthesis tasks.

### Current Setup

```
Worker starts task (lesson)
  ↓
Agent executes (implementing/synthesizing)
  ↓ (timeout at 1200s)
Worker times out, records failure
  ↓
Lesson marked as fail
```

### What We Need

If we want to support longer-running tasks without changing the wall-clock limit globally, we need *task-specific timeouts*:

```
Worker starts task (lesson)
  ↓
Harness checks: is this a synthesis task? Yes → allow 2400s
Harness checks: is this an implement task? No → allow 1200s
  ↓
Agent executes
  ↓ (timeout at task-specific limit)
Worker times out, records failure
  ↓
Lesson marked as fail
```

This requires:
1. **Task tagging** — Mark tasks as synthesis (2400s), implement (1200s), heal (600s)
2. **Harness configuration** — Add timeout lookup table keyed by task type
3. **Monitoring** — Track timeout rates per task type to detect anomalies

## Recommended Changes

### Phase 1 (Immediate)
- Add `timeout_seconds: 2400` to synthesis task definitions in the backlog
- Deploy without changing harness code (if harness already supports per-task timeouts)
- Monitor: are lessons 34–36 retries now succeeding?

### Phase 2 (This Week)
- If harness doesn't support per-task timeouts, add it:
  - Read timeout from ticket metadata or task tag
  - Default to 1200s if not specified
  - Cap at 3600s globally (no single task > 1 hour)

### Phase 3 (Next 2 Weeks)
- Add monitoring dashboard:
  - Timeout rate per task type
  - Cumulative timeout tokens per block
  - Model escalation frequency
- Set up alerts:
  - If synthesis timeout rate > 20%, escalate to architect
  - If any task type has 100% timeout rate, auto-disable or auto-decompose

## Resilience Patterns

Lessons 34–36 can be retried. But we should build in resilience for future failures:

1. **Automatic retry on timeout** (with exponential backoff)
   - First attempt: 1200s, sonnet
   - Retry 1 (if timeout): 2400s, sonnet
   - Retry 2 (if timeout): 3600s, opus
   - After 3 retries, escalate to architect

2. **Partial result capture** (if task is still in progress at 1200s)
   - If the agent has produced intermediate output (e.g., started implementing), save it
   - On retry, resume from checkpoint instead of starting over
   - This requires the agent to support checkpointing (not yet implemented)

3. **Task decomposition trigger** (if retry still fails)
   - After 2 timeouts on the same task, automatically attempt decomposition
   - File sub-tickets instead of retrying the original
   - This requires a decomposition engine (not yet implemented)

## Monitoring We Need

Add these metrics to the telemetry ledger:

```
{
  "iteration": 4136701,
  "task_type": "synthesis",
  "timeout_seconds": 1200,
  "actual_duration_seconds": 1200,
  "model_used": "opus",
  "tokens_used": 65000,
  "agent_ok": false,
  "reason": "timeout after 1200s"
}
```

From this, we can calculate:
- Timeout rate per task type
- Average tokens per task type
- Model effectiveness per task type
- Cost-per-completed-task

This data will inform future decisions on timeouts, model selection, and decomposition strategy.

## Risk Assessment

**Risk of growing budget to 2400s:**
- Low: already have the capacity, just extending the window
- Downside: masks the problem, doesn't fix root cause

**Risk of not growing the budget:**
- Medium: lessons 34–36 will continue to fail, blocking forward progress
- Downside: the loop stalls

**Risk of implementing task decomposition:**
- High: requires harness changes, could introduce new bugs
- Upside: long-term solution, scales indefinitely

## Recommendation

1. **Immediate**: Deploy increased timeout to 2400s for synthesis tasks (low-risk unblock)
2. **This week**: Deploy per-task timeout support in harness (infrastructure investment)
3. **Next phase**: Implement automatic retry and decomposition logic (resilience layer)

This gives the loop immediate relief, foundation for scaling, and a path to autonomous recovery.

## Infrastructure Checklist

Before the next block:
- [ ] Verify 1200s timeout is per-task, not global
- [ ] Add task-type metadata to ticket template
- [ ] Update telemetry to capture timeout reasons
- [ ] Add dashboard widget for timeout rate by task type
- [ ] Document timeout expectations per task type
- [ ] Set up alerts for anomalous timeout rates

The infrastructure is sound. It just needs to be made explicit and observable.
