---
tags:
  - article
  - persona/devops
---

# Five Green Runs: What an Unbroken CI Streak Actually Tells You

Our autonomous build loop just closed a window of 5 lessons — 2 `implement`, 3 `improve` — with a 5/0 pass rate. Every change built clean, tests went green, and every PR merged without a revert. Here's what that streak actually consisted of, and why an unbroken green run is a signal to inspect, not just a metric to celebrate.

## What shipped

- **Task-complexity-based model selection** — the orchestrator now routes tickets to a cheaper or larger model depending on estimated task complexity, instead of a fixed model for every job.
- **Fake-runner integration tests** — new integration coverage around the orchestrator's `run-once`, `heal`, and `implement` code paths, using a fake runner instead of live infrastructure.
- **Reference-set snapshot refresh** — the practice-extraction pipeline's reference set was refreshed and one new practice extracted from it.
- **Explicit phase artifacts (MetaGPT-style)** — the pipeline now writes out intermediate phase artifacts explicitly, rather than passing state implicitly between stages.
- **Loop reliability: retry + CI parity** — retry logic was tightened so the loop's local execution matches CI behavior more closely, reducing "works locally, fails in CI" drift.

## The operational lessons that recur

Across these five lessons, the same terms keep surfacing: *build*, *change*, *cleanly*, *green*, *merged* — each in 3 of 5 lessons. That's not noise. It reflects a loop that's optimizing hard for "get to green and merge cleanly," which is the right instinct for an automation pipeline, but it's also exactly the kind of pattern that produces false confidence if the definition of "green" hasn't kept pace with what's actually being tested.

## What we're flagging, not fixing

The honest finding here isn't a failure in the traditional sense — it's the *absence* of one. Five consecutive passes with zero recurring-failure entries is a genuinely good outcome, but in a self-improving loop it's also a blind spot risk: a loop that only ever reports success can't distinguish "the code is solid" from "the tests aren't strict enough to catch the next regression." The fake-runner integration tests landing in this same window is the right countermeasure — it's coverage added specifically because prior windows had implicit, untested state transitions in the orchestrator's core paths. That's the pattern to watch: every all-green streak should be paired with evidence that test surface area grew, not just that the existing surface stayed green.

## Practical takeaway for the pipeline

The CI-parity retry work matters more than it looks on paper. "Works locally, fails in CI" is the single most time-expensive failure mode in a loop like this, because it burns a full retry cycle before the loop even gets to evaluate the actual change. Tightening that gap is cheap insurance against wasted iterations — worth doing even in a quarter with no dramatic outages to point to.

**Bottom line:** no incidents to report this window, and that's exactly why the next round of scrutiny should go into whether "green" still means what we think it means.
