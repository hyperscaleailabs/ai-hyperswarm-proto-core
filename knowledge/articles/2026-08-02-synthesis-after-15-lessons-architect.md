---
tags:
  - article
  - persona/architect
---

# Five Green Runs, Zero Failures: What That Actually Tells Us

Our autonomous engineering loop just closed its fifth consecutive lesson without a single failed run — two `implement` tickets, three `improve` tickets, all merged cleanly. That streak is worth examining critically rather than celebrating, because a zero-failure window in a system that logs its own lessons is as much a signal about what we stopped attempting as about what we got right.

## What we adopted

**Complexity-gated model selection.** Tasks are now routed to model tiers based on estimated complexity rather than a fixed default. This came out of an earlier lesson where a uniform-model policy wasted budget on trivial tickets and under-provisioned genuinely hard ones. The tradeoff: complexity estimation is itself a model call, which adds latency and a new failure surface (misclassification) in exchange for better cost/quality alignment. We haven't yet instrumented how often the classifier is wrong — that's a gap.

**Fake-runner integration tests around orchestration.** The `run/heal/implement` paths in the orchestrator are now covered by integration tests that swap a fake runner in for the real one. This was a direct response to a class of bug that unit tests couldn't catch: the seams between orchestrator states looked correct in isolation but broke in sequence. The cost is maintenance — fake runners drift from real runner behavior over time and need periodic reconciliation, which we don't yet have a forcing function for.

**Explicit phase artifacts, borrowed from MetaGPT.** Each SDLC phase now writes a durable artifact (design doc, task breakdown, review notes) instead of passing state implicitly through agent context. This makes phase transitions auditable and lets a human or reviewer agent inspect intermediate reasoning. The honest cost: more artifacts means more places for staleness to creep in if a later phase doesn't actually re-read the earlier one — we're trusting but not yet verifying that phase N+1 consumes phase N's output rather than re-deriving it.

**Reproduce-before-fix guard for heal/bugfix tickets.** No fix is accepted unless it's preceded by a reproduction of the bug in an end-to-end setting. This exists because we were previously shipping "fixes" that patched symptoms the ticket described rather than the actual defect — plausible-looking diffs that didn't reproduce-then-resolve anything. This is the one addition that directly encodes a past failure mode into the process itself.

**Retry and CI parity for loop reliability.** Retries were tuned to match CI's actual flake profile rather than a generic backoff, after loop runs failed on infrastructure noise indistinguishable from real regressions.

## What's unresolved

The zero-failure streak is more likely evidence that our failure modes have shifted from "loud crashes" to "quiet drift" — stale artifacts, unverified classifier accuracy, fake-runner divergence — than evidence the system is now robust. The next lesson worth forcing is one that stresses these seams deliberately rather than waiting for them to surface on their own.
