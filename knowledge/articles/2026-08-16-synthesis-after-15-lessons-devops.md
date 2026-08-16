---
tags:
  - article
  - persona/devops
---

# Five Green Runs: What the Autonomous Build Loop Actually Taught Us

Our last five loop iterations (2 implement, 3 improve) all landed pass/merged with a clean CI history — no rollbacks, no red-to-green scrambles. That's a good run, but "5/5 pass" is a headline, not a lesson. Here's what actually changed in the pipeline mechanics.

## What worked

**Task-complexity-based model selection.** The loop now routes ticket complexity to model tier before spinning up an agent, instead of defaulting every ticket to the heaviest model. This cut wasted compute on trivial tickets and, more importantly, reduced flaky timeouts we used to see when a light task got starved behind a slow heavy one.

**Fake-runner integration tests for orchestrator paths.** We added integration tests that exercise `run-once`, `heal`, and `implement` against a fake runner rather than the real one. This is the boring but load-bearing fix: previously, orchestrator-path regressions only surfaced in production loop runs, which meant a bad merge could silently break `heal` for hours before anyone noticed. Now those paths are covered pre-merge.

**Explicit phase artifacts (MetaGPT-style).** Each loop phase now writes a discrete artifact instead of passing implicit state between agents. This made failures easier to localize — when something goes wrong, you can point at the artifact from the phase that broke instead of replaying the whole loop to find where state diverged.

**Loop reliability: retry and CI parity.** We tightened retry logic and closed gaps between local and CI environments. This one exists because of a failure class, not a feature request (see below).

## What failed (before this window)

The "loop reliability" and "CI parity" work exists because loop runs were passing locally-ish but failing in CI for reasons that had nothing to do with the code change — environment drift between what the agent's sandbox saw and what CI actually ran. That's a classic and expensive failure mode: you burn agent cycles debugging a "regression" that's actually an environment mismatch. The fix wasn't clever — it was making the CI environment and the loop's execution environment structurally the same, plus adding retries for the genuinely transient failures (network, resource contention) so they don't get misdiagnosed as code bugs.

Separately, we know from operating this: **workers inside loop worktrees cannot run pytest/ruff/python directly** — those are denied inside the sandboxed worktree. That means self-verification inside a ticket's worktree is not real verification; the actual test signal only comes from CI after merge. If you're designing a similar loop, don't let an agent report "tests pass" based on a worktree run — gate merge confidence on the real CI run, not the sandbox echo of it.

## Operational takeaway

Five straight green merges is only trustworthy because of what's underneath it: routed model cost, fake-runner coverage on orchestrator paths, per-phase artifacts for debuggability, and — most importantly — closing the local/CI parity gap that was previously producing false-positive regressions. The recurring theme across lessons ("build," "change," "cleanly," "green," "merged") is less about velocity and more about making sure "green" means what it says.
