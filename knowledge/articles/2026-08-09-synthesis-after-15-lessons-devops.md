---
tags:
  - article
  - persona/devops
---

# Five Green Runs: What a Self-Improving CI Loop Actually Looked Like

Our autonomous dev loop just closed its fifth consecutive lesson with a clean pass: 5/5 green, split across two ticket kinds — 2 `implement`, 3 `improve`. No rollbacks, no halted pipelines, no manual intervention. Here's what that actually took, mechanically, and where the friction still is.

## The mechanics

Each cycle runs as a ticket through a worker in an isolated git worktree. The worker can't run `pytest` or `ruff` directly — those are denied inside loop worktrees by design, so self-verification happens through the harness's own test/CI gates, not ad hoc shell invocation. That's a deliberate constraint: it forces every ticket to prove itself through the same CI path a human PR would use, rather than trusting an agent's self-report that "tests pass."

Two changes landed this window that are worth calling out as operational, not cosmetic:

**Model selection got cost-aware.** A new skill routes tickets to model tiers based on task complexity instead of using one model for everything. This is the kind of change that doesn't show up in a diff of "features" but directly moves the cost/latency curve of running the loop continuously.

**A cost gate went in.** We added quota/cost telemetry — a ledger that tracks spend per block and enforces a warn-then-halt policy. Before this, there was no automated backstop between "loop is running" and "loop burned an unbounded budget." Now it warns first, halts second. This is the unglamorous but load-bearing kind of automation: a circuit breaker, not a feature.

**Test coverage for the orchestrator itself.** We added fake-runner integration tests covering the run-once/heal/implement paths — testing the thing that runs the tickets, not just the tickets it produces. This matters because a bug in the orchestrator silently corrupts every downstream ticket's result.

## What's still unresolved

The recurring-theme extraction ("build," "change," "cleanly," "merged") is honestly pretty thin signal right now — it's surfacing high-frequency words, not causal patterns. That's a known limitation of theme synthesis at this sample size (5 lessons); it needs either a bigger window or better clustering before it's useful for spotting real failure modes.

We also carried forward, but haven't re-validated, an earlier lesson on retry behavior and CI parity — the fact that local/loop test conditions don't perfectly mirror the CI environment. That gap is exactly the kind of thing that stays invisible during a green streak and then bites on the first flaky dependency or environment drift. Five passes in a row is a good sign, not proof it's solved.

**Bottom line for anyone running a similar setup:** the wins this window were guardrails (cost gate, orchestrator tests, cost-aware routing), not user-facing features. That's usually the right trade when you're optimizing a loop that runs unattended — reliability infrastructure compounds; feature velocity alone doesn't.
