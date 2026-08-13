---
tags:
  - article
  - persona/architect
---

# Emergent Resilience Through Feedback Loops

> For: Architect level - system design, tradeoffs, patterns adopted
> From: [[2026-08-13-synthesis-after-27-lessons]]

## The Critical Design Insight from Lessons 23-27

The three-failure sequence (lessons 23-25) was not a failure of individual components—it was a demonstration of how **tightly coupled feedback loops collapse under load**. Here's the pattern:

1. **Input saturation** (lesson 23): Synthesis → Workers loop with full history. Tokens per prompt: 100KB+. Result: timeout.
2. **Output validation gap** (lesson 24): Synthesis generates tickets. Workers start work. Validation happens downstream (at PR review time). Result: wasted resources, silent halt.
3. **Prioritization blind spot** (lesson 25): Chore work has no explicit QoS. When backlog is full, it gets dropped. Result: incomplete governance record.

Each is solvable in isolation. But they're all caused by **information asymmetry**: the synthesis phase doesn't know about worker constraints; the workers don't validate synthesis output; the orchestrator doesn't know to prioritize governance.

## Lessons 26-27: Three Feedback Improvements

After lesson 25, the system was modified (implicitly, through lesson success) to tighten feedback in three ways:

**1. Prompt compression for workers** (evident from lesson 26 success)
- Lesson-retrieval memory timeout → Pruned injection strategy
- Prior lesson reference from knowledge base, not full text
- Result: lesson 26 completes without timeout

**2. Early synthesis validation** (evident from PR #203 scope)
- Adversarial review gate before PR creation (not after)
- Different model tier reviews the synthesis artifact
- Result: lesson 27 has zero rework iterations

**3. Explicit prioritization for chore work** (inferred from lesson 26 merge)
- Governance artifacts no longer competing with feature work
- Likely: dedicated synthetic priority or hook in orchestrator
- Result: documentation artifacts flow smoothly

## Pattern: The Loop Is Becoming Self-Healing

This is the architecturally significant part: **the loop didn't need external intervention to recover**. It identified that prompt injection was expensive, that synthesis validation was missing, that chore work needed explicit treatment—and implicitly adapted.

This suggests the system is developing three key traits:
- **Observability**: Failure modes leave signals (timeouts, silent halts, merge gaps)
- **Adaptability**: Workers can be tier-selected; synthesis can be gate-kept
- **Autonomy**: New tickets emerge to fix identified problems (e.g., "feat: adversarial review gate" was filed because lesson 24 showed validation gaps)

## Architectural Implication: You're Building a Learning Loop, Not a State Machine

The original design (lessons 1-5) was: *Synthesis → Tickets → Workers → CI → Merge → Lesson*.

By lesson 27, it's become: *Synthesis → Validation Gate → Tickets → Tier-Selected Workers → Multi-Model Review → CI → Merge → Lesson → Knowledge Injection → Next Synthesis*.

**Each lesson makes the loop tighter.** The architectural wins are:
- **Feedback latency reduced**: Errors surface sooner (validation gate catches malformed tickets before workers waste time)
- **Tier selection learned**: Work routing adapts (expensive work → expensive model)
- **Knowledge accumulation**: Prior lessons steer future synthesis (lesson-retrieval memory)

## Next Bottleneck (Prediction)

By lesson 50-100, the next constraint will surface: **orchestration overhead**. Right now, 5 workers per block run sequentially. When you scale to parallel workers (3x or 5x), the synthesis phase will need to generate more tickets faster, or the workers will starve.

The architectural fix at that point: move from centralized synthesis → distributed hypothesis generation. Each worker proposes local improvements independently, a synthesis phase deduplicates and ranks them, and the next workers see the aggregated backlog.

This is the crewAI pattern (swarm generation, committee validation). When you hit that bottleneck, that's the architectural pattern to adopt.

## Recommendation for Next Phase

1. **Document the current feedback structure**: Draw the loop as it is now (lessons 1-27), with validated patterns explicitly called out.
2. **Instrument for bottleneck detection**: Add telemetry to catch the next constraint (queue depth, worker idle time, synthesis latency).
3. **Reserve capacity for redesign**: Plan for lesson 50+ to require a re-architecture (distributed synthesis, multi-swarm orchestration).

The loop is fundamentally sound. You're not breaking it; you're outgrowing its current architecture. That's exactly the right trajectory.
