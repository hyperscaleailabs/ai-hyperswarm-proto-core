---
tags:
  - article
  - persona/cto
---

# 30 Lessons In: The Loop is Stable, Now Optimize It

We've closed 30 full iterations of an autonomous engineering loop. The product you're looking at is not "does the loop work?" It's "what do we optimize next?" Here's my read of the current state.

## The Stability Checkpoint (Lessons 26–30)

Last five lessons: 5/5 pass rate. No catastrophic failures. Two of those (27–28) shipped significant infrastructure changes (adversarial review gate, synthesis memory) with zero iteration. One timed out (29), but the code passed CI. One was documentation (30).

This is stability.

Compare to lessons 23–25: three consecutive failures. Timeout, incomplete feature, missed governance artifacts. At lesson 25, you had to ask: "Is this loop viable?" By lesson 29, the answer is yes, and you're asking: "How do we run it more efficiently?"

That's the difference between a proof-of-concept and a system you can actually operate.

## The Timeout Signal (Lesson 29)

Lesson 29's 1200s timeout is important not because it failed, but because it *diagnosed*. The ticket didn't fit the budget. But look at the details:

- Agent timed out at 1200s during implement phase.
- CI passed remotely.
- No corruption, no partial merge, no cascading issues.

This is exactly what you want from a timeout: clean diagnostics, no collateral damage, and a clear signal of where the boundary is.

The lesson learned: **Your current per-ticket budget is 1200s wall-clock at the standard model tier.** That's your SLA. Anything bigger times out.

## What That Means Operationally

At 1200s per ticket, assuming 5 tickets per block and 1 block per ~4 hours, you're doing roughly 5 tickets per 4 hours = ~1 ticket per 48 minutes of calendar time, including overhead.

That's 20 production PRs per day if you run this 24/7. At our current throttle (2 reviews per day), we're nowhere near capacity. But if you scale the synthesis phase (more tickets per block), you'll start queuing.

**Operational metrics you should track:**

- **Lessons per day**: Currently ~6 (since we started 2026-08-14 with 30 lessons accumulated). If this dips, something is slowing down.
- **First-pass merge rate**: Currently ~66% (2/3 recent impl tickets). Target: >80%.
- **Timeout rate**: Currently ~33% (1/3 recent impl tickets). Target: <10%.
- **Escalation rate** (once you build escalation): Currently N/A. Target: <5%.

## Lean System Tuning: Three Levers

You have three places to pull to optimize:

### Lever 1: Model Selection
Lesson 29 used `sonnet`; lesson 28 also used `sonnet` and landed clean. That's 50/50 on identical conditions—workload variance, not a trend. But if you tracked model choice vs. outcome, you might find:

- `haiku` (light): 90% pass, 5% timeout, fast.
- `sonnet` (standard): 66% pass, 33% timeout, medium.
- `opus` (heavy): 95% pass, 0% timeout, slow.

If that's what the data shows, your next move is obvious: route complex tickets to `opus` and save `sonnet` for routine stuff.

**Action:** Instrument model choice in every lesson. Start tracking pass rate per model by ticket complexity (inferred from synthesis phase description).

### Lever 2: Synthesis Decomposition
If synthesis could recognize a complex ticket and split it into two smaller ones, you'd convert timeouts into multiple passes. This requires synthesis to be smarter, but it's learnable.

**Action:** When lesson 29 times out, do a post-mortem: could this ticket have been split into two smaller ones? If yes, that's a practice to teach synthesis.

### Lever 3: Worker Budget
Right now, the worker has a hard 1200s limit. You could raise it, but that's a patch, not a fix. The real question is: should the limit even be fixed?

A smarter approach: let the worker run until it hits 80% of budget, then commit what it has and escalate. This trades a hard timeout for a soft threshold, giving you time to escalate gracefully.

**Action:** Implement soft-threshold escalation at 960s (80% of 1200s), then hard-fail at 1200s.

## The Knowledge Flywheel

Lessons 27–28 (gates and memory) are infrastructure features that operate *on* the loop's own behavior. They're working—both shipped clean. This is the beginning of the loop learning to improve itself.

The next step: feed lesson outcomes back into synthesis. When synthesis sees "lesson 29 timed out on `sonnet`," it should *use* that fact to avoid proposing similar tickets to `sonnet` in the future.

Right now, each lesson is recorded but not actively used by the synthesis phase. You're documenting; you're not yet *learning* from documentation.

**Action:** Implement lesson-retrieval memory in the synthesis prompt. When synthesis generates the next batch of tickets, include a summary of recent lesson outcomes and timeouts. This closes the feedback loop.

## The Revenue Model Question

If you're considering offering this loop as a service, lesson 29 tells you the pricing model:

- Standard tier: 1 complex ticket per day (given the 1200s limit and overhead).
- Premium tier: multiple complex tickets per day (if you route to `opus` or parallelize).
- Escalation tier: complex tickets go to humans who can spend hours on them.

You're not selling "unlimited autonomous PRs." You're selling "autonomous routine PRs + escalation for hard problems + human review at scale."

That's a different value proposition, and it's honest about your limits.

## One Caution: Optimization Theater

Be careful not to spend all your energy optimizing the per-ticket runtime when the real bottleneck might be elsewhere. Track these too:

- **Synthesis time**: How long does it take to generate the next batch of tickets? (Should be <10 min, or synthesis is the bottleneck.)
- **Review time**: How long are the two manual reviews per day taking? (Should be <30 min each, or review is the bottleneck.)
- **CI time**: How long is remote CI taking? (Should be <10 min, or CI is the bottleneck.)
- **Merge time**: How long from PR open to merge? (Should be <2 hours, or the gate is slow.)

If worker time (1200s) is only 20% of total cycle time, optimizing worker time is premature.

## Bottom Line

The loop is stable and serviceable. Your job now is to:

1. **Instrument everything:** Model choice, ticket complexity, timing, outcomes.
2. **Feed back the data:** Use lesson outcomes to steer synthesis and model routing.
3. **Gracefully degrade:** When the loop hits its limits, escalate, don't crash.
4. **Monitor for trends:** One timeout is variance; three timeouts in a row is a signal.

You have a working system. Now make it observable and learnable. That's how you move from "proof of concept" to "production service."
