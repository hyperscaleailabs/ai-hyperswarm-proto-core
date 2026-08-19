---
tags:
  - article
  - persona/devops
---

# Monitoring Report: Block 41369 – Model Tier Asymmetry and Execution Boundaries

This is a DevOps read of lessons 32–36. The operational narrative: you have healthy data collection, clear failure signals, and now you're hitting resource constraints that need infrastructure-level decisions.

## Execution Metrics (Lessons 32–36)

| metric | value | note |
| --- | --- | --- |
| Total iterations | 5 | All `implement` kind |
| Successful | 1 | Lesson 36 (retrieval synthesis) |
| Failed | 4 | Lessons 33–35 timeouts; lesson 32 governance synthesis |
| Model distribution | 4× sonnet, 1× opus | Escalation happened on lesson 36 only |
| Timeout pattern | 3/5 hit 1200s wall | Always with sonnet, >400 token context |
| CI health | 5/5 green | No crashes, no runtime errors |

## What Operational Health Looks Like Here

The fact that 1200s timeouts show up as clean failures (not crashes) tells you:
- Timeout detection is working
- Worker loops properly exit
- CI doesn't hide the failure

That's good operational discipline. You're not hiding failures in logs; you're capturing them as data.

## The Resource Constraint

Lessons 33–35 all hit the same wall: sonnet times out on complex, multi-file work. Lesson 36 succeeded with opus. This is a **model-tier mismatch**, not a bug.

What you're observing:
```
Context size: 3000–5000 tokens (estimated from work descriptions)
Sonnet budget: ~1200s wall-clock
Opus budget: ~1200s wall-clock

Sonnet throughput: maybe 20–40 tokens/sec (extrapolated from timeout)
Opus throughput: ~50–80 tokens/sec (rough guess from successful lesson 36)

→ Sonnet runs out of time; opus finishes
```

This is a hard constraint. You hit it when:
- Synthesis work (complex decision logic)
- Multi-file refactors (large context window)
- Provenance tracking (more state to manage)

## Operational Decisions

You need to choose how to handle this constraint going forward:

### Option 1: Escalation to Opus for Suspected Complex Work
**Pro**: Works immediately, no engineering investment, unblocks lesson 34–35  
**Con**: Cost increases ~2–3x per iteration; no learning about what's actually complex  
**Recommended if**: You're OK with higher per-iteration cost and want fast unblocking

### Option 2: Learn Model Selection from Historical Data
**Pro**: Data-driven, cost-optimized, works for all future lessons  
**Con**: Requires building and tuning a heuristic; takes 2–3 lessons to stabilize  
**Recommended if**: You're willing to keep seeing timeouts for 2 more cycles while learning

### Option 3: Decompose Large Tickets into Smaller Subtasks
**Pro**: Keeps cost constant, teaches the loop to think in smaller chunks  
**Con**: Requires smarter orchestration, harder to get right  
**Recommended if**: You have engineering budget and want the loop to be more surgical

## What I'd Instrument Right Now

If you pick **Option 1** (escalation):
- Track opus usage rate (percentage of iterations using opus)
- Monitor cost per iteration (token spend, wall clock)
- Set alert thresholds: if opus usage exceeds 50% within a block, investigate

If you pick **Option 2** (learned heuristic):
- Collect feature vectors for every lesson: context size, file count, change type, model used, outcome
- Train a binary classifier: "will this timeout with sonnet?"
- Track false-positive rate (predicted complex → actually simple, wasted opus budget)
- Track false-negative rate (predicted simple → timed out, wasted sonnet budget)

If you pick **Option 3** (decomposition):
- Monitor orchestrator overhead: how much extra time does decompose/reassemble add?
- Track subgoal success rate: what % of subtasks succeed?
- Monitor synthesis quality: do the decomposed tickets actually solve the parent problem?

## CI Pipeline Readiness

Your CI is already catching the timeouts correctly:
- Green before work: ✓
- Timeout detected: ✓
- Green after timeout (nothing broke): ✓
- Lesson captured: ✓

What you should add to CI:
1. **Model-tier cost tracking**: Report tokens per lesson, flagged if >normal
2. **Timeout heuristic testing**: If you go route 2, test the heuristic against past lessons before deploying
3. **Escalation gate**: If route 1, gate opus usage to specific ticket types (governance, synthesis, practice) to prevent runaway cost

## Next Block Readiness

Block 41370 will tell you which path you chose:
- If you see opus in 80%+ of iterations → Option 1 (escalation)
- If you see timeout + retry pattern continuing → Option 2 (learning in progress)
- If you see smaller tickets + orchestrator overhead → Option 3 (decomposition)

Watch block 41370's metrics closely. They'll tell you if your choice is working or if you need to pivot.

## Health Summary

- **CI**: Healthy, timeouts are clean
- **Model selection**: Needs a policy (you have time to decide in block 41369)
- **Data quality**: Excellent (each timeout is fully logged)
- **Next risk**: Uncontrolled escalation (if you go route 1 without cost gating)

Your call on which path. But pick one for blocks 41369–41371 and stick with it so you have enough signal to measure.
