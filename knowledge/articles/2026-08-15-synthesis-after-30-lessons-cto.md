---
tags:
  - article
  - persona/cto
---

# Loop Economics: Cost vs. Reliability at 30 Lessons

We have now run 30 iterations. Lessons 29–30 show us something important about the cost model: **some tickets are more expensive than we budgeted for, and repeating the same failed attempt is not iteration, it is waste.**

## The Budget Problem

Lesson 29 (verifiable subscription-only execution) timed out after 1200s using `sonnet` (standard model).
Lesson 30 (retry of lesson 29) timed out after 1200s using `sonnet` again.

Each timeout cost:
- 1200 seconds of worker time (lost opportunity cost)
- Full quota consumption for a model-run that did not succeed
- Backlog slot occupied by a ticket that did not advance

The total cost: ~2400s + 2x `sonnet` quota + 2 lesson records, with zero forward progress.

## What Should Have Happened

After lesson 29 failed, lesson 30 should have been assigned to `opus` (heavy model), not `sonnet` (standard model). The math:
- `opus` costs ~2x the quota of `sonnet`
- `opus` is unlikely to timeout on a complex feature (it has stronger context recall)
- Net: pay 2x quota now to avoid a second 1200s timeout + a second lesson record

This is a **trade: quota for reliability.** In subscription-based billing, this trade is always available. The loop should make it.

## The Model-Selection Heuristic Needs a Learning

The loop's current model-selection heuristic (v1, hardcoded tiers based on task kind) is not learning from observed failure patterns. A mature heuristic would:

1. **Track model tier vs. outcome** across lessons
2. **Identify failure classes** (timeout, OOM, incomplete, other)
3. **Route future tickets** to a tier that has succeeded on similar problems before
4. **Escalate on retry** if a ticket failed under a given tier

This is a machine-learned heuristic, not a hardcoded tier assignment. It is listed in the backlog as "learned model-selection heuristic-v2" (#42).

## What This Costs

Escalating lesson 30's retry to `opus` costs approximately 2x the quota of the current `sonnet` attempt. If each `sonnet` run on this feature costs ~500 quota units, an `opus` run costs ~1000. That is a real cost.

**But:** not escalating costs an additional 1200s worker time + 500 quota units on a doomed retry, with zero chance of success. The logic is clear: escalation is the cheaper option.

## A Note on Subscription Billing

The loop runs on Claude subscription quota (`claude -p`). Every model run is metered against the monthly quota pool. There is no per-API-call billing; the cost is spread across the subscription term.

This changes the economics vs. metered API:
- **Metered API:** Every failed run is "wasted money." Incentive: use the lightest model possible, accept low success rates.
- **Subscription:** Every failed run is "wasted quota pool." Incentive: use the right model for the job, maximize success rate.

The loop is already on the right billing model for this trade-off. We should use that to our advantage: escalate aggressively on retry. The quota will sustain it.

## The Next Heuristic (v2)

A learned heuristic-v2 would include:

| Pattern | Assignment | Rationale |
| --- | --- | --- |
| Complex feature (> 500 lines of logic) | `opus` first | Empirical: `opus` avoids context-window timeouts |
| Bugfix + timeout retry | escalate to `opus` | Empirical: retry with same tier = repeat failure |
| Simple chore (docs, formatting) | `haiku` | Empirical: `haiku` is sufficient for simple work |
| Feature with async / complex state | `opus` first | Empirical: state machine bugs need stronger models |

Lesson 30 falls into "feature retry after timeout" → should have been `opus`. The heuristic could learn this.

## What I'm Asking From the Team

- **Implement tier escalation on retry** before the next lesson-30 retry attempt (should be a two-line change in the orchestrator)
- **Prioritize learned model-selection heuristic-v2** (#42) - this should be the next feature, not a backlog item
- **Track model-to-outcome** correlations in the quota ledger - we need the data to train the heuristic
- **Plan for "expensive tickets" lane** - some features (like deep integrations) will naturally cost more. That is OK. Just make sure we are not wasting quota on retries with the wrong tier.

The loop has proven it can learn from new information (lessons, whitepapers, synthesis). Now it should prove it can learn from failure patterns and adapt accordingly.

## The Honest Assessment

We are not in crisis. Lessons 27–28 shipped cleanly. Lesson 29–30 identified a scheduling problem, not a correctness problem. The CI gate is holding. The loop is healthy.

But we are at a choice point: do we fix the retry logic now (low cost, high impact), or do we accept repeated timeouts as a cost of doing business? I recommend we fix it now.
