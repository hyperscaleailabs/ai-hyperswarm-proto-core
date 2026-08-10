---
tags:
  - article
  - persona/architect
---

# What Five Green Lessons Actually Taught Us

Over our last window of five lesson cycles — two `implement`, three `improve` — every run landed pass/merge. No failures to mine for regression patterns. That's a good outcome operationally and a mediocre one epistemically: a synthesis loop that only sees green tends to converge on shallow, process-shaped lessons rather than causal ones. Worth naming up front rather than dressing up as a trend.

## What actually got built

**Task-complexity-based model selection.** We stopped routing every ticket through the same model tier and started sizing the model to the task's complexity signal. The tradeoff is real: complexity estimation is itself a heuristic that can misfire on tickets that look simple but aren't (deep call graphs, cross-service state). We accepted that risk because the cost/latency win on the long tail of trivial tickets was large and the failure mode (under-provisioning a hard ticket) is cheaply detectable — the ticket just fails and re-routes up a tier.

**Fake-runner integration tests for orchestrator paths.** Instead of running real workers to test the `run-once`, `heal`, and `implement` orchestration paths, we built a fake runner that simulates worker behavior deterministically. This traded fidelity for speed and determinism — real-worker E2E tests are flaky and slow to gate CI on. The honest caveat: a fake runner can drift from real worker behavior over time, so it's a complement to, not a replacement for, periodic real-worker validation. We haven't yet built that periodic check, which is a known gap.

**Explicit phase artifacts from MetaGPT.** We adopted MetaGPT's pattern of making each SDLC phase (design, implement, review) produce a durable artifact rather than passing state implicitly through agent context. This cost more storage and pipeline plumbing but bought us resumability and auditability — if a phase's output is wrong, we can inspect it in isolation instead of re-running the whole chain to find where it diverged.

**Reference-set snapshot refresh.** A recurring maintenance practice: periodically re-pin the reference set used for evaluation so it doesn't silently drift out of date with the codebase it's meant to represent. Small, unglamorous, but the kind of thing that rots quietly if left to "someone will notice."

**Loop reliability: retry and CI parity.** We hardened the loop's retry logic and tightened parity between local and CI execution environments — a direct response to intermittent loop failures that weren't reproducible locally.

## Pattern across all five

The recurring themes — "build," "change," "cleanly," "green," "merged" — read as generic because they are: they're artifacts of a window with no failures to differentiate the lessons. The actual throughline is narrower: every adopted pattern trades some fidelity or upfront cost (fake runners over real workers, heuristic routing over uniform routing, artifact persistence over implicit state) for speed, determinism, or auditability, and each trade has an explicit, still-open weak point rather than a fully closed loop.

## The honest gap

Zero failures in five lessons isn't evidence the system is failure-proof — it's evidence this window didn't stress it. The retry/CI-parity work exists precisely because failures *do* happen elsewhere in the loop; this synthesis window just didn't sample one. Next window should weight toward harder or more adversarial tickets to keep the failure signal alive.
