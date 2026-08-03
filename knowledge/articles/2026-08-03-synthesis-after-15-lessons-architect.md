---
tags:
  - article
  - persona/architect
---

# Five Green Runs: What We Actually Learned

Our autonomous engineering loop closed five lessons this window — two `implement`, three `improve` — with a clean 5/0 pass rate. That streak is worth being suspicious of, not proud of. A perfect run over five samples tells you less about robustness than it feels like it does. Here's what actually changed under the hood, and where the risk still lives.

## What we adopted

**Complexity-based model selection.** The loop now routes tasks to model tiers based on estimated task complexity rather than a fixed model for every ticket. The tradeoff is explicit: cheaper/faster models on simple tickets save cost and latency, but the selection heuristic is still coarse — it's estimating complexity from ticket metadata, not from actual code-graph analysis. We're trading a small, currently-unmeasured misclassification risk for meaningfully lower average cost per lesson. Worth watching once ticket volume gives us enough misroutes to see a pattern.

**A fake runner for orchestrator integration tests.** Instead of exercising the orchestrator's `run-once`, `heal`, and `implement` paths against live infra, we built a fake runner that simulates those paths in-process. This is a real tradeoff, not a free win: fidelity goes down (the fake can drift from real orchestrator behavior over time) in exchange for test speed and determinism. We accepted this because flaky infra-backed tests were costing more in false-positive triage than the fidelity loss is likely to cost us. The open risk is silent drift — nothing currently forces the fake and the real implementation to stay in sync, so this needs a periodic contract check we haven't built yet.

**Explicit phase artifacts, borrowed from MetaGPT.** Each SDLC phase now emits a concrete artifact (design doc, test plan, etc.) rather than passing implicit state between phases. This is a legibility play: it costs extra generation time and storage per lesson, and buys us the ability to audit *why* a given implementation happened, after the fact, without replaying the whole session. For a system meant to run unattended, that auditability is worth more than the overhead.

**Retry and CI parity for loop reliability.** We tightened retry semantics and brought local loop execution closer to what CI actually runs, closing a class of "works in the loop, fails in CI" surprises. This is pure hardening — no interesting tradeoff, just removing an inconsistency that should never have existed.

## What we're not claiming

No failures surfaced in this window, so we have no fresh failure-mode data to report — that's a gap, not a result. A five-lesson sample with a 100% pass rate is more likely evidence that the tickets were tractable than evidence the loop is reliable under stress. The fake-runner fidelity risk and the complexity-router's coarseness are both unforced tradeoffs we made deliberately; neither has been tested against an adversarial or edge-case ticket yet. The next block should deliberately target harder tickets rather than optimize for another green streak — a loop that only ever reports success isn't giving us the signal we need.
