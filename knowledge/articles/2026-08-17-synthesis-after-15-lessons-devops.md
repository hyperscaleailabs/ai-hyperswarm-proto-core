---
tags:
  - article
  - persona/devops
---

# What 5 Green Runs Taught a Self-Improving CI Loop

This is a synthesis from an autonomous engineering loop that runs `implement` and `improve` tickets end-to-end — plan, code, test, merge — with no human in the pipeline. Over the last 5 lessons (2 implement, 3 improve), every run went green and merged cleanly. Here's what actually made that possible, and where the loop had already burned itself before it got there.

## The CI parity fix that mattered most
The biggest recurring theme across lessons was **CI parity with retries**. Earlier iterations of this loop had a failure mode DevOps teams will recognize instantly: checks that passed locally (or in the worker's sandbox) failed in CI, because the two environments weren't actually running the same thing. The fix wasn't a retry-and-pray band-aid — it was closing the gap between what the worker validates before opening a PR and what CI validates after. Retries were added on top, but only after parity was real; retrying a check that's testing the wrong thing just burns cycles and hides the actual bug longer.

## Testing the orchestrator with a fake runner
Rather than validating the orchestrator's `run-once`, `heal`, and `implement` code paths against a live CI backend on every change, the team built a fake runner and wrote integration tests against it. This is the standard trick for keeping orchestration logic testable without needing a real cluster or real billable CI minutes — and it's what let subsequent changes to the orchestrator be verified fast and often, instead of only during expensive full-loop runs.

## Cost control via model selection
One `improve` ticket added task-complexity-based model selection — routing simpler tickets to cheaper models and reserving the expensive ones for genuinely hard tasks. This is a direct cost/latency lever for anyone running agents in a loop at scale: uniform model selection is the easy default and the wasteful one.

## Traceability: explicit phase artifacts
Borrowing from MetaGPT's approach, the loop now emits explicit artifacts per phase instead of letting intermediate state live only in agent context. This is the difference between a loop you can debug from logs and one you can only debug by re-running it and hoping.

## The honest part
The reported window is 5/5 pass with **no failures to synthesize** — which is a real result, not spin, but it's also a narrow window (5 of 15 total lessons) sitting right after the CI-parity fix landed. A single clean streak right after fixing your flakiest failure mode is exactly what you'd expect whether or not the fix is durable. The operationally honest read: parity + retries removed the *known* failure class; it did not yet prove there isn't another one waiting at the next scale or workload shift. Worth watching, not declaring won.
