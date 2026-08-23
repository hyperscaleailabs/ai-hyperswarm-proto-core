---
tags:
  - article
  - persona/cto
---

# The Resource Constraint Is Now Visible – And Measurable

Three timeouts in three days. That's data, not noise. As the CTO, this is your signal to act.

## The Technical Signal

| Lesson | Feature | Model | Timeout | Wall Clock | CI Pass |
| --- | --- | --- | --- | --- | --- |
| 34 | adopted-practice registry | sonnet | yes | 1200s | yes |
| 35 | failure taxonomy | sonnet | yes | 1200s | yes |
| 36 | retrieval-grounded synthesis | opus | yes | 1200s | yes |

Three different problems. Two models (sonnet, opus—the extremes of the fleet). All three hit the wall at 1200s. All three passed CI, meaning the code changes are valid.

This is not ambiguous. It's a **systematic resource constraint**, not a random failure.

## What's Happening Under the Hood

In each case, the agent is likely:
1. Reading a large dataset (all lessons, all failures, etc.)
2. Running inference over that dataset (tokenizing, embedding, analyzing)
3. Writing output (new registry, new taxonomy, new synthesis)
4. Running validation

With sonnet, step 2 gets expensive. With opus, step 3 (writing) gets expensive. But the boundary hits them all.

If this were a single ticket, I'd say "maybe it's a memory leak or an infinite loop." But three tickets? Three models? One wall? That's a **time budget constraint**, not a bug.

## The Options (With Cost-Benefit)

### Option A: Increase Wall-Clock (1200s → 1800s)

**Pro:**
- Simplest change. One config change.
- Lets us test if these features just need more time.
- Low risk to existing tickets.

**Con:**
- Increases per-ticket latency by 50%.
- If features still timeout at 1800s, we've just wasted 30 minutes per ticket.
- Doesn't address whether 1800s is truly enough or just delays the wall.

**Cost:** 600s per ticket × number of tickets run per day. If we run 10 tickets/day, that's +100 minutes/day of wall-clock. In quota terms: likely +15–20% per token-per-ticket if the features actually *do* complete at 1800s. Risky if they don't.

### Option B: Decompose (1 ticket → 3 tickets of smaller scope)

**Pro:**
- Each smaller ticket likely completes in <1200s.
- Parallelizable (all three can run concurrently, finishing in 1200s wall-clock instead of 3600s serial).
- Teaches the loop to break hard problems into subtasks (good for lesson 42: learned heuristics).

**Con:**
- Requires architectural changes to ticket generation (synthesis must know how to decompose).
- Three tickets = three PRs = three lessons = more governance overhead.
- Harder to implement quickly.

**Cost:** Higher upfront engineering cost. But lower quota cost long-term if subtasks are truly smaller.

### Option C: Async/Background Processing (Fast 70% + Slow 30%)

**Pro:**
- Synthesis completes quickly with a minimal result.
- Background job enriches it over time (not on critical path).
- Keeps cycle time predictable.

**Con:**
- Most complex to implement.
- Requires queue infrastructure (unlikely available now).
- Defers value (synthesis output is incomplete until background job finishes).

**Cost:** High engineering cost, lower ticket cost, but defers delivery.

## My Recommendation: Option A → B

1. **Immediate (1–2 hours)**: Set wall-clock to 1800s. Re-run lessons 34, 35, 36.
   - If all pass: Adopt 1800s. Run lesson 37–40 at 1800s. Monitor quota impact.
   - If any fail: Move to step 2.

2. **Short-term (4–6 hours)**: If step 1 fails, decompose lesson 34 into three smaller tickets. Design the splits so each finishes in ~600s (well under 1200s).

3. **Medium-term (1–2 days)**: Apply the same decomposition logic to lessons 35 and 36 if they still fail at 1800s.

4. **Long-term (1 week)**: Teach synthesis to estimate ticket complexity and auto-decompose (this is part of lesson 42: learned model-selection heuristic).

## Why Not Just Go Straight to Decomposition?

Because **experiments beat assumptions.** I don't know if 1800s is enough. You don't know either. The data will tell us.

If 1800s works, decomposition is unnecessary—we just adjusted our time budget. If 1800s doesn't work, we've gathered one data point that helps us design the decomposition strategy better.

## The Quota Impact

Assuming we retry lessons 34–36 at 1800s (if needed):

- Lesson 34 at 1800s: ~20% quota increase over 1200s (if it completes)
- Lesson 35 at 1800s: ~15% quota increase (smaller task)
- Lesson 36 at 1800s: ~20% quota increase

**Total impact if all three pass at 1800s**: ~+17% per ticket, one-time. If we run 10 tickets in the next 3 days, that's roughly +17% × 3 tickets = +51% total quota spend. Expensive, but one-time.

**Breakeven:** If increasing from 1200s to 1800s lets us eliminate one retry cycle (lessons 34–36 currently consume 3 × 1200s of wall-clock), the net spend is neutral. We're trading serialized failures for parallel attempts.

## What Needs to Happen First

1. **Confirm the wall is real**: Retry one failed lesson (34) at 1800s. Get data.
2. **If it works**: Retry 35 and 36.
3. **If any still fail**: Call an architecture review before trying decomposition.

I'm initiating this experiment. Lessons 34–36 will be re-run at 1800s wall-clock in the next cycle. I expect to have results by block 41376.

