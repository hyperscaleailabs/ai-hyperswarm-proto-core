---
tags:
  - article
  - persona/architect
---

# What Five Green Changes Taught Us About the Build Loop

Five changes landed this block — two implementations, three improvements — all passing, all merged cleanly. That streak is worth being suspicious of as much as proud of: a loop that never fails either means the design is solid, or the failure modes aren't being exercised yet. Here's what actually shipped and where the risk still sits.

## Model selection by task complexity

We moved from a flat model choice to routing tasks by estimated complexity — cheap/fast models for mechanical work, stronger reasoning for design-heavy tickets. This is a classic cost/quality lever, but the real risk isn't cost, it's misclassification: a task that looks mechanical (a "simple" refactor that turns out to touch a concurrency invariant) getting routed to a model that won't catch it. We don't yet have a fallback path — if a low-tier model produces a subtly wrong result, nothing currently escalates it to a stronger model for a second pass. That's a gap worth tracking before it produces a defect that ships.

## Fake runner for orchestrator integration tests

We built a test fake for the orchestrator's run-once/heal/implement paths instead of exercising the real runner in CI. Trade-off, honestly stated: this buys fast, deterministic tests at the cost of fidelity. Fakes drift from the real runner's behavior over time — a fake that hasn't been re-validated against production integration paths is a green check that's stopped meaning what it used to. This pattern only stays trustworthy if the fake is periodically reconciled against real runs; that reconciliation isn't automated yet.

## Reference-set snapshot + practice extraction

We started snapshotting the reference set and mining it for reusable practice, rather than treating each cycle's context as disposable. This is a knowledge-compounding bet: pay a small tax each cycle to make future cycles cheaper. The failure mode here is silent staleness — a snapshot that quietly diverges from what's actually true in the codebase and gets trusted anyway. No expiry or drift-detection exists yet.

## Explicit phase artifacts (MetaGPT-style)

Borrowing from MetaGPT, we made each phase of the work loop (design → implement → review) produce an explicit, inspectable artifact instead of passing implicit state between steps. This is the most defensible change of the five: it trades a bit of verbosity for auditability, and it's the one pattern that would have caught a failure had one occurred, since each phase's output is now checkable independently rather than trusted end-to-end.

## Loop reliability: retry and CI parity

We hardened the loop's retry behavior and tightened CI parity with local runs. This is necessary plumbing, not a novel design choice — but it's exactly the kind of infrastructure that only proves itself under failure, and this window had none. Retry logic that's never seen a real transient failure is retry logic that's still unverified.

## The honest takeaway

Zero failures in five changes is a thin sample. The patterns adopted this block are individually reasonable, but three of them (model routing, fake runner, snapshot practice) share the same latent risk: they degrade silently rather than loudly. The next block's job isn't just shipping more green — it's deliberately testing whether these safety nets catch something when they need to.
