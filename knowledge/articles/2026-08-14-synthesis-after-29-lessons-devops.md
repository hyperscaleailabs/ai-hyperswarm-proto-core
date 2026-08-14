---
tags:
  - article
  - persona/devops
---

# CI/CD Signals from 29 Lessons: What the Timeout Means

Three lessons, two passed, one timed out. Here's what the automation layer is telling us:

## What Shipped Clean (Lessons 27–28)

**Lesson 27** (adversarial review gate): Added a pre-merge check where multiple models independently review a PR before auto-merge. This is a new gate in the CI pipeline. It passed all checks and merged clean.

**Lesson 28** (synthesis memory): Added a feature to track what proposals the loop has already made, so it doesn't waste budget re-proposing the same idea. This touches the synthesis phase. Merged clean, no flakiness.

Both of these added complexity to the pipeline without adding flakiness. That's the signal you want: gates, guards, and memory mechanisms all working as expected.

## What Broke (Lesson 29)

**Lesson 29** (verifiable subscription-only execution) hit the 1200s wall during the implement phase in the worker sandbox. **The CI result was SUCCESS.** This matters because:

- The agent's local timer ran out (1200s hardlimit).
- But the agent did not crash or produce partial work.
- And CI (remote) completed normally.

This tells you the breakdown was not "CI is flaky" or "the change is broken." It's "the local worker's budget is smaller than the ticket's actual complexity."

## What This Means for Your Infrastructure

**1. Your budget model is real and working.** The 1200s limit is enforced. It's not a suggestion; it's a wall.

**2. You have a divergence: local worker vs. remote CI.** The agent timed out locally but CI passed remotely. This is a known constraint in the project (workers can't run pytest directly), but it's now visible at scale: a feature can time out in the agent's local context but still be deemed CI-pass-worthy.

**3. The CI gate is holding.** You haven't seen a false negative (CI passes, code is broken) or a false positive (CI fails, code is fine). The gate is reliable, just not omniscient. It can't catch timeouts in the worker's simulation phase.

## Operational Implications

- **The 1200s limit is your scheduling bottleneck, not your CPU or memory.** Raising it is a band-aid. Smarter ticket decomposition or model routing is the real fix.
  
- **You need a "timeout is not failure" path.** When lesson 29 timed out, it should have escalated, not died. Right now it becomes a lesson ("fail"), but the actual ticket (#220) is still in backlog. Add tracking for that.

- **Watch for timeout clustering.** If timeouts start happening in batches (lessons 30–32 all timing out), that's a signal the loop's history or synthesis prompt is getting too expensive. If timeouts are sporadic (29 times out, 30 passes, 31 times out), that's just variance and doesn't require infrastructure changes.

## The Audit Trail

Here's what the CI logs should show for lesson 29:

```
[worker] iteration=4135705 ticket=220 model=sonnet
[worker] implement phase: start
[worker] ...
[worker] wall_clock=1200s: timeout (no error, clean exit)
[ci] remote test run: SUCCESS
[harness] outcome=fail (timeout despite CI pass)
```

If that's what you're seeing, your instrumentation is working correctly. The apparent contradiction (CI=pass, outcome=fail) is actually correct: the agent failed to complete, but the thing it *did* submit passes CI.

## Monitoring Going Forward

Add these dashboards:

1. **Lesson completion rate** — what % of lessons complete within budget? (Target: >90%.)
2. **Timeout per model** — segment timeouts by which model was used. If `sonnet` has 40% timeout rate and `opus` has 10%, that's your routing signal.
3. **First-attempt pass rate** — out of lessons that complete within budget, what % pass CI on the first try? (Target: >80%.)

Lesson 29 gives you a baseline: 33% timeout (1 in 3 recent lessons), but only among implement-phase complexity. Observe whether that holds or degrades.

## The Path Forward

Don't change anything in CI yet. The gate is working. What needs to change is:

1. **Worker escalation**: Timeout → escalate, don't just fail.
2. **Model routing**: Complex tickets → heavier models, simpler tickets → lighter models.
3. **Synthesis decomposition**: If a ticket is too big, propose two smaller ones instead of one large one.

All of these are above the CI/CD layer. Your job is to make sure the timeout signal is *visible* to the thing that can act on it (the synthesis engine or the escalation handler).

Currently, the signal is visible in the lesson file. That's a start. Next step is to plumb it into the live loop's decision-making, not just the retrospective.
