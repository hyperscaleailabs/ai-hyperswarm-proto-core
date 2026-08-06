---
tags:
  - article
  - persona/devops
---

# Operational Durability: When the Loop Survives Its Interruptions

The cycle journal landed this week, and from an operations standpoint, this is the moment the autonomous loop became resilient to failure. Before: if the loop crashed mid-block, you'd restart from the beginning. Now: it resumes from the last completed iteration and finishes the block. That's the difference between a toy system and something you can run in production.

## What shipped and what it means operationally

**Idempotent cycle resumption.** The cycle journal records which iterations completed and their outcomes. When a run is interrupted (timeout, crash, quota spike, manual halt), resume reads the journal, identifies the last good state, and skips already-executed iterations. This eliminates the "did we already merge that PR?" operational burden. You know, because it's recorded.

**Full trajectory archiving.** Every agent execution is now a multi-KB JSON record with inputs, reasoning, outputs, model metadata. Seven terabytes per million executions. At current throughput (hundreds of iterations per day), you're at ~megabytes per day. That's a long-term storage story you need to build, but for the next quarter, it's manageable.

**Replay harness for offline validation.** You can take a trajectory, swap in a different model, and replay without touching production. That's cheap validation before a potentially risky change. But remember: it's not a production run. It doesn't trigger CI, doesn't interact with external systems, doesn't experience real latency. It's a logic simulator.

## The operational commitments you're now making

**Cycle journal is source-of-truth for run state.** If the journal says an iteration is complete, the system trusts that. If the journal is corrupted or inconsistent (partial write, crash before flush), resumption can do the wrong thing. You need to monitor journal write health and have a procedure for manual journal recovery.

**Trajectory storage is load-bearing.** Trajectory data is now essential for auditing decisions. You can't silently drop old trajectories. You can't change the schema without a migration. You can't lose a trajectory and pretend it didn't happen. You're running a database now, not just logs.

**Parallel resumption is untested.** If five agents are interrupted simultaneously and all five resume at once, you haven't tested whether the cycle journal can handle that concurrency. That's a stress test for Q3. Until then, resume serially or with a lock.

## What to monitor and watch

1. **Journal write latency.** The cycle journal is synchronous — every iteration completion waits for the journal write. If writes slow down (filesystem, disk contention, NFS lag), your loop slows down. Add observability for journal write time, not just iteration time.

2. **Trajectory query performance.** You can query trajectories, but aggregations ("show all implement tickets where model=haiku, confidence > 0.9") are taking 5-30 seconds. As trajectory becomes part of operational decision-making, that needs to drop to sub-second. Index aggressively.

3. **Resume failure recovery.** If resume itself crashes (e.g., corrupted journal), what's the recovery procedure? Can you manually edit the journal and retry? Do you have a read-only audit trail of what was resumed? Test this now, not when it happens in production.

4. **Quota safety with resumption.** If a run is interrupted and resumed, you're not re-spending tokens on already-completed iterations. But you need monitoring to make sure resumed runs don't accidentally double-count against your daily quota. A stuck run that keeps resuming could blow your budget.

## Q3 operational roadmap

- **Week 1:** Add dashboards for journal write latency and trajectory query performance
- **Week 2:** Load-test concurrent resumption (5+ interrupted blocks resuming simultaneously)
- **Week 3:** Document manual journal recovery procedure and test it once
- **Week 4+:** Build feedback loop: trajectory analysis → model-selection recalibration → replay validation → production rollout

The infrastructure is solid. Operationally, you're now responsible for keeping the journal and trajectory store healthy. That's a new baseline commitment.
