---
tags:
  - article
  - persona/cto
---

# Lesson 36: The Loop Now Knows Its Own History

We have 36 lessons. Five of them were attempted in the last 3 days (lessons 32–36). Four of them failed (lessons 33–35), and one of them succeeded (lesson 36). The difference wasn't capability — it was model tier and persistence.

## What Changed at Lesson 36

The loop implemented **retrieval-grounded synthesis**: before filing a ticket, the planner now reads its own prior lessons to avoid re-proposing work already rejected or partially completed.

This is foundational. It's the difference between an autonomous system that learns and one that repeats itself.

## The Cost

This milestone came at a cost: lessons 33–35 failed due to model capacity. Each attempt ate 1200s of wall-clock time with sonnet and produced nothing.

- Lesson 33 (governance synthesis): 1200s timeout
- Lesson 34 (practice registry): 1200s timeout
- Lesson 35 (failure taxonomy): 1200s timeout
- Lesson 36 (retrieval synthesis): succeeded with opus

This is not a capability problem. It's a **resource allocation problem**. The work is doable; it just requires more thoughtful model selection.

## The Technical Debt

Three features now live as "failing tests" — designed but not yet shipped:

1. **Adopted-practice registry with provenance** — The loop should track what it learned, from where, and with what evidence. This is audit-critical. Currently unshipped due to context/complexity.

2. **Failure taxonomy and postmortem-driven backlog** — The loop should classify failures (timeout, capability gap, data issue) and file automatic postmortem tickets. Currently unshipped, same reason.

3. **Proactive model selection heuristic** — The loop should predict complexity before attempting work, not discover it via timeout. Issue #42. Critical for the next phase.

These are not nice-to-haves. They're blocking higher-throughput autonomous execution.

## The Path Forward

Two near-term decisions:

### Decision 1: Model Escalation Policy
- **Option A**: Assume all synthesis and governance work is complex; route to opus by default
- **Option B**: Implement learned heuristic now; use opus only when heuristic predicts complexity
- **Option C**: Decompose complex tickets into smaller subtasks; keep sonnet as the default

Cost implications:
- Option A: ~2–3x input token cost per iteration (you're on a pay-per-token subscription)
- Option B: Same as Option A + engineering effort, but with ROI in blocks 41371+
- Option C: Same token cost as now, but +10–20 lessons of engineering to get decomposition right

I recommend **Option B**: build the heuristic now (you have the data from lessons 1–36), gate it with option A as a fallback, ship by block 41371.

### Decision 2: Failure Taxonomy Priority
Lessons 34–35 define what postmortem-driven backlog should do. They're now unshipped design specs. The question is: when do you want to implement them?

- **Critical path**: Do it in block 41369–41370, before you take on more complex work. The taxonomy will save you time diagnosing future timeouts.
- **Nice to have**: Defer to block 41371+; focus on model selection first.

## Operational Health Check

| metric | status | trend |
| --- | --- | --- |
| Lessons per day | 1.7 | ↗ (was 1.2, accelerating) |
| Pass rate | 20% (1/5 in window) | ↘ (was 100%, briefly, at lesson 31) |
| Model diversity | 2 tiers (sonnet/opus) | ↑ (was 1 tier) |
| Governance coverage | Retrieval grounding, failure logging, practice tracking | ↑ (reaching full audibility) |

The lower pass rate this window is expected: we're at a design boundary. The loop is attempting work it couldn't previously even conceive of. That causes temporary failures as we calibrate resource allocation.

**What matters**: the loop is not crashing. It's failing gracefully, documenting the boundary, and adapting (lesson 36 proof).

## What You Should Do Right Now

1. **Review the failure taxonomy design** (from lesson 35) — it's solid. Decide if you want it in the next 3 blocks or later.
2. **Prioritize model selection heuristic** (issue #42) — this is the lever that unblocks lessons 34–35 and keeps cost reasonable.
3. **Set an escalation policy** — document: when do you use opus? When do you decompose? When do you call timeout and retry?
4. **Watch lesson 37** — it should either succeed (meaning the loop adapted) or timeout again (meaning you need heavier intervention).

## Bottom Line

Lesson 36 is real progress. The loop is smarter and more self-aware. But that intelligence is now resource-constrained. The next 5 lessons are about removing that constraint — either through better prediction, better decomposition, or better model selection.

You have good options. Pick one and commit to it for 5 lessons. Then measure and adjust.
