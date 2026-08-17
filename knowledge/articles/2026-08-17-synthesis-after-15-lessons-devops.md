---
tags:
  - article
  - persona/devops
---

# Five Green Runs: What the Automation Loop Learned

Over the last five lessons in our build loop — two "implement" tickets, three "improve" tickets — every single run passed. No failures, no reverts, no red CI. That's a fine data point, but the more useful part is *what changed* to get there, and what almost didn't work.

## What shipped

**Task-complexity-based model selection.** The skill routing logic now picks model tier by estimated task complexity instead of a flat default. Mechanically this is just a scoring step inserted before dispatch, but it changes cost and latency profiles per ticket — worth watching if your CI budget is metered per-model.

**Fake-runner integration tests for the orchestrator.** The orchestrator's `run-once`, `heal`, and `implement` code paths previously had no integration coverage beyond unit mocks. A fake runner now exercises those paths end-to-end without touching real infra. This is the kind of test that's easy to skip because it doesn't map to a single failing ticket — it exists because someone noticed a class of bug (orchestration state drift) wasn't being caught anywhere.

**Reference-set snapshot refresh + practice extraction.** Part of the "improve" work is now a standing chore: periodically refresh the reference-set snapshot and pull one reusable practice out of it into the shared playbook. This is a low-glamour but important discipline — without it, reference data silently drifts from what the loop is actually being graded against.

**Explicit phase artifacts (MetaGPT-style).** Each phase of a ticket's lifecycle now writes an artifact instead of just advancing implicit state. This is the change that makes the other three legible after the fact — you can't audit "why did the model pick that plan" without a written artifact per phase.

**Loop reliability: retry + CI parity.** This is the one to read between the lines on. A "reliability" ticket that adds retry logic and CI parity checks doesn't get written unless the loop was previously flaky in a CI-specific way — passing locally, failing (or behaving differently) in the CI environment. That's a real operational lesson: environment parity bugs don't show up as a single dramatic failure, they show up as noise that erodes trust in the loop, and someone had to spend a ticket just closing that gap.

## The honest caveat

"5 pass / 0 fail" is a healthy signal, but it's also a small window — five lessons, no failures, isn't enough to say the loop is failure-proof, just that the last batch of fixes (especially the retry/CI-parity work) is holding. The recurring themes across this window — *build*, *change*, *cleanly*, *green*, *merged* — read less like a victory lap and more like the loop's own definition of done: a change only counts once it builds clean, goes green, and merges without manual intervention. That bar is doing a lot of quiet work, and it's the thing worth protecting as the ticket mix gets more ambitious.
