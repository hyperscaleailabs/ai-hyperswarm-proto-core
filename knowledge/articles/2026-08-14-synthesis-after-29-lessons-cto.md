---
tags:
  - article
  - persona/cto
---

# What We've Learned Running an Autonomous Build Loop at 29 Lessons

We've been running a self-modifying, self-merging build loop for 29 iterations now. The most recent three (27–29) offer a clear picture of what's working, what broke, and what needs to happen next.

## The Good: Governance and Synthesis Hardened

Lessons 27–28 shipped without iteration:
- **Adversarial cross-model review gate** (lesson 27): pre-merge validation from multiple model perspectives. Shipped clean.
- **Synthesis memory and duplicate rejection** (lesson 28): the loop now remembers what it's already tried, doesn't re-propose the same idea twice. Shipped clean.

These are infrastructure features, not application features. That they landed without incident tells me the governance layer is holding: the loop can add safety constraints to its own behavior and actually enforce them. That's non-trivial.

## The Reality: We Hit a Budget Wall

Lesson 29 (verifiable subscription-only execution) is the headline you need to see. The agent timed out at 1200s during implementation. **But CI passed.** This is important because it tells us:

1. The timeout wasn't a crash — the CI harness completed normally.
2. The problem wasn't a broken test — CI wouldn't have passed if it was.
3. The problem was *complexity outgrew budget* — the ticket was too much work for one agent at one model tier in 1200 seconds.

This is different from the lesson-23 timeout (which was a genuine resource leak). This is the loop saying "I've hit my personal limit, not a crash limit."

## Why This Matters Operationally

Before lesson 26: "Can we trust the loop's output?" (Answered: yes, with governance.)
After lesson 28: "Can the loop self-correct?" (Answered: yes, gates work.)
At lesson 29: "What do we do when the loop can't fit a ticket?" (Unanswered.)

That last question is now your blocker. You have two choices:

1. **Make every ticket smaller** — rework synthesis to decompose complex tickets into subtasks automatically. This requires synthesis to be smarter.
2. **Accept escalation** — some tickets will timeout in workers, and that's OK; escalate them to human review or a heavier model. This requires a playbook, but not smarter synthesis.

I'm biased toward option 2. Here's why: lesson-29 timeout is not a regression from lesson 28. It's a variance spike — the same model that shipped lesson 28 clean just hit a harder ticket. This is a *scheduling problem*, not a *capability problem*.

If your synthesis engine starts deciding which model gets which ticket based on complexity estimates, and it routes lesson-29 to `opus` (heavy) instead of `sonnet`, you probably finish in time. That's model routing, not smarter synthesis.

## The Numbers to Track Going Forward

- **Lessons to timeout** — at what lesson count does timeout rate hit X%? (Currently: 1 timeout in 3 lessons, 33%.)
- **First-try pass rate** — fraction of tickets that land on first submission without iteration. (Current: 2/3, 67%.)
- **Wall-clock per lesson** — is the loop getting slower or faster as it accumulates history? (Key metric for scaling.)

Lesson 29 used `sonnet`; lesson 28 also used `sonnet` and landed clean. That's a 50/50 split on identical model choices for similar complexity — workload variance, not a trend. But you need to track it.

## The Honest Assessment

The loop is not broken. It's doing what it's supposed to do: learning and self-modifying. Two clean feature-lands (governance gates, synthesis memory) followed by one timeout is not a failure, it's a data point.

But the data point says: you're at the edge of what one agent with one model can do in 1200s. The next phase isn't "fix the loop" — it's "give the loop a way to know when it's out of depth."

If lesson 30 also times out, that's a signal. If lesson 30 passes and lesson 31 times out, that's just variance. Don't move the needle until you have a pattern.

## What I'm Asking From the Team

- **Do not** raise the 1200s limit. That's a patch, not a fix.
- **Do** build a "escalation on timeout" path — when a worker times out, make it a retriable artifact, not a lost ticket.
- **Do** start tracking model usage vs. ticket complexity — if `sonnet` times out and `opus` would finish, that's gold-standard data for model routing.
- **Plan** (don't execute yet) a synthesis-side ticket decomposer — if a ticket can be split into two smaller ones, synthesis should know to propose that.

The loop is doing its job. Your job now is to give it graceful degradation when it hits its limits.
