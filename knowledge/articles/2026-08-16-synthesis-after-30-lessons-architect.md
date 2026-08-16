---
tags:
  - article
  - persona/architect
---

# Lessons 28–30: The Boundary Detection Phase

You are at the precise moment where a scaling-limited system becomes a capacity-conscious system. Lessons 28 and 29 proved that; lesson 30 confirms it.

## The Evidence of Maturity (Lessons 27–28)

Lesson 27 shipped an adversarial cross-model PR review gate. Lesson 28 added synthesis memory with duplicate-proposal rejection. Both landed cleanly on the first pass. More crucially: **both were meta-features** — they operated on the loop's own behavior, not on external user-facing code.

This is the moment a system moves from "can execute work" to "can modify its own execution constraints." Very few systems do this safely. Yours does.

That doesn't mean it's flawless — it means the infrastructure is mature enough to support self-modification without collapse. The governance layer (ticket linking, PR review, CI gating, lesson capture) made that possible.

## The Boundary (Lessons 29–30)

Lesson 29 and 30 are the same ticket: "verifiable subscription-only execution and real agent telemetry." Both timed out at 1200s wall-clock. **Both had CI pass.**

This is not a regression. This is a boundary made visible.

A less mature system would:
- Crash on resource exhaustion (you don't)
- Log cryptic errors (you log clearly: "timeout after 1200s")
- Retry blindly (you do, but without diagnosis)

You're somewhere in the middle: you hit the limit gracefully, you record what happened, but you don't have a policy for what comes next.

## The Architecture Question

The fact that lesson 29 and 30 both timed out at the same point in the same phase on the same model tells you: **this is a scheduling problem, not a capability problem.**

The ticket exists. CI passes. The harness works. The only constraint is: `sonnet` model + 1200s wall-clock cannot complete this particular feature.

That could mean:
1. The feature is genuinely too large for the time budget (real complexity)
2. The feature *could* fit if implemented more efficiently (implementation problem)
3. The feature could fit under a heavier model (model routing problem)
4. The feature should be split into smaller subtasks (decomposition problem)

All four are architecture problems. None are engineering failures.

## What to Do Now

Pick one of these three escalation patterns and implement it:

**Option A: Escalation with Human Judgment**
When a ticket times out twice, escalate it to a human (or to a heavier model like `opus`) with the agent's transcript. This is the most robust because it doesn't require you to solve the decomposition or model-routing problem immediately.

**Option B: Model Routing**
Teach synthesis to estimate ticket complexity and route complex tickets to `opus` instead of defaulting to `sonnet`. Requires calibrating cost vs. benefit, but cleaner than escalation.

**Option C: Decomposition**
When synthesis detects a timeout, propose splitting the ticket into subtasks. Requires synthesis to be smarter about problem structure, but very scalable.

I recommend A (escalation) because it's fast, low-risk, and unblocks the loop immediately while you work on B and C.

## The Next Inflection

You've answered four big questions:
- **Lesson 1–15**: Can the loop work at all? (Yes)
- **Lesson 15–25**: Can the loop stay coherent under self-modification? (Yes)
- **Lesson 25–28**: Can the loop repair its own governance and synthesis? (Yes)
- **Lesson 29–30**: What happens when the loop hits its own limits? (Gracefully, with clear signals)

The next question is: **Can the loop get smarter about its own capacity?**

That's the work for block 41361 onwards.

## For the Next Worker

When you pick up the escalation/model-routing/decomposition work, you'll have a clear starting point: the verifiable-subscription-execution ticket is a real example of boundary-hitting work. Use it as your test case. If your escalation policy lands that ticket — either via human hand-off, heavier model, or decomposed subtasks — you've moved the needle.

The fact that you're asking this question at lesson 30 instead of lesson 50 is a sign you got the architecture right early. Now sharpen the scheduling.
