---
tags:
  - article
  - persona/cto
---

# Operations Update: 30 Lessons, Holding at a Boundary

At lesson 30, the loop is healthy but facing a hard constraint. You need to make a decision about escalation policy.

## The Operational Status

**Governance:** Green. All governance infrastructure is working — tickets file cleanly, PRs link to tickets, CI gates merges, lessons are captured, MOCs stay updated. The infrastructure layer is holding under self-modification (lessons 27–28 added gates and memory to the loop itself and landed cleanly).

**Pass rate (last 3 lessons):** 1/3 (33%). Both failures are the same ticket, both timeouts, both with CI passing. This is not a regression from earlier blocks; it's a new class of failure.

**First-try success:** Lesson 27 (gate): ✓ Lesson 28 (memory): ✓ Lesson 29–30 (execution feature): ✗✗

## The Constraint

Lesson 29 and 30 are two attempts at the same ticket: "verifiable subscription-only execution and real agent telemetry." Both hit 1200s wall-clock timeout during the implementation phase. **Both completed CI successfully.** This means:

1. The timeout is not a crash — the worker stopped cleanly
2. The failure is not a test failure — CI passed
3. The problem is: `sonnet` model cannot complete this ticket in 1200s

The ticket is real. The feature is valuable (subscription verification, agent telemetry). The implementation is valid (CI passes). The only issue: the per-worker budget is too small.

## Three Operational Choices

**Choice A: Escalate on timeout (low cost, immediate unblock)**
- Policy: If a worker times out twice on the same ticket, route the next attempt to `opus` (heavier model) or escalate to human review
- Cost: Slightly higher token spend on hard tickets
- Benefit: Loop doesn't stall, humans stay in the loop for hard problems
- Timeline: Implementable in next block
- Risk: Low (doesn't change anything for passing tickets)

**Choice B: Model routing at synthesis (medium cost, smarter)**
- Policy: Synthesis learns to estimate ticket complexity; routes complex tickets to `opus` instead of `sonnet` by default
- Cost: More `opus` usage overall, but smarter allocation
- Benefit: Fewer timeouts, better resource utilization
- Timeline: 2–3 blocks to calibrate
- Risk: Medium (requires synthesis to be more sophisticated)

**Choice C: Ticket decomposition (higher cost, most scalable)**
- Policy: Synthesis learns to split hard tickets into subtasks
- Cost: More coordination overhead, more tickets to process
- Benefit: Scales to arbitrarily complex work
- Timeline: 3–4 blocks to implement well
- Risk: Medium–High (requires synthesis overhaul)

## My Recommendation

**Start with A (escalation).** Here's why:

1. **It's immediately implementable.** Add a counter for timeouts per ticket; on second timeout, escalate to `opus` or human.
2. **It unblocks the loop today.** You're not stuck waiting for a synthesis overhaul.
3. **It gives you data for B and C.** If you escalate lesson 29–30 to `opus`, you'll know if it finishes in time, which directly informs whether B (model routing) is worth it.
4. **It's low-risk operationally.** A human or heavier model taking on a hard ticket doesn't destabilize the loop.

Once escalation is working (block 41361), measure:
- How many tickets escalate? (target: <5%)
- Does `opus` finish them? (if yes, model routing is gold)
- Can synthesis propose smaller subtasks? (if yes, decomposition is next)

## What Not To Do

**Do not** raise the 1200s limit to 1800s or 2400s. That's a patch. You'll just find a different limit later. Instead, make the loop aware that limits exist and have a graceful degradation path.

## For Compliance and Tracking

- **Current state:** 30 lessons, governance solid, holding at scheduling boundary
- **Next ticket:** Implement escalation policy for timeout+retry cases
- **Success metric:** Lesson 31 successfully completes a previously-timeout ticket (either via `opus` or escalation)
- **Tracking:** Monitor timeout rate lesson-to-lesson; if it drops after escalation lands, you've fixed it

The loop is not broken. It's asking you a question: "What's your policy when I hit my limit?" Answer that question cleanly, and you move from "autonomous but brittle" to "autonomous and resilient."
