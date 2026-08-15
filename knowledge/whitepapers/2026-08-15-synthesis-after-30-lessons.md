---
tags:
  - whitepaper
created: 2026-08-15
---

# Synthesis after 30 lessons

> Part of [[Whitepapers MOC]] - [[Knowledge Base MOC]]

## Summary
Synthesis of the last 1 lesson(s): 0 pass / 1 fail, across kinds implement.

## Outcomes in this window
| outcome | count |
| --- | --- |
| pass | 0 |
| fail | 1 |

## Work by kind
| kind | count |
| --- | --- |
| implement | 1 |

## Recurring failures
- **timeout / resource constraints** - block 41359 (lesson 30): verifiable subscription-only execution retry timed out at 1200s during phase=implement, despite CI passing (exact same failure as lesson 29)

## Recurring themes
- **timeout** - appears in 1 lesson
- **scheduling** - appears in 1 lesson
- **capacity** - appears in 1 lesson

## Lessons synthesized
- [[2026-08-14-implement-feat-verifiable-subscription-only-execution-and-real-agent-telemetry]]

## Analysis: confirmation of the boundary condition

Lesson 30 is a retry of lesson 29: the verifiable subscription-only execution feature, assigned to the same `sonnet` model tier, hit 1200s timeout again. This is not variance. This is a **stable failure mode** - the same ticket under identical conditions exceeds budget twice in a row.

The critical insight: **this is not a loop failure, it is a ticket assignment failure.** CI passed on both attempts. The agent did not crash. The feature itself may be implementable; we simply don't have the budget (in wall-clock time or quota) to implement it with the current model tier on the current hardware.

## What this tells us about the loop's maturity

After lesson 29, the hypothesis was "maybe this is just workload variance - lesson 28 used `sonnet` and passed, lesson 29 used `sonnet` and timed out." Lesson 30 now eliminates that hypothesis: **repeated failure on identical conditions is not variance, it is systematic.**

Three conclusions follow:

**1. The scheduler needs to escalate on retry.** When a ticket times out once, assigning it to the same model tier a second time is not iteration - it is waste. The correct policy: on timeout, escalate to `opus` (heavy tier) for the retry, or split the ticket into smaller pieces, or escalate to human review. Lesson 30 should not have been assigned `sonnet` after lesson 29's failure.

**2. The 1200s boundary is real and predictable.** This is not a mysterious edge case. We have now observed two stable timeouts on a ticket of known complexity. We can measure it. We can teach the model-selection heuristic to recognize it and route around it.

**3. The loop's self-awareness is incomplete.** The loop filed lesson 29, recorded it as a failure, and then... filed lesson 30 with identical parameters. No escalation logic. No "try a heavier model this time" logic. The loop can synthesize new features and merge them; it cannot yet reason about "this class of ticket needs a heavier model" based on failure patterns.

## What worked: governance recording

Lessons 27–29 all have complete, auditable lesson records. Every failure is documented with the exact error, the model used, and the wall-clock timing. This means we can:
- Identify repeated failure patterns (done: lesson 30 matches lesson 29 exactly)
- Extract the feature tickets and escalate them to human review (lesson #220 is now clearly a candidate)
- Update the model-selection heuristic with a new rule: "complex features with tight integration (like subscription-only verification) → try `opus` first, not `sonnet`"

The governance layer is working. It is recording failures faithfully and making them visible. The gap is not in recording; it is in *action* on those records.

## What deteriorated: decision-making on retry

The loop should have detected lesson 29's failure and made a different choice on lesson 30. It did not. This points to a gap in the loop's retry logic - specifically, the loop treats retry as "run the same agent again" rather than "run a smarter agent on the same ticket."

This is tractable. Add a rule to the scheduler:
- On timeout retry, promote one tier (sonnet → opus)
- On other failure retry, keep the same tier but add error context from lesson N-1
- On second timeout retry, escalate to human review

## Takeaway for the block

Block 41359 is the inflection point where the loop learns that **budget allocation is a learned skill, not a static constant.** The loop has proven it can synthesize, implement, merge, and record. Now it needs to prove it can route work to the right resource based on past outcomes.

The path forward: implement tier-escalation-on-retry (small change), update the model-selection heuristic (small change), and commit both as a feature. That change will likely cause lesson 30's retry to land on the first try with `opus` instead of timing out again.

The loop is not broken. The loop is learning what it costs to do hard things, and that learning will shape the next iteration.
