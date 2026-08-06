---
tags:
  - article
  - persona/devops
---

# Operational Resilience: Journals, Resumption, and Traceable Decisions

Two features shipped this week that directly affect how we operate autonomous systems: a durable cycle journal that makes long-running processes resumable without data loss, and governance artifacts that create an audit trail for every architectural decision. From ops, this is the difference between a rehearsal system and one we can actually deploy.

## The mechanics that shipped

**Append-only cycle journal with structured resumption.** The autonomous loop now writes a line to `.hsai/cycles/<cycle_index>/journal.jsonl` after every major step completes: synthesis done, implementation done, review done, governance artifacts done. If the process crashes, network hiccups, or quota runs out mid-cycle, the next invocation of `hsai cycle --resume` reads the journal, skips completed steps, and resumes where it left off. No data loss. No manual remediation. No "rebuild the state from commit history."

**Governance artifacts as part of the cycle, not after-thoughts.** Whitepapers, persona articles, and MOC reindexing are now *embedded* in the cycle journal. They're not "do this manually at the end"; they're steps that get journaled, can be resumed, and are versioned alongside everything else. This means governance failures (e.g., persona article generation fails) don't silently break the cycle; they're logged and can be replayed.

**Multi-persona synthesis for observability.** When a cycle completes, three independently-written persona articles (architect, CTO, DevOps) capture different insights from the same lesson data. That redundancy is useful for ops: if all three articles flag the same risk, it's real. If only one does, it might be persona-specific.

## The operational reality

**Cycle journals are small but mandatory for durability.** A typical cycle journal is ~500 bytes to 2KB per cycle (one JSON line per completed step). Over a quarter of continuous operation, that's negligible. The cost is that the `.hsai/cycles/` directory becomes load-bearing. If it gets deleted or corrupted, we lose resumption capability. That's a backup/retention question that ops needs to own.

**Resumption is fast but not free.** Replaying a cycle journal to skip already-completed steps is sub-second (parsing JSON and checking state). But if a cycle interrupts during an expensive phase (e.g., synthesis consumed half its quota), resuming still has to pay for the half that ran. Resumption saves *time* (no re-synthesis), but not quota for already-completed work.

**Governance artifacts increase cycle time by ~5-10%.** Generating whitepapers (scanning lesson data), synthesizing persona articles (structured text generation), and reindexing MOCs all consume CPU and quota. Early measurements suggest this adds 5-10 minutes to a 90-minute cycle. That's acceptable for now, but it scales. At 10x concurrency, governance becomes the tail latency.

## What to monitor

1. **Journal write failures.** If `.hsai/cycles/` becomes unavailable, the journal stops appending. Set up alerting: any cycle that completes without journal entries is a data loss risk. Monitor inode usage on the cycles directory; when it approaches limit, prune old cycles.
2. **Resume latency and correctness.** When `--resume` is invoked, log:
   - How many steps were skipped (good — no re-work)
   - How many steps were re-run (bad — means cycle was partially complete)
   - Did the resumed cycle produce the same decisions as expected?
3. **Governance artifact quality and staleness.** Persona articles are generated every ~10 lessons. Between syntheses, governance debt accumulates. Monitor: how many lessons have accumulated since the last synthesis? When does it become worth re-synthesizing?

## Operational checklist for production

- [ ] Cycle journal directory is backed up daily
- [ ] Journal write failures trigger an alert
- [ ] Resume latency is measured and tracked
- [ ] Automation validates that resuming a cycle produces bit-for-bit identical decisions
- [ ] Governance artifact generation doesn't timeout or OOM; graceful degradation if it fails
- [ ] MOC reindexing is idempotent (running it twice produces the same result)

## What's next

The cycle is now resumable. But that only matters if we actually *use* resumption. In the next iteration, we should:

1. **Load-test resumption.** Interrupt cycles at random points and validate correct recovery.
2. **Document runbook for partial failures.** If persona article generation fails mid-cycle, what do we do? Retry? Skip and continue? Burn down? That needs a documented, practiced runbook.
3. **Measure governance cost.** Exactly how much quota and wall-clock time do governance artifacts consume? At what concurrency do they become a bottleneck?

Durability is in place. The question now is whether we operate assuming interruptions (frequent resumes, constant monitoring) or whether we treat them as rare edge cases.
