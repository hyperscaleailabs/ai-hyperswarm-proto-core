---
tags:
  - article
  - persona/devops
---

# Five Green Lessons, One Blind Spot: What Our Autonomous Build Loop Actually Learned

Our AI-driven dev loop just closed its fifth clean window: 5 lessons synthesized, 2 `implement` tickets, 3 `improve` tickets, 0 failures. That "0 fail" number should make any DevOps engineer suspicious — a loop that never fails either has excellent guardrails or isn't testing itself hard enough. Here's what actually happened, and where the gap is.

## What worked

**Model selection by task complexity.** One lesson formalized picking a bigger/smaller model based on ticket complexity signals rather than a flat default. This cut wasted cycles on trivial tickets and reduced timeout-driven retries on hard ones — a straightforward cost/latency win once the routing logic existed.

**Fake-runner integration tests.** The orchestrator's `run-once`, `heal`, and `implement` code paths got integration tests against a fake runner instead of the real one. This is the standard trick for testing orchestration logic without paying for live agent runs on every CI trigger — cheap, fast, deterministic, and it caught orchestration-layer bugs that unit tests on individual functions missed.

**Explicit phase artifacts (MetaGPT-style).** Instead of implicit state passed between pipeline stages, each phase now writes a concrete artifact to disk. This made retries and resumption debuggable — you can see exactly what stage a ticket died in instead of reverse-engineering it from logs.

**Retry and CI parity for the loop itself.** The loop's own retry logic was tuned to better match what CI actually does, closing a gap where local loop behavior diverged from what would happen on a real CI runner.

**Reference-set snapshot refresh.** A recurring maintenance task — keeping the loop's reference examples current — got automated as a chore rather than staying a manual, easy-to-forget step.

## What's still broken (the part the "0 fail" number hides)

The loop's worktree-isolated workers **cannot run pytest, ruff, or python directly** — those commands are denied inside loop worktrees for isolation/safety reasons. That means tickets closed by the loop are not self-verified against the actual test suite before being marked done. A "pass" in this window's ledger reflects the loop's own checks completing cleanly, not necessarily a green pytest run.

This is the honest caveat: a 5/5 pass rate from a system that can't run its own tests is not the same signal as a 5/5 pass rate from one that can. Recurring themes in this window — "build," "change," "cleanly," "green," "merged" — describe process hygiene (does it build, does it merge cleanly), not correctness verification.

## The operational takeaway

Structural improvements (phase artifacts, fake-runner tests, model routing) compound reliably and are worth investing in first — they're cheap, low-risk, and immediately visible in fewer stuck/retried tickets. But they don't substitute for closing the test-execution gap. Until worktree workers can run the real test suite — or a trusted post-merge CI gate does it for them before "done" is declared — treat this loop's pass/fail ledger as a process-health signal, not a correctness guarantee. That's the next fix, not a future one.
