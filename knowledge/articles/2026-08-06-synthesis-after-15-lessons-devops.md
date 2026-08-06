---
tags:
  - article
  - persona/devops
---

# Five Green Runs: What a Clean CI/CD Window Actually Teaches You

Over the last five lessons in our automation loop — two `implement` tickets, three `improve` tickets — we shipped 5 for 5. Zero failures. That's not the interesting part. The interesting part is what a clean streak like this exposes about the pipeline itself, and where it's still fragile enough that "green" doesn't mean "done."

## What actually happened

The work skewed toward hardening, not features: two-thirds of the tickets were `improve` — refreshing a reference-set snapshot, extracting reusable practices, tightening loop reliability around retries and CI parity. Only two tickets were net-new `implement` work (model selection by task complexity, and a fake-runner integration test harness for the orchestrator's run/heal/implement paths). That ratio matters operationally: a loop that spends most of its cycles improving itself is either maturing or masking debt. In this window it was the former — the improve tickets targeted concrete gaps (CI parity, snapshot staleness) rather than cosmetic cleanup.

## The recurring signal: "merged cleanly" and "build green" show up in 3 of 5 lessons

That's the honest tell. When "build," "change," "cleanly," "green," and "merged" dominate the theme extraction, it means the loop's own retrospective language is converging on process mechanics, not domain outcomes. That's worth flagging rather than celebrating — it can mean the automation is stable, or it can mean the lessons are getting shallow (post-hoc notes about merge status rather than what was learned about the system under test). We didn't have failures to force deeper analysis this round, and that's a gap in the retro process itself: a synthesis step that only fires on failure will under-invest in mining passes for latent risk.

## What we'd flag as failure-adjacent, even without a failure

- **Loop reliability work implies prior flakiness.** A ticket explicitly targeting "retry and CI parity" doesn't materialize from nothing — it's a scar from earlier non-deterministic runs (local pass, CI fail, or transient retries masking a real issue). We didn't capture the original incident in this window's lesson set, which is itself an operational lesson: link remediation tickets back to the failure that motivated them, or the retro loses the "why."
- **Snapshot refresh as a recurring chore.** A reference-set snapshot needing periodic manual refresh is a smell — it's a candidate for a scheduled job or a drift check in CI rather than a recurring human-in-the-loop ticket.
- **Model selection by task complexity landed as a skill, not a gate.** Worth watching whether it's actually enforced in the pipeline or advisory-only; advisory automation tends to erode.

## Takeaway

Five passes in a row is a fine outcome, not a fine data point. The next synthesis window should deliberately sample near-misses — retries that succeeded on the second attempt, CI-vs-local discrepancies — not just binary pass/fail, or we'll keep reporting "no failures" right up until we don't.
