---
tags:
  - article
  - persona/devops
---

# Five Green Runs: What It Took to Get a CI/CD Loop That Doesn't Lie to You

Our last five automated build cycles all passed — 5/5, spanning two work types: net-new implementation and incremental improvement. That streak is worth less as a trophy and more as a data point: it only happened because of specific mechanical fixes made in prior cycles. Here's what's actually running under the hood, and what it took to get here.

## What's in the pipeline

**Task-complexity-based model selection.** The orchestrator now routes work to different model tiers based on estimated task complexity rather than using one model for everything. This is a cost/latency lever, not a correctness one — but it changes what "the pipeline" means, since different steps are now non-uniform in behavior and failure mode. If you're debugging a flaky step, check which tier ran it before assuming the logic is at fault.

**Fake-runner integration tests.** We added integration tests that exercise the orchestrator's `run-once`, `heal`, and `implement` code paths against a fake runner instead of the real one. This was a gap fix — before this, those paths were only validated end-to-end in production-like runs, which is expensive and slow to iterate on. The fake runner lets us catch orchestration bugs (wrong path taken, wrong retry behavior) without paying for a full real build every time.

**Explicit phase artifacts.** Borrowed from MetaGPT-style multi-phase pipelines: each phase now writes down an explicit artifact instead of passing implicit state to the next phase. This is the boring-but-important kind of fix — it's what makes a pipeline debuggable after the fact, because you can inspect what phase N actually produced instead of inferring it from phase N+1's behavior.

**Loop reliability: retry and CI parity.** This is the one that matters most operationally, and it's a tell: if you need a lesson titled "retry and CI parity," it's because retries and CI didn't agree before. The concrete failure mode this fixes is the classic one — a step behaves one way locally/in the loop and another way in CI, so "green" in one place doesn't mean green in the other. Getting parity here means retry logic is now driven by the same conditions in both environments, not tuned separately.

## The honest part

The reporting window itself shows zero failures — genuinely, not a filtered stat. That's worth being suspicious of, not proud of. A 5/5 streak with no failure data is a green loop, not necessarily a robust one; it just means nothing broke *during* the window, not that nothing *can*. The retry/CI-parity fix above is the evidence that earlier windows weren't this clean — this batch inherited a more honest signal because a prior batch paid down the flakiness first.

**Takeaway for anyone running a similar loop:** treat an unbroken green streak as a prompt to check whether your CI and your retry logic are actually testing the same thing, not as confirmation that they are.
