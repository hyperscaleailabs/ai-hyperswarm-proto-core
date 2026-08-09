---
tags:
  - article
  - persona/architect
---

# Synthesis After 15 Lessons: What a Green Streak Actually Taught Us

The last five lessons in this loop — two `implement`, three `improve` — all landed clean: 5/5 pass, zero failures. That's worth treating with more suspicion than celebration, but the underlying design decisions are real and worth recording.

## What got built

**Task-complexity-based model selection.** The orchestrator now routes work to different model tiers based on estimated task complexity rather than using one model for every step. This is a classic cost/latency-vs-quality tradeoff, and the honest framing matters: it only works if complexity estimation is cheap and reasonably accurate. Get that wrong and you either burn budget on trivial steps or under-provision hard ones silently — the failure mode doesn't show up as a crash, it shows up as quietly worse output that's easy to miss in a green run.

**Fake-runner integration tests for orchestrator paths.** Instead of hitting real infra for `run-once`, `heal`, and `implement` code paths, the team built a fake runner to integration-test orchestration logic in isolation. This is the standard pattern for keeping integration tests fast and deterministic — the tradeoff is fidelity. A fake runner can pass while the real runner diverges in exactly the ways that matter (timing, partial failures, resource contention). It's a reasonable bet, but it shifts risk toward "things that only break in production," which is precisely the kind of gap a synthesis report full of passes won't surface.

**Explicit phase artifacts, borrowed from MetaGPT.** Making intermediate phase outputs explicit and inspectable (rather than implicit state threaded through the pipeline) is a maintainability and debuggability win — it trades a bit of upfront structure for the ability to audit what happened at each stage after the fact. This is the right call for a system meant to run unattended for many cycles.

**Loop reliability: retry plus CI parity.** Retrying flaky steps and aligning local/loop execution with CI environment closer together reduces false failures. The risk here is the opposite of visibility: retries can paper over genuine intermittent bugs, converting a signal into noise. Worth periodically auditing retry logs for the same failure repeating, not just disappearing.

**Reference-set snapshot refresh.** Routine maintenance work — keeping a reference/golden set current — extracted as a discrete practice. Unglamorous but exactly the kind of hygiene that prevents silent drift between what the system is tested against and what it actually encounters.

## What failed

Nothing, in this window. That's the honest — and slightly uncomfortable — finding. A five-lesson streak with zero failures either means the loop found genuinely stable footing, or the failure bar is set too low to catch the interesting problems (see: fake-runner fidelity gap, retry-masking risk above). At architect review, the actionable move isn't to trust the streak — it's to pressure-test it: inject a known-bad case into the fake runner, spot-check whether complexity-based routing ever misclassified something, and confirm retries aren't hiding a repeat offender.

**Recurring theme across lessons:** "build," "change," "cleanly merged," "green" — vocabulary of a system optimizing for throughput. The next block should explicitly optimize one lesson for catching a failure, not avoiding one.
