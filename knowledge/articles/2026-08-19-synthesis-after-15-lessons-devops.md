---
tags:
  - article
  - persona/devops
---

# Five Green Runs: What the Autonomous Build Loop Actually Learned

Our engineering loop just closed its fifth consecutive passing block — 5/5, split across 2 "implement" and 3 "improve" tickets. No failures to report this window, which is itself worth being honest about: a clean streak tells you the guardrails are catching problems earlier, not that problems stopped happening. Here's what actually changed in the pipeline mechanics.

## What shipped

**Complexity-based model selection.** The loop now routes tickets to a model tier based on task complexity rather than a fixed default for everything. This is a CI cost/latency lever as much as a quality one — trivial tickets don't need the same reasoning budget as multi-file refactors, and burning the expensive tier on every ticket was previously just waste.

**Fake-runner integration tests for the orchestrator.** The `run/heal/implement` paths in the orchestrator now have integration coverage against a fake runner instead of relying on end-to-end runs against the real worker fleet to catch regressions. This matters operationally: it moves failure detection from "found in production loop run" to "found in CI," which is a strictly cheaper place to fail.

**Reference-set snapshot refresh, done incrementally.** Rather than a big-bang resync of the reference corpus, the loop now refreshes the snapshot and extracts one practice at a time. Smaller, reviewable diffs — the kind of change that's easy to roll back if the extracted practice turns out to be wrong, instead of a single large commit you either accept wholesale or fully revert.

**Explicit phase artifacts, MetaGPT-style.** Each SDLC phase now emits a concrete artifact instead of leaving phase transitions implicit in log output. Operationally this is the difference between "the loop says it did design work" and "here's the design doc it produced" — auditable state instead of trust.

**Loop reliability: retry and CI parity.** This is the one to pay attention to. The fix explicitly targets *CI parity* — meaning prior to this, the loop's retry behavior on failure didn't match how CI itself retries or reports flakiness, so a transient failure could either get masked (loop retries silently, CI would have failed loud) or over-reported (loop treats a real CI flake as a hard failure). Neither is acceptable in an autonomous system where nobody's watching the logs live.

## The honest part

This window's ledger shows zero failures, and the recurring themes across lessons are literally "build," "change," "cleanly," "green," "merged" — in other words, the last five tickets were about *keeping the loop green*, not fixing things that broke it. That's a meta-lesson worth stating plainly: a run of passes right after you ship reliability and test-coverage fixes isn't proof the system is now bulletproof — it's the expected result of just having built the safety net. The real test is the next block, once the loop hits a ticket those fixes weren't designed for.

**Operational takeaway:** invest in catching regressions in CI before they reach the live loop, keep changes small enough to revert individually, and don't mistake a green streak for the absence of failure modes — it may just mean you finally built the detector.
