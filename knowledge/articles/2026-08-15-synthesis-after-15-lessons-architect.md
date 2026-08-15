---
tags:
  - article
  - persona/architect
---

# What Five Green Lessons Actually Taught Us

Five consecutive loop iterations — two `implement`, three `improve` — landed clean: no rollbacks, no reverts, no red CI. That streak is worth examining critically, because an unbroken pass rate is exactly the condition under which architects stop asking hard questions.

## What shipped

The window covered a mix of net-new capability and refinement work: task-complexity-based model selection, a fake-runner integration test suite for the orchestrator's run/heal/implement paths, a reference-set snapshot refresh with one practice extracted into reusable form, explicit phase artifacts borrowed from the MetaGPT pattern, and reliability hardening (retry logic, CI parity) for the loop itself.

The recurring theme across three of five lessons — build, change, cleanly, green, merged — points at a specific design bias: every change was scoped to merge cleanly on its own, rather than batched. That's a deliberate tradeoff. Small, independently-mergeable increments cost more coordination overhead per unit of work, but they cap blast radius and keep `git bisect` meaningful. For a system that's still accumulating governance scaffolding (two-phase engine, SDLC evidence, review rhythm — all recent additions), that's the right call: you don't want a bad assumption baked into five files at once.

## The honest gap

The lesson set reports zero failures in this window. That's not the same as zero risk — it's the density that should worry an architect. Five-for-five is what you'd expect either from disciplined scoping *or* from a review process that isn't yet catching the failure modes that matter. Given that the same window introduced test-fakes for orchestrator paths and CI-parity retries, the more likely explanation is the former: the loop had been failing in ways related to test flakiness and CI drift, and this window is the payoff of fixing that class of problem — not evidence the system is now failure-proof.

That's a pattern worth naming for anyone adopting a similar iterate-and-synthesize loop: **the first "clean" window after a reliability fix is a lagging indicator of the fix, not a leading indicator of stability.** Don't extrapolate the streak forward without independently re-testing the failure modes the fix targeted.

## Patterns adopted, with tradeoffs

- **Model selection by task complexity** — cheaper on average, but only as good as the complexity classifier; misclassification risk is invisible until the wrong-tier model produces a subtly wrong result that passes tests.
- **Fake-runner integration tests over live orchestrator runs** — fast and deterministic, at the cost of not exercising real scheduler/queue timing. Bugs that only manifest under real concurrency won't show up here.
- **Explicit phase artifacts (MetaGPT-style)** — makes intermediate state inspectable and reviewable, which is a genuine governance win, but adds ceremony per task; whether that's worth it scales with how often those artifacts actually get read versus just archived.
- **Reference-set snapshot refresh** — keeps the practice library from drifting stale, but a snapshot-and-extract model only surfaces one practice at a time; it's a slow drip, not a systematic audit.

## Takeaway for architects

None of this is a critique of the outcomes — it's a reminder that a synthesis built from a green streak will always under-report risk. The next useful synthesis isn't "5/5 pass" again; it's whichever failure the fake-runner and CI-parity work was built to catch actually recurring, or not, under real load.
