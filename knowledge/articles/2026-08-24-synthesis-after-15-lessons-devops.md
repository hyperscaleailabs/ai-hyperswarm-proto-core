---
tags:
  - article
  - persona/devops
---

# Five Green Runs: What It Took to Keep the Loop That Way

Our autonomous build loop just closed its fifth consecutive clean cycle — five tickets (two `implement`, three `improve`), zero failures. That streak is worth being honest about: it's not because the pipeline got lucky, it's because the last several cycles were spent hardening the exact places where it used to break.

## What we actually shipped

**Model selection got cheaper, not smarter.** We added a complexity-based router that picks the model per task instead of defaulting every ticket to the heaviest one. Mechanically this is just a pre-step in the orchestrator that classifies a ticket before dispatch — but it only works if the classifier is boring and deterministic. The first version scored complexity from free-text ticket descriptions and was flaky enough to mis-route small chores onto expensive models. We ended up scoring off structured ticket metadata instead of prose.

**We stopped trusting "it ran" as a test result.** The orchestrator's `run-once`, `heal`, and `implement` paths had no integration coverage — CI was asserting that steps executed, not that they executed *correctly* end to end. We built a fake-runner harness that simulates the loop without touching real infra, which finally let us catch orchestrator regressions in CI instead of in production runs. This is the boring-but-critical kind of work: no feature, just closing a blind spot that had been there since the loop's first version.

**Retry logic and CI parity.** This is the one with real scar tissue behind it. Loop runs were failing in ways that never reproduced locally, because the CI environment and the loop's runtime environment had quietly drifted apart — different retry/backoff behavior on transient failures, different assumptions about what counts as a clean worktree. We hadn't reproduced these failures in an end-to-end setting before attempting fixes in earlier cycles, which is exactly backwards, and it cost us wasted iterations chasing symptoms. The fix that stuck was aligning retry semantics between CI and the loop runtime and treating any divergence between them as a bug in its own right, not an environment quirk.

**Phase artifacts, MetaGPT-style.** We made each SDLC phase (design, implement, review) emit an explicit artifact instead of relying on the orchestrator's implicit state, borrowing the pattern from MetaGPT's role-based pipeline. Practically: every phase now writes something a human or the next phase can inspect, rather than trusting an in-memory handoff.

## The recurring theme

Every one of these lessons traces back to the same failure mode: *the loop said green when it wasn't actually verified.* Fake state passing as tested state, implicit handoffs passing as verified handoffs, local success passing as CI success. None of these were dramatic outages — they were quiet trust erosion that only showed up as flaky re-runs and hard-to-reproduce failures downstream.

The lesson for anyone running a similar autonomous CI loop: treat "the pipeline is green" as a claim to verify, not a fact to consume. Every fix here was really the same fix — replace an assumption with an artifact you can check.
