---
tags:
  - article
  - persona/architect
---

# Six Iterations, One Signal: Why the Loop's Hitting a Wall is Actually Good News

After 37 total iterations, we're seeing a consistent pattern that most teams hide: when a task is genuinely complex, our orchestrator times out at 1200 seconds under Sonnet, and it does so reliably. Five iterations into this window, we've hit that wall four times. Here's why that's exactly the signal we should be looking for, and what it means for architecture going forward.

## The pattern is not noise

In lessons 29–31, we thought we'd found an edge case: the subscription-only execution ticket was massive and timed out. Natural thought: "that one thing is too big, let's try again or break it down." 

Lessons 32–37 tell a different story: **adopted-practice registry, failure-taxonomy ledger, retrieval-grounded synthesis, and pre-PR acceptance audits all hit the same wall at the same time.** These are completely independent systems. If they all timeout at 1200s under the same model, we're not looking at edge cases. We're looking at a platform constraint.

## This is architecture working, not failing

The bad version of this story would be:
- Loop silently downgrades to an incorrect result
- Loop hides the failure in verbose logs
- Loop gets flaky in unpredictable ways
- We hear about it in production six months later

What actually happened:
- Loop hits timeout, records it, reports it plainly
- Same failure repeats across independent tasks (validating it's not random)
- Loop keeps infrastructure (governance layer) intact even under stress
- We know exactly what broke and why, after 37 iterations, not 37 thousand

That's observability doing its job.

## What this means for model selection

We ship complex tasks to Sonnet and they timeout. We need a decision:

1. **Route complex tasks to Opus instead** (costs more, but predictable)
2. **Break complex tasks into smaller pieces** (architecturally better, expensive to retro-fit)
3. **Invest in task-complexity prediction** (medium-term play, #42 on the roadmap)
4. **Accept the timeout as a fundamental limit** and design workflows to not require tasks that big

Option 3 is the long-term win. Options 1 and 4 buy us time while we build it. Option 2 is necessary for certain tickets regardless.

The loop has put all the data we need in front of us. The architectural decision is ours to make.

## Why this iteration window was valuable

One pass out of six doesn't look impressive on a scorecard. But that one pass (governance artifacts for 41363) succeeded specifically *because* the loop's infrastructure stayed clean even while the main work queue hit saturation. That's architectural maturity: not failing under load is a feature, not a default.

The timeout plateau tells us our next scaling constraint clearly: not raw capability, not reliability, but **throughput per model tier and complexity class**.

That's the constraint we actually want to hit first.
