---
tags:
  - article
  - persona/devops
---

# Five Green Runs, and the Machinery Behind Them

Our last five lesson-cycles all landed clean — 5 pass, 0 fail. That's worth being honest about on both sides: it's a good result, but a 100% pass rate over five runs is a small sample, and it says more about the guardrails we'd just tightened than about the system being bulletproof.

## What we shipped

Two `implement` and three `improve` cycles went through the pipeline:

- **Model selection by task complexity** — the orchestrator now picks model tier per skill task instead of a fixed default. Mechanically this is a routing rule in front of the agent dispatch step, not a new model.
- **Fake-runner integration tests** for the orchestrator's `run-once` / `heal` / `implement` paths — this is the one that actually moves the needle operationally. Before this, those three code paths were only exercised by real runs, which meant failures surfaced in production-adjacent loops instead of CI.
- **Loop reliability: retry + CI parity** — the loop was retrying on transient failures in a way that didn't match how CI classified the same failures, so a run could "pass" locally/in-loop and still be flagged inconsistently downstream. This cycle aligned the retry logic with CI's definition of failure.
- **Reference-set snapshot refresh** plus one extracted practice — routine but easy to skip; stale snapshots are a quiet source of false negatives in later runs if left alone.
- **Explicit phase artifacts** (MetaGPT-style) — making intermediate phase outputs first-class artifacts instead of implicit state, mainly for auditability.

## What actually broke, before this window

The synthesis for this block shows zero failures, but that's the output of the CI-parity fix, not the absence of a problem. The recurring failure mode we'd been chasing going into this block was **retry/CI mismatch**: the loop's retry logic and CI's pass/fail logic were drifting apart, so a flaky step could be "handled" by the loop's retry while CI still recorded it as red, or vice versa. That inconsistency is what the "loop reliability: retry and CI parity" lesson was written to close. It didn't get fixed by writing better tests first — it got fixed by making the two systems agree on what "failure" means, then adding tests.

Similarly, the fake-runner integration tests exist because the `heal` and `implement` paths didn't have any CI coverage that didn't require a live run — a gap, not a feature.

## Operational takeaways

1. **A clean run of 5 is not a trend.** Track pass rate over a rolling window, not per-block.
2. **Retry logic and CI's failure definition must be the same source of truth**, or you get silent disagreement between "the loop says it's fine" and "CI says it's red."
3. **Coverage gaps hide until something exercises the path.** The `heal`/`implement` orchestrator paths ran unsupervised in production before they had integration tests — worth auditing other paths for the same gap.
4. **Snapshot/reference-data refresh is cheap now and expensive later.** Treat it as a recurring chore, not an ad hoc fix.

Recurring terms across this window — *build*, *change*, *cleanly*, *green*, *merged* — track with what you'd expect from a period focused on getting CI trustworthy again: the work was about the mechanics of landing changes cleanly, not new capability.
