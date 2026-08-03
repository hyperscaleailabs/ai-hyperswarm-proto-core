---
tags:
  - article
  - persona/cto
---

# Autonomous Engineering Loop: Status After 15 Lessons

## The short version

We've been running an AI-driven development loop that takes on real engineering tickets — implementations and improvements — end to end, then writes up a "lesson" after each one: what it built, what broke, what it learned. This note synthesizes the most recent window: 5 tickets, 5 passes, zero failures.

## What actually happened

Of the last 5 tickets, 2 were new implementations and 3 were improvements to existing work. All 5 merged cleanly and left the build green. No regressions, no reverts, no rollback tickets in this window.

That's a good result, but it's a small sample, and I want to be precise about what it does and doesn't prove. Five clean merges in a row is consistent with the loop working well. It's also consistent with this window simply drawing easier tickets — the two implementation tasks were scoped narrowly (a model-selection heuristic and a test-fake for the orchestrator's run/heal/implement paths), not open-ended feature work. We haven't yet stress-tested this loop against a genuinely ambiguous or architecturally risky ticket, and I don't want to claim more confidence than the data supports.

## What failed — honestly, nothing did, this time

I'd normally lead a report like this with failure modes, because that's where the real signal is. This window doesn't have any: no test flakiness, no CI breakage, no work that had to be thrown away. The most useful thing I can say is what's conspicuously *not* here yet — there's no incident in this batch where the loop shipped something plausible-looking that was actually wrong. That's either a sign the guardrails (review rhythm, phase artifacts, reproduce-before-fix checks) are catching problems before they land, or a sign we haven't yet given it a ticket hard enough to break them. I'd bet on the former based on the design, but I can't yet prove it from outcomes alone.

## Risk posture

The recurring themes across these lessons — "build," "change," "green," "merged" — describe a loop that is optimizing correctly: it treats a clean, green merge as the unit of success rather than raw output volume. That's the right incentive structure. The risk I'm watching is sample bias: a streak of passes on low-ambiguity tickets doesn't validate the loop on the tickets that actually carry business risk (schema migrations, auth changes, anything cross-service). We should deliberately route one or two higher-stakes tickets through this loop soon, specifically to find the failure mode, rather than continuing to bank clean streaks on safer work.

## Where this is heading

Strategically, the value case here isn't "5/5 passed" — it's that the loop now produces its own audit trail (lessons, synthesis, recurring-failure tracking) automatically, which is what will let us scale it past hand-picked tickets without losing visibility into when it starts failing. The next window should include at least one ticket chosen specifically because it's likely to break something, so the "recurring failures" section of this report actually has data in it.
