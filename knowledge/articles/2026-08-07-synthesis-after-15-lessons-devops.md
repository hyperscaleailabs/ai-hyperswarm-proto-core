---
tags:
  - article
  - persona/devops
---

# Five Green Runs: What the Automation Loop Actually Taught Us

We just closed a window of five lessons through our autonomous build loop — 2 "implement" tickets, 3 "improve" tickets, 5/5 pass, zero failures. No incidents to dissect this time, which is its own kind of signal worth being honest about: a clean window tells you as much about what your gates *aren't* catching as what they are.

## What shipped

- A model-selection skill that routes tasks to the right model tier by complexity, instead of defaulting everything to the biggest/most expensive option.
- A fake-runner integration test suite covering the orchestrator's run-once, heal, and implement paths — meaning we can now exercise those control-flow branches without spinning up real infra.
- A reference-set snapshot refresh plus one extracted reusable practice from prior runs.
- Explicit phase artifacts modeled on MetaGPT's approach, making intermediate pipeline state inspectable instead of implicit.
- Retry-and-CI-parity work aimed at loop reliability — making local retry behavior match what CI actually enforces.

## The mechanics that mattered

The two structural changes worth calling out for anyone running similar automation:

**Fake-runner integration tests.** Before this, orchestrator paths (run-once, heal, implement) were only exercised by real end-to-end runs — slow, flaky-prone, and expensive to iterate on. Swapping in a fake runner let us hit the same control-flow branches deterministically in CI. This is the kind of investment that doesn't show up as a feature but pays down flakiness debt every subsequent run.

**Retry/CI parity.** We had a gap where local retry logic and CI's retry/failure semantics didn't agree — the kind of drift that produces "works on my machine, flakes in CI" tickets. Closing that gap was framed explicitly as a loop-reliability fix, not a feature.

## Honest caveat: no failures isn't the same as no risk

Five-for-five is good, but a synthesis with zero recurring failures should raise an eyebrow, not lower one. It likely means one of: the ticket sizing in this window stayed conservative, the regression/heal gates from earlier work are doing their job, or failure modes are being caught and silently retried before they surface as a "fail" outcome. We don't have the breakdown to say which. If the next window is also all-green, that's worth actively checking — either the gates are excellent, or the loop is only being fed the easy tickets.

## Recurring themes

The lesson notes repeatedly reference "build," "change," "cleanly," "green," and "merged" — consistent with a loop that's optimizing for keeping the pipeline unbroken on every merge, not just at release boundaries. That's the right instinct for CI/CD hygiene, but it's worth periodically stress-testing with a deliberately harder ticket to confirm the gates catch real breakage, not just the easy stuff.
