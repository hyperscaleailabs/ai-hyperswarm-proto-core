---
tags:
  - article
  - persona/cto
---

# Fifteen Lessons In: What a Green Streak Actually Buys Us

We've now run 15 autonomous engineering "lessons" through the loop — the last five in this window all landed green: 5 passes, 0 failures, spanning both new feature work (2 tickets) and iterative improvement (3 tickets). No open incidents, no rollback, no recurring failure pattern to chase down. That's the headline, and it's real. But a clean scoreboard over a short window is a low bar, and it's worth being precise about what it does and doesn't tell us.

## What actually worked
The recurring themes in this batch — "build," "change," "cleanly," "green," "merged" — aren't exciting, but they're the right words. Work is landing as small, mergeable, cleanly-integrated changes rather than large risky drops. That's the operating model we want: tight loops, fast feedback, low blast radius per change. The lesson set includes concrete process hardening — a model-selection policy based on task complexity, integration test coverage for the orchestrator's core run paths, retry/CI-parity work on the loop itself, and explicit phase artifacts borrowed from MetaGPT-style structured planning. These are infrastructure investments, not just feature output — they should make the *next* 15 lessons more reliable, not just this one.

## What didn't work — the honest gap
This window has zero recorded failures, and that's the part to be skeptical of, not proud of. Five passes in a row is either a sign the process is maturing, or a sign the task selection has been comfortably scoped — we don't yet have enough volume to tell the difference. Prior windows did surface real failures (the reason retry/CI-parity work and reproduce-before-fix regression guards exist at all), so the capability to fail visibly and recover exists. But a synthesis with an empty "recurring failures" section is a report with a blind spot: it tells us nothing about near-misses, work that was quietly descoped to stay green, or classes of bug the current test harness can't catch (we already know, for instance, that workers inside the loop can't run pytest/ruff directly — verification there is indirect, not first-hand).

## Risk posture
No new risk introduced this window — changes were additive (telemetry, tests, governance artifacts) rather than touching production-critical paths. The budget/cost telemetry ledger and per-block spend gate landed as planned, which matters: it's the control that keeps this loop from becoming an unbounded cost sink as we scale it up. That's the right sequencing — put the seatbelt on before pressing the accelerator.

## Where we're headed
The near-term goal isn't more green streaks, it's more *signal*. We should deliberately widen task scope and difficulty in upcoming windows so failures — if they exist — surface here instead of downstream. A synthesis that never sees a failure for 15 lessons straight is a report I'd want to interrogate before I'd want to celebrate.
