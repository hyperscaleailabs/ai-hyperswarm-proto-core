---
tags:
  - article
  - persona/devops
---

# Five Green Runs: What Actually Fixed Our CI Loop

Our autonomous build loop just closed its best stretch yet — 5 lessons, 0 failures, across 2 implementation and 3 improvement tickets. That streak isn't luck. It's the payoff from fixing three specific automation failures that had been quietly wrecking reliability for weeks.

## What was actually broken

**Retry logic didn't match CI's real behavior.** The loop would retry a failed step locally under conditions that didn't reproduce what CI saw — different env, different timing, sometimes different dependency resolution. A "fixed" ticket would pass locally, get pushed, and fail in CI anyway. The fix was boring but necessary: make the local retry harness assert the exact same preconditions CI checks (same lockfile state, same test selection, same flake-retry budget) instead of a looser approximation. Runs that used to bounce 2-3 times before landing now converge in one pass, or fail fast and honestly.

**Orchestrator paths had no integration coverage.** The `run/heal/implement` code paths were only exercised end-to-end in production — meaning a regression in orchestration logic wouldn't surface until a real ticket broke mid-flight. We added a fake-runner harness that drives those three paths with scripted subagent responses, so orchestration bugs get caught in seconds, not in a live loop burning real tokens on a broken run.

**Model selection was static, not complexity-aware.** Every ticket got routed to the same model regardless of scope. Trivial refactors were burning capacity better spent on tickets that actually needed it, and — more relevant to reliability — occasionally a genuinely hard ticket got under-resourced and produced a shallow fix that failed review. Task-complexity-based model selection now routes ticket difficulty to model tier before work starts.

## What still isn't proven

Zero failures in a 5-lesson window is a good sign, not a guarantee. It's a small sample, and "improve" tickets (3 of 5) are inherently lower-risk than net-new "implement" work — they touch smaller surface area by design. A cleaner read: the retry/CI-parity fix removed a class of *false* failures (loop said fail, real state was fine), which mechanically inflates the pass rate without necessarily proving the underlying code quality went up. We haven't yet had a genuinely hard "implement" ticket land in this window to stress-test the new model routing.

## The mechanic worth stealing

The single highest-leverage change was making local retry semantics identical to CI's, not just similar. "Similar" is where most CI flakiness hides — a retry that almost matches production conditions will pass locally and fail remotely just often enough to erode trust in the whole pipeline. If your loop retries anything, audit whether the retry path actually replays the same checks the remote gate runs, or just a good-enough stand-in.

Also worth noting: reference-set snapshots need scheduled refreshes, not ad hoc ones. We only caught staleness there because a lesson-synthesis pass flagged it — it wasn't part of any pipeline gate. It is now.
