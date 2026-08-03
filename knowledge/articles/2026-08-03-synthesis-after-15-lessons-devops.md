---
tags:
  - article
  - persona/devops
---

# Five Green Runs, One Retry Fix: Lessons from an Automated Build Loop

Our last five automated build/improve cycles all passed — 2 new implementations, 3 improvements, zero failures. That's a good outcome, but a five-run green streak is a small sample, and the more interesting signal is in what the loop had to fix about itself along the way, not just what shipped clean.

## What actually happened

Two of the five lessons were about testing infrastructure catching up with automation, not the automation itself:

- **Fake-runner integration tests** were added for the orchestrator's `run`, `once`, `heal`, and `implement` paths. Translation: those code paths were previously exercised only by real runs, not integration tests — meaning bugs in orchestration logic could only surface in production-shaped runs, not CI. Adding a fake runner closes that gap without needing a live environment for every test.
- **Loop reliability work** targeted retry behavior and CI parity specifically. Parity gaps between CI and the local/loop environment are a classic source of "works in the loop, fails in CI" (or vice versa) — the fact this needed a dedicated fix means the loop and CI had drifted in assumptions (timing, retries, environment state) at some point before this window.

The other three were governance and cost mechanics:

- **Task-complexity-based model selection** — routing cheaper/faster models to simple tasks and reserving higher-capability models for complex ones, presumably to control spend without hand-tuning every call.
- **Reference-set snapshot refresh** — a recurring chore to keep a baseline/reference dataset current, extracted as a reusable practice rather than a one-off fix.
- **Explicit phase artifacts** (borrowing from the MetaGPT pattern) — making intermediate build phases produce inspectable artifacts instead of black-box transitions.

## The honest caveat

This synthesis window reports zero failures — the loop stayed green throughout. That's worth being skeptical of, not celebratory about. Five runs is not enough to say the retry/CI-parity fix or the new integration tests are actually solid; it's enough to say they didn't break anything in five tries. The recurring-failures section is empty because there's nothing to synthesize yet, not because failure modes have been eliminated.

## Operational takeaways

1. **CI/local parity bugs are silent until retried.** If your loop retries failed steps, verify retries behave identically in CI and in the dev loop — divergence there is exactly the kind of bug that looks like flakiness until someone traces it to environment drift.
2. **Orchestrator paths need integration coverage before they need more features.** Adding fake-runner tests for `run/once/heal/implement` after the fact is a sign these paths shipped ahead of their test harness — a pattern worth watching for in future orchestrator changes.
3. **Model-routing-by-complexity is a cost lever, not a quality one.** It's showing up as a "lesson" here, meaning it was likely bolted on rather than designed in from the start — worth checking whether complexity scoring is itself tested, or just trusted.

Next window's real test is whether the retry/parity fix holds under a failure, not just under five passes.
