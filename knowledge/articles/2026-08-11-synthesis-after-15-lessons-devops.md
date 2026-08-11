---
tags:
  - article
  - persona/devops
---

# Five Green Runs: What Our CI Loop Actually Taught Us

Our automation loop just closed its last five lessons at 5/5 pass — two `implement` tickets, three `improve` tickets, zero failures. That streak is worth being suspicious of, not proud of: a fully green window usually means either the process matured or the failure modes just haven't shown up yet. Here's what actually changed under the hood, including the parts that didn't work the first time.

## What shipped

**Model selection by task complexity.** The orchestrator now picks a model tier based on estimated task complexity rather than defaulting everything to the same model. This came out of watching cheap, mechanical tickets burn the same budget as genuinely hard ones — the fix is a routing decision, not a prompt tweak, and it only works because complexity is estimated *before* the run starts, not inferred after the fact from how long it took.

**Fake-runner integration tests for the orchestrator.** We added integration tests that exercise the orchestrator's `run-once`, `heal`, and `implement` code paths against a fake runner instead of the real one. This is the boring but load-bearing change: without it, every orchestrator edit was validated only by full end-to-end loop runs, which are slow and expensive to iterate against. The fake runner lets us catch orchestration bugs in seconds instead of a full CI cycle.

**Loop reliability — retry and CI parity.** This is the one with teeth. Retries in the loop had been silently masking a class of failures where local runs and CI disagreed — a ticket would pass locally, fail in CI, retry, and pass on the second attempt without anyone noticing the environments had drifted. The fix tightens CI parity so retries can't paper over that gap anymore. This is the closest thing to a "failure" in this window: it wasn't a broken run, it was a broken *signal* — green builds that shouldn't have been trusted.

**Explicit phase artifacts (from MetaGPT-style staging).** Each phase of a ticket now writes out an explicit artifact instead of passing state implicitly between steps. This makes failures debuggable after the fact — you can inspect what phase 2 actually produced instead of reconstructing it from logs.

**Reference-set snapshot refresh.** Routine maintenance: the reference set used for comparison/validation was going stale, so refreshing it is now a recurring chore rather than a one-off fix.

## The honest takeaway

Nothing broke in this window because the last window's fixes were about *detecting* breakage better — tighter CI parity, faster fake-runner feedback, explicit artifacts. A clean streak right after you improve your instrumentation is a good sign; a clean streak with no instrumentation changes is the one to distrust.
