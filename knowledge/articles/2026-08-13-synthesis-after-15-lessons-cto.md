---
tags:
  - article
  - persona/cto
---

# Five Clean Merges, Zero Failures — What Our Autonomous Build Loop Just Proved (and Didn't)

Our AI-driven engineering loop just closed out its latest window: five tickets in, five merged clean, zero failures. That's a good result, but the honest framing matters more than the scoreline — this is a five-ticket sample, not a trend line, and I want to be clear about what it does and doesn't tell us.

## What happened

The loop shipped two new features (`implement`) and three refinements to existing work (`improve`). Concretely: it added complexity-based model selection so cheaper/faster models handle simple tickets and stronger models get reserved for hard ones; it built fake-runner integration tests to cover the orchestrator's run-once, heal, and implement code paths; it refreshed a reference-set snapshot and extracted a reusable practice from that work; it introduced explicit phase artifacts modeled on the MetaGPT pattern, giving each build stage a durable, inspectable output instead of an ephemeral log line; and it hardened loop reliability with retry logic and tighter CI parity, so a flaky run in CI looks the same as a flaky run locally.

Every one of these landed without a rollback, a hotfix, or a second pass. That's the win, and it's real.

## What I'm not claiming

Zero failures in five tickets is not zero risk. It's a narrow window, and the recurring vocabulary in this batch — "build," "change," "cleanly," "green," "merged" — is exactly what you'd expect from a system that is currently only being asked to do things it's already good at: incremental, well-scoped changes to code it built itself. We have not yet stress-tested it against a genuinely novel, ambiguous, or cross-cutting change in this window, and I don't want a five-for-five streak to read as "solved."

There's also a known verification gap I'm tracking, not from this batch but structurally: workers executing inside loop worktrees can't run the test suite or linter directly — those are denied in that environment by design. Self-verification currently depends on the fake-runner and orchestrator harness catching what a direct `pytest` run would. The new integration tests narrow that gap; they don't close it. Until a worker can prove its own work end-to-end the way a human engineer would, "green" means "green according to the harness," not "green, full stop."

## Where this is heading

The direction is right: push toward cheaper model routing for routine work, explicit artifacts at each phase (so a human can audit a build without replaying it), and CI/local parity so we stop debugging environment drift instead of real bugs. The next test of this loop isn't another clean streak — it's deliberately routing it a harder, messier ticket and seeing whether the reliability work holds. I'd rather find that failure mode ourselves, on our schedule, than have it show up in production.

**Bottom line:** the loop is earning trust on routine work. It hasn't yet earned trust on hard work, and we're not pretending otherwise.
