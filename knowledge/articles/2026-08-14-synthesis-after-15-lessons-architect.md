---
tags:
  - article
  - persona/architect
---

# Five Green Iterations: What It Took to Get an Autonomous Build Loop to Stop Lying to Itself

Over the last five lessons in this loop — two `implement`, three `improve` — every run passed. That streak is worth being suspicious of, because it was earned by removing specific failure modes, not by avoiding hard problems. Here's what actually changed under the hood.

**Model selection got tied to task complexity, not task kind.** Early on, every ticket got routed to the same model tier regardless of whether it was a one-line config bump or a multi-file refactor. That's simple to reason about but wasteful and, worse, occasionally under-powered for the tickets that needed more context-holding. The fix was a complexity classifier ahead of dispatch. The tradeoff is real: classification is itself a place to be wrong, and a misclassified ticket now fails silently at the *wrong* model rather than obviously at the right one. We accepted that risk in exchange for cost and latency wins, but it means the classifier needs its own audit trail — we don't fully have that yet.

**Orchestrator tests moved to a fake runner.** The `run-once` / `heal` / `implement` paths were previously only exercised end-to-end against real worktrees, which made the test suite slow and flaky in ways that had nothing to do with orchestrator logic — a git lock, a slow clone, a transient CI runner. Swapping in a fake runner for integration tests bought speed and determinism. The honest cost: a fake runner tests the orchestrator's *contract* with its runner, not the runner itself. We now have a gap where a real-runner regression (like the environment mismatches below) can pass every fake-runner test and still break in production. That gap is deliberate, not accidental, but it's a real gap.

**CI parity and retry logic came out of actual pain, not foresight.** Loop reliability work landed specifically because the loop's local success didn't match CI's — jobs that passed locally were failing in CI due to environment drift, and transient failures were being treated as real ones. The fix wasn't "add more retries" blindly; it was distinguishing retryable infrastructure failure from genuine task failure, and closing the local/CI gap so a green local run predicts a green CI run. This is the one place I'd call an actual failure that got fixed rather than a tradeoff — the loop was giving false confidence before this landed.

**Phase artifacts, borrowed from MetaGPT, made failures legible.** Rather than a monolithic "it worked / it didn't," each phase (design, implement, review) now emits its own artifact. This is pure upside for debuggability, at the cost of more files to govern — which is why reference-set snapshots now get refreshed and pruned explicitly rather than growing unbounded.

The pattern across all four changes: every reliability win came with a corresponding new blind spot (classifier trust, fake-runner fidelity). A five-for-five window is a checkpoint, not proof the blind spots are closed.
