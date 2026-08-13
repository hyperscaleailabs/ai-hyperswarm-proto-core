---
tags:
  - article
  - persona/architect
---

# Synthesis After 15 Lessons: What a Green Loop Actually Costs

Five lessons, five passes, zero failures. On its face that's the least interesting kind of update — no incident, no postmortem. But a run of five consecutive clean cycles in an autonomous build loop is itself a signal worth examining, because it tells you where the engineering effort actually went: not into feature velocity, but into the scaffolding that makes velocity trustworthy.

## What shipped

The five lessons split roughly into two categories: two `implement` (new capability) and three `improve` (hardening existing capability). That ratio is the headline. In a system that runs itself repeatedly — generating, testing, merging — the majority of sustainable work is not adding surface area, it's reducing the variance of the loop itself.

Concretely, this window adopted:

- **Task-complexity-based model selection** — routing cheaper/faster models to simple tickets and reserving stronger models for complex ones, rather than a fixed model per task type. This is a cost/quality tradeoff made explicit and automatable instead of left to ad hoc judgment.
- **Fake-runner integration tests** for the orchestrator's run-once/heal/implement paths — a deliberate choice to test orchestration logic against a fake runner rather than the real one. This trades some fidelity for speed and determinism, and is only defensible if the fake's contract is kept honest against the real runner's behavior; that's a standing maintenance liability, not a one-time cost.
- **Explicit phase artifacts** borrowed from MetaGPT-style multi-agent design — making intermediate phase outputs first-class, inspectable artifacts instead of implicit state passed between agents. This is a legibility investment: it slows individual runs slightly in exchange for making failures diagnosable after the fact.
- **Retry and CI-parity work for loop reliability** — the most telling item. The existence of a dedicated "reliability" lesson implies the loop was previously flaky enough, or divergent enough from CI, to warrant a targeted fix. That's the closest thing to an admitted failure in this window: the loop wasn't reliable enough before, so reliability itself became a first-class deliverable.

## Recurring pattern: "build," "change," "merged," "cleanly," "green"

The theme extraction isn't glamorous, but it's honest: the vocabulary of these five lessons is dominated by mechanics of shipping, not by domain features. Read charitably, that's a system in the phase where the meta-loop (build → test → merge) is the product, and everything else is downstream of whether that loop can be trusted.

## The honest gap

The synthesis itself admits its limits: no failures in this window means no counterexample to learn from, and a five-lesson sample is too small to claim the loop is now reliable — only that it was reliable *this time*. The CI-parity and retry work exists precisely because "green" and "actually correct" have diverged before elsewhere in this project's history; this window's cleanliness should be read as evidence the mitigations are working, not as proof the underlying flakiness is gone.

## Takeaway for architects

If you're building a self-iterating system, budget real cycles for loop-integrity work — model routing, fake-vs-real test fidelity, artifact legibility, retry/CI parity — before trusting throughput numbers. The interesting failure mode isn't a red build; it's a green one you can't fully explain.
