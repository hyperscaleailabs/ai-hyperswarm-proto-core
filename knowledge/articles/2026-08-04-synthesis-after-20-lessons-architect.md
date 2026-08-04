---
tags:
  - article
  - persona/architect
---

# Five Foundations Locked: The Loop Learned How to Not Repeat Itself

Five consecutive passing lessons, block 41337, and we just closed a loop that's begun to talk to itself: synthesis now deduplicates against its own history, workers retrieve lessons before starting work, and the quota ledger feeds back into model selection. These aren't flashy, but they're the difference between a loop that gets better and one that just gets busier.

## The inflection points

**Synthesis learned not to repeat itself.** The practice registry watches for duplicate ideas before filing them. This is trivial in isolation (semantic comparison, cost ~100ms) but profound at scale: we were previously discovering the same improvement three times, filing it, debating it in review, rejecting it, then discovering it again a month later. Now the cycle front-loads that dedup and saves everyone's time. The tradeoff: we need a good definition of "duplicate" — near-copies (same insight, different framing) still slip through and require architect judgment. The registry is permissive on purpose; false negatives are cheaper than false positives.

**Workers became aware of precedent.** When a ticket arrives, the worker now retrieves 3-5 related past lessons and sees what we tried before — what worked, what we learned. This one-line change (inject lessons into the prompt) reduced repeated mistakes and accelerated decision-making. Honest assessment: we're relying on lesson quality for this to work. If lessons are shallow or misdated, the retrieval becomes noise. It's not yet automated; we're betting on human diligence in lesson-writing.

**Model selection closed its feedback loop.** The quota ledger now informs which model tier to pick for tasks of similar complexity. Early blocks were burning heavy-model quota on trivial chores; now the selector sees that pattern and biases toward cheaper tiers when the ledger supports it. The system is still learning (sparse signal over 20 PRs) and falls back to the static heuristic when uncertainty is high. This is the right way to do it — measure first, let the data accumulate, optimize when the signal is clear enough.

**Invariants became guarded by CI.** Five architectural invariants (ticket-linked PRs, model-recorded, lesson per PR, green-gated merges, subscription-only models) are now enforced by a pre-merge gate. This stops regressions that tests can't see. The gate is strict (no edge-case overrides yet), which means it occasionally blocks legitimate work that violates a rule for good reason. That's acceptable for now; if it becomes frequent, we'll add an explicit-exemption mechanism.

**Governance instrumentation started.** Added an evidence ledger that dumbly collects signal per PR: model, tests, lesson, CI outcome. No analysis yet — just infrastructure. The next step is alerting (e.g., "test coverage dropped this week") and the one after that is feeding those alerts back into synthesis priorities.

## What's still unresolved

The loop is now talking to itself, but it's still learning to listen. Synthesis dedup is permissive (lets near-copies through). Lesson retrieval assumes well-written lessons. Model selection's feedback loop is slow to adapt (needs weeks of data). The invariants gate is all-or-nothing (no nuanced overrides). These are not failures — they're edge cases that a young system lives with until it has enough evidence to be stricter.

The inflection we just passed: the loop went from "self-correcting" (fixing mistakes after they surface) to "self-aware" (knowing what it tried before). That shift is subtle but substantial. It means fewer redundant explorations and smarter synthesis candidates going forward.
