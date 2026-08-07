---
tags:
  - article
  - persona/architect
---

# What Five Green Runs Taught an Architect Loop

Our self-improving build loop — the system that takes a sealed handoff and produces the next generation of a microservice, tested and merged, without a human in the middle — just closed its fifth consecutive pass. Zero failures in this window. That's worth being suspicious of, not proud of, so here's what actually changed under the hood and why.

## Model selection became a routing problem, not a config knob

Early on we picked one model tier for the whole loop. It was wasteful on trivial steps and occasionally under-provisioned on genuinely hard ones. The fix was to treat model choice as a function of task complexity signals extracted from the ticket — file count touched, whether it's a net-new implementation vs. a bugfix, presence of ambiguous requirements — rather than a static per-skill setting. Tradeoff: this adds a classification step that can itself be wrong, and a misclassified "simple" task now silently gets under-resourced instead of failing loud. We accepted that risk because the alternative (always over-provisioning) doesn't scale cost-wise across generations.

## Fake-runner integration tests, not more mocks

The orchestrator's `run-once`, `heal`, and `implement` paths were previously tested with unit-level mocks that verified call shapes, not behavior. That gave false confidence — the paths could be individually correct and still compose into a broken loop. We replaced that layer with a fake runner: a real orchestrator driving a stubbed execution backend that behaves like the real one (returns realistic artifacts, can be told to fail mid-step). This caught coordination bugs unit tests structurally can't see. The cost is slower, more complex test setup, which we judged worth it for a system that runs unattended.

## Explicit phase artifacts, borrowed from MetaGPT

We moved from implicit state (agents inferring what phase they're in from conversation history) to explicit phase artifacts — each phase writes a typed document that the next phase reads, rather than re-deriving context. This is the same pattern MetaGPT uses for role handoffs. It cost us upfront design work to define the artifact schemas, but it made failures debuggable: when something breaks, you can point at which phase produced a bad artifact instead of grepping transcripts.

## Loop reliability: the CI-parity gap was the real bug

The most consequential fix wasn't a feature — it was discovering that local retry behavior didn't match CI retry behavior, so the loop could pass locally and flake in CI, or vice versa. We closed the gap by making the loop's retry policy CI-aware rather than environment-agnostic. This is the kind of bug that doesn't show up as a "failure" in a lessons ledger — it shows up as unexplained variance, which is arguably worse.

## The honest caveat

Five passes with no failures is a small sample from a loop that's gotten better at avoiding the failure modes it already knows about. It says less about robustness against novel failure classes than the "0 fail" headline implies. The next useful lesson will probably come from a failure, not another clean pass — and the CI-parity gap is a reminder that the absence of visible failures isn't the same as the absence of bugs.
