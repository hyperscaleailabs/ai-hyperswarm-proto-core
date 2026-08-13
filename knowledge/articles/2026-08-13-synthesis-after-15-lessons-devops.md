---
tags:
  - article
  - persona/devops
---

# CI/CD Notes from an Autonomous Build Loop: 15 Lessons In

We've been running a self-improving build loop — an agent that picks up tickets, implements them, and merges on green CI — for 15 lessons now. The last 5 (2 `implement`, 3 `improve`) all passed. Here's what actually made that possible, and where it still breaks.

## What worked

**Fake-runner integration tests over live orchestration.** Instead of running the full orchestrator (plan → heal → implement) end-to-end for every test, we built a fake runner that simulates the `run-once`/`heal`/`implement` paths. This cut test time and flakiness dramatically — but it's a fake, so it only catches what we bothered to model. Anything in the real orchestrator's plumbing that the fake doesn't simulate ships blind.

**Model selection by task complexity.** Not every ticket needs the biggest model. Routing trivial tickets to cheaper/faster models and reserving heavier reasoning for genuinely complex ones cut cost without a measurable quality drop — but "measurable" is doing some work there; we're going on 5 lessons of green, not a large sample.

**CI parity for retries.** This was a direct response to a recurring failure mode: the loop would retry a step locally in a way that didn't match what CI actually re-ran, so a "fixed" ticket would pass locally and fail in CI, or vice versa. Aligning retry semantics with CI's actual re-run behavior closed that gap. This is worth flagging explicitly because it's the kind of bug that's invisible until you specifically audit for divergence between your retry harness and your CI runner.

**Explicit phase artifacts (MetaGPT-style).** Making each phase (design, implement, review) emit a concrete artifact instead of passing implicit state between agent turns made failures easier to localize — when something broke, you could point at which artifact was wrong instead of re-running the whole pipeline to find out.

## What we're being honest about

The headline number — 5/5 pass, zero failures this window — is not a trophy, it's a small sample. Five lessons is not enough to claim the loop is reliable; it's enough to say the *known* failure modes (CI/retry mismatch, orchestration flakiness) got fixed. New failure modes haven't shown up yet because we haven't thrown enough varied work at it.

We also don't have hard regression protection yet in the worker sandboxes — a known constraint is that workers in loop worktrees can't run `pytest`/`ruff`/`python` directly, so self-verification inside the loop is more limited than it looks from the outside. Green CI on merge is real, but it's the *only* gate; there's no local test signal available to the agent before it pushes.

## The operational takeaway

None of this was one big fix — it was five small, boring changes: mock what's slow, route by complexity, match retry behavior to CI, make phases legible, and now (per the governance-layer work landing alongside this) start tracking quota/cost as a budget gate instead of an afterthought. The loop got more reliable by removing sources of divergence between what the agent believes happened and what actually happened, not by making the agent smarter.
