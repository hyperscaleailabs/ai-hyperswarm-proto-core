---
tags:
  - article
  - persona/architect
---

# Fifteen Lessons In: What a Clean Streak Actually Tells You

The last five lessons in this loop — two `implement`, three `improve` — all passed. Zero failures. Before reading that as "the system is solved," it's worth being precise about what a green streak like this does and doesn't prove, and what patterns got us here.

## What shipped

The window covered a mix of infrastructure and refinement work: model selection tied to task complexity (routing cheaper models to simpler tickets), a fake-runner integration test suite covering the orchestrator's run-once/heal/implement paths, a refreshed reference-set snapshot with one extracted practice, explicit phase artifacts borrowed from MetaGPT's process discipline, and retry/CI-parity fixes for loop reliability.

The throughline across these: **make failure modes observable and cheap to retry**, rather than trying to prevent them upfront. The fake-runner tests are the clearest example — instead of asserting against a live orchestrator (slow, flaky, expensive to iterate on), we built a fake that exercises the same code paths deterministically. That's a standard test-double tradeoff, but it's easy to skip under time pressure, and skipping it is exactly what produces flaky CI later.

## The tradeoff we made, explicitly

Model selection by task complexity is a cost/quality tradeoff dressed up as an architecture decision. Routing "simple" tickets to a cheaper model only works if the complexity classifier is honest — misclassify a subtly hard ticket as simple and you get a plausible-looking but wrong implementation that passes shallow checks. We don't yet have hard data on false-negative rate for that classifier in this window; that's a gap, not a solved problem.

## Where the "no failures" claim is weaker than it looks

A five-lesson window with zero failures is small-sample good news, not proof of stability. The recurring-themes list — *build*, *change*, *cleanly*, *green*, *merged* — is really one theme repeated: work landed without breaking CI. That's necessary but it's a lagging indicator. It tells you the merge gate worked, not that the underlying design decisions (model routing, phase-artifact schema, retry policy) are the right long-term shape. We haven't yet had a lesson in this window that stress-tested the retry/CI-parity fix under an actual flaky-infra incident — it shipped clean because nothing forced it to prove itself yet.

## What we'd do differently

The explicit-phase-artifacts pattern (from MetaGPT) is a bet that structured intermediate outputs make agent work auditable and resumable. It adds ceremony per ticket. We haven't measured whether that overhead pays for itself on genuinely simple tickets, or whether it's dead weight there and only earns its keep on the complex ones the model-selection layer is supposed to route elsewhere — which means these two changes need to be evaluated together, not in isolation.

**Bottom line:** this window validates plumbing (tests, retries, artifacts) more than it validates judgment (routing, classification). The next useful signal isn't another green streak — it's a failure that exercises the retry path and a misrouted ticket that exercises the classifier, so we can see if the safety nets actually catch what they're meant to.
