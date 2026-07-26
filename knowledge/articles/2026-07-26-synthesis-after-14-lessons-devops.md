---
tags:
  - article
  - persona/devops
---

# What Five Green Runs Taught Us About Autonomous CI Loops

Over our last five automation cycles, every run passed: three feature implementations, two improvement patches, zero failures. That's a good streak, but a streak with no failures in the window is also a warning sign for anyone running unattended CI loops - it means the guardrails weren't tested, only the happy path was.

## What actually shipped

**Reproduce-before-fix as a merge gate.** For bugfix and heal tickets, the pipeline now refuses to open a PR until it has reproduced the reported failure in an isolated run first. Before this, "fixes" occasionally patched symptoms that looked plausible but didn't address the actual failing behavior, because nothing forced the automation to first prove it understood the bug. This is now a hard gate, not a suggestion.

**Model selection by task complexity.** The orchestrator started routing tasks to different model tiers based on estimated complexity instead of a single fixed model for every job. This was as much a cost-control move as a quality one: cheaper models on trivial changes, stronger models reserved for anything touching regression-prone paths.

**Fake-runner integration tests around the orchestrator itself.** We added integration tests that exercise the `run-once`, `heal`, and `implement` code paths against a fake runner rather than the real CI backend. This closed a real gap: the orchestrator's control logic had no test coverage of its own, only the code it produced did.

**Loop reliability: retry plus CI parity.** Automation loops now retry transient failures instead of halting the whole cycle, and the local pre-merge check was brought closer to what CI actually runs. Previously, "passes locally, fails in CI" was a recurring class of wasted cycles - the fix was mechanical: make the two environments agree, not add more retries on top of a mismatch.

## The honest part

Nothing in this window failed, which sounds good until you notice the recurring themes across all five lessons: "build," "change," "cleanly," "green," "merged." That's the vocabulary of a loop that is optimizing for landing PRs cleanly, not necessarily the vocabulary of a loop under adversarial pressure. A five-run window with zero failures doesn't tell you the loop is robust - it tells you the loop hasn't been stress-tested recently. We haven't yet forced a run where CI parity fails, or where a retry masks a real regression instead of a transient blip, and until we do, "0 fail" should be read as "not yet failed" rather than "can't fail."

## Next check

The gap worth closing isn't another feature - it's deliberately injecting failure modes (flaky CI, a bad reproduce step, a misrouted model tier) into the loop to see if the new guardrails actually catch them, instead of inferring robustness from an unbroken streak.
