---
tags:
  - article
  - persona/architect
---

# Five Loops In: What Held Up and What Didn't

Five autonomous engineering runs, zero failures. That's the headline number, and it's also the least interesting part of the story — a 5/0 streak over one window tells you the loop stayed stable, not that the architecture is sound. What's worth extracting is the pattern set that got us here and where it's still thin.

## What we adopted

**Explicit phase artifacts.** Borrowed from MetaGPT: instead of letting an agent free-associate from ticket to PR, we force it through named phases (design → implement → verify) with a written artifact at each boundary. This is the single highest-leverage change in the last few cycles. It turns an opaque agent run into something reviewable mid-flight — you can inspect the design artifact before code exists and catch a wrong direction cheaply, instead of discovering it in a 400-line diff.

**Reference-set snapshots.** We periodically refresh a curated snapshot of "known-good" patterns extracted from merged work, and feed it back into the next generation's context. This is a deliberate bet on convergence over novelty: each generation should look more like the best of what came before, not reinvent conventions. The tradeoff is real — it's a ratchet, and a bad practice that sneaks into the reference set gets reinforced, not corrected, until someone notices in review.

**Reproduce-before-fix as a gate, not a guideline.** For heal/bugfix tickets specifically, we now require a regression test that fails before the fix and passes after, enforced structurally rather than requested in prose. This came directly out of watching agents "fix" symptoms that didn't reproduce the reported bug — plausible-looking diffs that didn't actually address the failure. Gating it mechanically was necessary; asking nicely in the prompt wasn't sufficient.

**Retry and CI parity for loop reliability.** Flaky infra was eating cycles on transient failures unrelated to the work itself. We tightened the loop's retry semantics and brought local/CI environment parity closer, mostly to stop burning generations on noise rather than signal.

## What we're not claiming

Five green lessons is a small sample from a system that's still young enough that "recurring failure" categories are empty by default — we haven't run long enough to know what the second-order failure modes look like. The recurring-theme extraction ("build," "change," "cleanly," "merged," "green") is honestly more indicative of the reporting template's vocabulary than of deep signal; it's not yet doing the job of surfacing genuinely novel friction.

The bigger open question is verification depth: hsai workers running inside loop worktrees can't execute pytest/ruff directly (sandboxing constraint), so self-verification within a ticket is weaker than we'd like — correctness checks lean more heavily on the reproduce-before-fix gate and downstream CI than on the agent's own test run. That's a known gap, not a solved one, and it's the most likely place a future failure streak originates.

**Net effect:** structural gates (reproduce-before-fix) outperformed prose instructions every time we compared them. That's the transferable lesson — where correctness matters, encode the constraint in the pipeline, not the prompt.
