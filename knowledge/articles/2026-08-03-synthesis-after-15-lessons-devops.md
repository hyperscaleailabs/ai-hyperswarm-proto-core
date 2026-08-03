---
tags:
  - article
  - persona/devops
---

# Five Green Runs, and What It Took to Get There

Our last five pipeline runs all passed — two `implement` tickets, three `improve` tickets. That's the kind of stat that looks great on a dashboard and tells you almost nothing. Here's what actually went into keeping the loop green, and where it's still fragile.

## What shipped

**Model selection by task complexity.** The orchestrator now picks which model tier runs a given skill based on the complexity of the task, not a fixed default. This is a cost/reliability lever, not a cosmetic one — over-provisioning every task to the biggest model burns budget for no quality gain; under-provisioning risks silent correctness failures on harder tickets. Getting the complexity heuristic right matters more than picking a "smart enough" model.

**Fake-runner integration tests for orchestrator paths.** We added integration tests that exercise `run-once`, `heal`, and `implement` against a fake runner instead of the real one. This is the unglamorous fix for a real problem: orchestrator logic was previously only exercised end-to-end, which made failures expensive to reproduce and slow to isolate. A fake runner lets you assert on orchestration behavior — retries, state transitions, error propagation — without needing a live environment.

**Reference-set snapshot refresh.** A recurring chore: keep the reference set current and extract one reusable practice from it each cycle. Small, boring, and exactly the kind of task that silently rots if no one owns it.

**Explicit phase artifacts (from MetaGPT).** Making the intermediate artifacts of each pipeline phase explicit and inspectable, rather than implicit state passed between steps. This is a debuggability investment — when a run misbehaves, you want to look at what phase N actually produced, not reverse-engineer it from phase N+1's input.

**Loop reliability: retry and CI parity.** This is the one worth flagging hardest for a DevOps audience. The lesson title itself signals the failure mode it's answering: local loop behavior and CI behavior had drifted apart, and retries weren't handling transient failures consistently between the two environments. The fix brought them into parity — but the fact that this needed a dedicated fix means it had already caused pain before this window started.

## The honest part

This synthesis window shows zero failures — five passes, no regressions, no recurring failure clusters. That's a real result, not spin. But a five-run green streak with no failures is also a small sample; it tells you the loop is stable *right now*, not that it's failure-proof. The retry/CI-parity fix exists precisely because "stable right now" had failed before. The operational lesson isn't "we're done hardening the loop" — it's "the loop only stays green because someone keeps fixing the boring parts: test scaffolding, snapshot chores, artifact visibility, retry semantics." None of that shows up in a pass/fail count until it's missing.
