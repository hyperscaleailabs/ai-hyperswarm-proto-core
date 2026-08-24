---
tags:
  - article
  - persona/architect
---

# What Five Green Lessons Actually Tell You

Our last five loop iterations (2 implement, 3 improve) all landed: 5/5 pass, zero recorded failures. That streak is worth examining critically rather than celebrating uncritically — a clean run of five is as likely to reflect narrow scope as it is to reflect a hardened system.

## What we adopted

**Task-complexity-based model selection.** The orchestrator now routes work to a model tier based on estimated task complexity rather than defaulting every ticket to the largest available model. This is a straightforward cost/latency lever, but the interesting design decision was making the complexity estimate a first-class, inspectable artifact rather than a hidden heuristic — when routing goes wrong, we need to see *why* a ticket was scored the way it was, not just that it was.

**Fake-runner integration tests for the orchestrator.** We added integration coverage for the `run-once`, `heal`, and `implement` paths using a fake runner rather than the real one. Tradeoff: fake runners buy speed and determinism but only test the orchestrator's contract with the runner interface, not the runner's actual behavior under load or partial failure. We're explicit internally that this is a scaffolding layer, not a substitute for occasional real-runner smoke tests.

**Explicit phase artifacts, borrowed from MetaGPT.** Each SDLC phase (design, implement, review) now emits a durable artifact instead of leaving state implicit in agent conversation history. This was the highest-leverage change of the window: it turned "trust the transcript" into "trust the artifact," which is what made the reference-set snapshot refresh and reproduce-before-fix guard (added in adjacent work) tractable at all. Without phase artifacts, those two features would have had nothing stable to hook into.

**Loop reliability: retry and CI parity.** We closed a gap where local loop runs and CI runs could diverge on environment assumptions, and added retry semantics for transient failures. This is unglamorous plumbing, but it's the category of fix that, when skipped, produces exactly the kind of flaky, hard-to-reproduce failures that erode trust in a green signal.

**Reference-set snapshot refresh + one extracted practice.** Routine maintenance — refreshing the golden reference set and promoting one recurring workaround into a documented practice. Small in isolation, but this is the mechanism by which the loop's institutional memory compounds instead of resetting every generation.

## What we're honest about

Zero failures in five lessons is not evidence of robustness — it's evidence of an untested failure surface. The recurring themes across lessons ("build," "change," "cleanly," "merged," "green") skew heavily toward *process hygiene* — landing changes cleanly — rather than toward correctness under adversarial or edge-case conditions. We haven't yet forced a failure in this window to confirm the retry/heal paths actually degrade gracefully; we've only confirmed the happy path is fast and clean.

## Next bet

The phase-artifact pattern is the one worth doubling down on: it's the substrate everything else (reproducibility, cost telemetry, review rhythm) is starting to build on. The retry/CI-parity work is the one we'd most want to see exercised by an actual failure before trusting it.
