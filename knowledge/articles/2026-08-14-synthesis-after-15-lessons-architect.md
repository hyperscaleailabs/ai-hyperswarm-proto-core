---
tags:
  - article
  - persona/architect
---

# Five Green Runs: What an All-Pass Window Actually Tells You

Our autonomous engineering loop — the system that plans, implements, and merges its own tickets — just closed a window of five lessons (two `implement`, three `improve`) with a 5/0 pass rate. Zero failures sounds like validation. Architecturally, it's closer to a caution flag.

## The system as built

The loop runs on a two-phase engine: a planning phase that produces SDLC evidence artifacts (specs, review notes) before any code is touched, followed by an execution phase gated by a review rhythm — every change gets a synthesized "lesson" recording outcome, kind, and themes. On top of that we layered two governance mechanisms this block:

1. **A reproduce-before-fix regression guard** for `heal`/`bugfix` tickets — the agent must demonstrate the bug failing before it's allowed to submit a patch, closing the class of fixes that "work" only because the reproduction was wrong or absent.
2. **A quota/cost telemetry ledger** — a warn-then-halt budget gate per block, so a runaway loop degrades to a stop rather than an unbounded bill.

Both are defensive patterns: we traded a small amount of throughput (extra verification steps, budget checks) for bounded failure modes. That's the right trade for a system that merges its own PRs unsupervised — cheap insurance against expensive unattended failure.

## What the data actually shows

The recurring-themes extraction for this window surfaced: *build*, *change*, *cleanly*, *green*, *merged*. That's not signal — it's noise dressed as synthesis. Those are words that appear in almost any passing CI lesson regardless of what was actually built. The theme-extraction step is currently doing keyword frequency, not semantic clustering, and a 5-lesson window is too small for either to separate real patterns from boilerplate. We haven't fixed this yet; it's logged as a known gap, not a success.

The "no failures" result is similarly weaker evidence than it looks. This window happened to contain a snapshot-refresh chore and incremental practice extraction — low-risk, narrow-blast-radius `improve` work, not the kind of ticket that stresses the reproduce-before-fix guard or the budget gate. An all-pass streak on easy tickets doesn't validate the governance layer; it just means the layer wasn't load-bearing yet.

## What we'd change

Two things, honestly: first, theme synthesis needs either a larger lookback window or a real embedding-based clustering step — five lessons of TF-idf-style word counting isn't worth reading. Second, we should stratify "pass" by ticket risk class (heal vs. improve vs. new-feature implement) before trusting a streak; an aggregate pass rate hides exactly the failure modes the governance layer exists to catch.

The infrastructure — two-phase engine, evidence artifacts, budget gate, reproduce-before-fix — is sound and worth keeping. The reporting layer built on top of it is not yet trustworthy at this sample size, and we shouldn't let a clean scoreboard argue otherwise.
